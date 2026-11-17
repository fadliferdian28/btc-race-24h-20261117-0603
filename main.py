#!/usr/bin/env python3
"""Deep-dip swing rider for the make.fun BTC race (cbBTC/USDC, 1-minute bars).

The idea: on a flat, choppy day the money is in the few real dips, not in
holding. This bot watches the highest high of the last 240 bars and Wilder's
RSI(14) on closes. When the close sits at least 1.0% below that rolling high
while RSI(14) is under 40, the market has dipped hard and is oversold, so it
buys 95% of cash in one order. It exits on the first of: +1.2% over cost,
-2.0% under cost, or 240 bars held. No entry is taken before bar 240, so
the rolling high always spans a full window.

The previous race's bot made ZERO trades because its 7.5% drop trigger never
fired on the race window. This bot never repeats that: if it has still made
no filled buy by bar 1200, the entry loosens to 0.7% below the rolling high
(RSI condition unchanged) for the rest of the window.

Protocol: the harness sends one hello, then one batch per minute bar, then
done. Each batch carries the bar that just closed, the fills of the orders
sent on the previous batch, and the account. The bot answers every batch
with exactly one reply echoing its seq (the vendored shim frames the reply;
this file only decides the orders). Orders decided on a batch fill at the
next bar's open, so every fill price differs from the close the decision
was made on. Long only, $1,000 paper USDC, at most one open position.
"""

import os
import sys
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

# --- Strategy parameters ---
LOOKBACK_BARS = 240        # rolling high window (bars)
RSI_BARS = 14              # Wilder's RSI length
ENTRY_DROP_NUM = 10        # entry: close >= 1.0% below the rolling high
ENTRY_DROP_DEN = 1000
FALLBACK_DROP_NUM = 7      # fallback entry: close >= 0.7% below the high
FALLBACK_DROP_DEN = 1000
FALLBACK_BAR = 1200        # loosen the entry if no filled buy by this bar
RSI_ENTRY_BELOW = 40.0     # entry: RSI(14) below 40 (oversold)
TP_NUM = 1012              # take-profit: +1.2% over cost
TP_DEN = 1000
SL_NUM = 98                # stop-loss: -2.0% under cost
SL_DEN = 100
MAX_HOLD_BARS = 240        # time exit: sell after 240 bars held
SIZE_NUM = 95              # buy 95% of cash per trade
SIZE_DEN = 100
MAX_SLIPPAGE_BPS = 100     # 1% max slippage from the latest close
DD_HALT_NUM = 20           # halt all new trading past 20% drawdown
DD_HALT_DEN = 100
MIN_NOTIONAL = 1_000_000   # $1 in quote units (6 decimals)


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


class SwingBot:
    """One bot's state: indicators, the open position, and the risk guards."""

    def __init__(self, state):
        syms = state.hello.get("symbols", [])
        self.symbol = syms[0]["symbol"] if syms else "CBBTC"
        self.highs = deque(maxlen=LOOKBACK_BARS)  # last 240 bars' highs
        self.last_close = None
        self.rsi_seed = []          # first RSI_BARS closes' changes
        self.rsi_gain = None        # Wilder smoothed average gain
        self.rsi_loss = None        # Wilder smoothed average loss
        self.bars_seen = 0          # bars of our symbol processed
        self.entry = None           # {"bar": idx, "price": fill price}
        self.pending_buy = None     # client_id of a buy awaiting its fill
        self.filled_buys = 0
        self.fallback_on = False
        self.peak_equity = state.equity
        self.halted = False
        self.round_trips = 0
        self.wins = 0
        self.losses = 0

    # -- indicators -----------------------------------------------------

    def rolling_high(self):
        """Highest high of the last LOOKBACK_BARS bars (0 before the first)."""
        return max(self.highs) if self.highs else 0

    def update_rsi(self, close):
        """Feed one close into Wilder's RSI(14); returns RSI or None until warm."""
        if self.last_close is None:
            self.last_close = close
            return None
        change = close - self.last_close
        self.last_close = close
        up = change if change > 0 else 0
        down = -change if change < 0 else 0
        if self.rsi_gain is None:
            self.rsi_seed.append(change)
            if len(self.rsi_seed) == RSI_BARS:
                self.rsi_gain = sum(x for x in self.rsi_seed if x > 0) / RSI_BARS
                self.rsi_loss = sum(-x for x in self.rsi_seed if x < 0) / RSI_BARS
            return None
        self.rsi_gain = (self.rsi_gain * (RSI_BARS - 1) + up) / RSI_BARS
        self.rsi_loss = (self.rsi_loss * (RSI_BARS - 1) + down) / RSI_BARS
        moved = self.rsi_gain + self.rsi_loss
        return 50.0 if moved == 0 else 100.0 * self.rsi_gain / moved

    # -- order builders -------------------------------------------------

    def buy_order(self, state, notional, client_id):
        return {
            "side": "buy",
            "client_id": client_id,
            "symbol": self.symbol,
            "notional_quote_units": int(notional),
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }

    def sell_order(self, state, qty):
        return {
            "side": "sell",
            "symbol": self.symbol,
            "qty": int(qty),
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }

    # -- fills and position tracking ------------------------------------

    def settle_fills(self, state, batch):
        """Match this batch's fills against the pending buy (if any).

        A fill arrives one batch after the order was sent: the order decided
        on the previous batch's bar fills at this bar's open, and this batch
        reports it. A filled buy becomes the tracked entry; a rejected buy
        clears the pending slot so the bot can try again.
        """
        if self.pending_buy is None:
            return
        for fill in batch.get("fills", []):
            if fill.get("client_id") != self.pending_buy:
                continue
            if fill.get("status") == "filled":
                self.filled_buys += 1
                # The fill price is journal-only (exits use the account's
                # cost basis); some harnesses report it as 0, so fall back
                # to the latest close rather than logging a bogus 0.
                fill_price = int(fill.get("price", "0"))
                if fill_price <= 0:
                    fill_price = state.close(self.symbol) or 0
                self.entry = {
                    "bar": self.bars_seen,
                    "price": fill_price,
                }
                held = state.positions.get(self.symbol)
                qty = held.qty if held is not None else 0
                _log(
                    f"[dip] FILLED seq={state.seq} price={self.entry['price']} "
                    f"qty={qty} buys={self.filled_buys}"
                )
            else:
                _log(
                    f"[dip] BUY-REJECTED seq={state.seq} "
                    f"reason={fill.get('reason')}"
                )
            self.pending_buy = None
            return

    def exit_position(self, state, reason, price):
        """Sell the whole position in one order; journal the round trip.

        The tracked entry is cleared when the sell is sent: if the sell is
        rejected, the next batch sees a position with no tracked entry and
        the orphan rule flattens it again, so a rejected exit never strands
        the position.
        """
        held = state.positions.get(self.symbol)
        if held is None or held.qty <= 0:
            self.entry = None
            return None
        if self.entry is not None:
            value = state.value(self.symbol, held.qty, price)
            pnl = value - held.cost
            self.round_trips += 1
            if pnl > 0:
                self.wins += 1
            else:
                self.losses += 1
            _log(
                f"[dip] EXIT {reason} seq={state.seq} price={price} "
                f"entry={self.entry['price']} pnl_units={pnl} "
                f"trips={self.round_trips} W{self.wins} L{self.losses}"
            )
        self.entry = None
        return self.sell_order(state, held.qty)

    # -- the per-batch decision -----------------------------------------

    def decide(self, state, batch):
        """Decide the orders for one batch. Never raises: every batch gets
        exactly one reply, and a crash here would fail the whole run."""
        orders = []
        try:
            orders = self._decide(state, batch)
        except Exception as exc:  # noqa: BLE001 - a reply must go out
            _log(f"[dip] ERROR seq={state.seq}: {exc!r}")
        return orders

    def _decide(self, state, batch):
        orders = []

        # Drawdown guard first: track the equity peak, and if equity has
        # fallen more than 20% from it, flatten any open position and halt
        # all new trading for the rest of the window.
        if state.equity > self.peak_equity:
            self.peak_equity = state.equity
        if (
            self.peak_equity > 0
            and (self.peak_equity - state.equity) * DD_HALT_DEN
            > self.peak_equity * DD_HALT_NUM
        ):
            if not self.halted:
                _log(f"[dip] HALT drawdown over 20% seq={state.seq}")
                self.halted = True
            held = state.positions.get(self.symbol)
            if held is not None and held.qty > 0:
                o = self.exit_position(state, "DD-HALT", state.close(self.symbol) or 0)
                if o is not None:
                    orders.append(o)
            return orders

        # Settle the previous batch's fills before looking at the new bar.
        self.settle_fills(state, batch)

        # Find this batch's bar for our symbol. A batch may carry no bar
        # for it (the recorder took no sample that minute): then there is
        # nothing to decide on, and the reply is simply no orders.
        ours = [b for b in batch.get("bars", []) if b.get("symbol") == self.symbol]
        if not ours:
            return orders
        bar = ours[0]
        high = int(bar["h"])
        close = int(bar["c"])
        self.bars_seen += 1
        self.highs.append(high)
        rsi = self.update_rsi(close)

        # Warm-up batches build history only: orders decided on them are
        # rejected by the harness, so never send any.
        if state.warming_up or self.halted:
            return orders

        # Manage the open position: take-profit at +1.2% over cost,
        # stop-loss at -2.0% under cost, or a 240-bar time exit, whichever
        # comes first. Cost comes from the account (fees included); value
        # is the position marked at this bar's close.
        if self.entry is not None:
            held = state.positions.get(self.symbol)
            if held is not None and held.qty > 0 and held.cost > 0:
                value = state.value(self.symbol, held.qty, close)
                if value * TP_DEN >= held.cost * TP_NUM:
                    o = self.exit_position(state, "TP", close)
                    if o is not None:
                        orders.append(o)
                    return orders
                if value * SL_DEN <= held.cost * SL_NUM:
                    o = self.exit_position(state, "SL", close)
                    if o is not None:
                        orders.append(o)
                    return orders
            if self.bars_seen - self.entry["bar"] >= MAX_HOLD_BARS:
                o = self.exit_position(state, "TIME", close)
                if o is not None:
                    orders.append(o)
                return orders
            return orders

        # Orphan position (held, but no tracked entry and no buy pending):
        # flatten it rather than leaving an unmanaged position open.
        held = state.positions.get(self.symbol)
        if held is not None and held.qty > 0 and self.pending_buy is None:
            _log(f"[dip] ORPHAN seq={state.seq}: flattening")
            orders.append(self.sell_order(state, held.qty))
            return orders
        if self.pending_buy is not None:
            return orders  # a buy is already in flight; wait for its fill

        # Entry: close far enough below the rolling 240-bar high AND RSI
        # oversold. The rolling high is only meaningful once the window is
        # full, so no entry is taken before bar 240: a "240-bar high" over
        # a partial window is a weaker signal, and the first validation run
        # showed an early partial-window entry (bar 179) that the
        # coordinator's backtest never takes. The fallback loosens the drop
        # threshold from 1.0% to 0.7% once bar 1200 passes with no filled
        # buy, so the bot cannot end a window with zero trades the way the
        # last race's bot did.
        if self.bars_seen < LOOKBACK_BARS:
            return orders
        if not self.fallback_on and self.bars_seen >= FALLBACK_BAR and self.filled_buys == 0:
            self.fallback_on = True
            _log(f"[dip] FALLBACK-ACTIVATED seq={state.seq} bars={self.bars_seen}")
        drop_num = FALLBACK_DROP_NUM if self.fallback_on else ENTRY_DROP_NUM
        drop_den = FALLBACK_DROP_DEN if self.fallback_on else ENTRY_DROP_DEN
        top = self.rolling_high()
        deep_enough = top > 0 and (top - close) * drop_den >= top * drop_num
        if deep_enough and rsi is not None and rsi < RSI_ENTRY_BELOW:
            notional = min(state.cash * SIZE_NUM // SIZE_DEN, state.weight_room(self.symbol))
            if notional < MIN_NOTIONAL:
                return orders
            cid = state.client_id()
            self.pending_buy = cid
            _log(
                f"[dip] ENTRY seq={state.seq} bar={self.bars_seen} close={close} "
                f"high240={top} rsi={rsi:.1f} notional={notional} "
                f"fallback={self.fallback_on}"
            )
            orders.append(self.buy_order(state, notional, cid))
        return orders


BOT = None


def on_hello(state):
    global BOT
    BOT = SwingBot(state)
    _log(
        f"[dip] hello symbol={BOT.symbol} equity={state.equity} "
        f"warmup={state.warmup_bars}"
    )


def on_batch(state, batch):
    return BOT.decide(state, batch)


def on_done(state, done):
    _log(
        f"[dip] DONE equity={state.equity} cash={state.cash} "
        f"trips={BOT.round_trips} W{BOT.wins} L{BOT.losses} "
        f"buys={BOT.filled_buys} fallback={BOT.fallback_on}"
    )


makefun_bars.run(on_batch, on_hello=on_hello, on_done=on_done)
