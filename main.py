#!/usr/bin/env python3
"""Deep-Dip Swing Rider v2 for the make.fun BTC race (cbBTC/USDC, 1-minute bars).

The idea: on a flat, choppy day the money is in the few real dips, not in
holding. This bot watches the highest high of the last 200 bars and Wilder's
RSI(14) on closes. It buys only when four things line up at once: the close
sits at least 1.1% below that rolling high, RSI(14) is under 35, RSI is
rising versus 3 bars ago (wait for the bounce, don't catch a falling knife),
and the rolling high is at most 120 bars old (the dip must be a pullback
from a recent high, not one more leg down in an established downtrend).
Then it buys 95% of cash in one order and exits on the first of: +1.8%
over cost, -2.0% under cost, or 240 bars held. No entry is taken before
bar 200, so the rolling high always spans a full window.

Two backstops keep a bad day from doing real damage. If the bot has still
made no filled buy by bar 1200, the entry loosens ONLY the drop to 0.7%
below the rolling high (the RSI, rising-RSI, and recency conditions stay
exactly as they are, so a downtrend day still blocks entries); that keeps
the zero-trade failure of the previous race's bot from repeating. And if
cumulative realized PnL from fills ever drops below -$30, the bot takes no
new entries for the rest of the window, letting any open position exit
normally. A 20% equity drawdown halts all new trading outright.

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
LOOKBACK_BARS = 200        # rolling high window (bars)
RSI_BARS = 14              # Wilder's RSI length
RSI_CONFIRM_LAG = 3        # entry: RSI must be rising vs this many bars ago
ENTRY_DROP_NUM = 11        # entry: close >= 1.1% below the rolling high
ENTRY_DROP_DEN = 1000
FALLBACK_DROP_NUM = 7      # fallback entry: close >= 0.7% below the high
FALLBACK_DROP_DEN = 1000
FALLBACK_BAR = 1200        # loosen the drop if no filled buy by this bar
RSI_ENTRY_BELOW = 35.0     # entry: RSI(14) below 35 (oversold)
HIGH_MAX_AGE_BARS = 120    # entry: the rolling high at most 120 bars old
TP_NUM = 1018              # take-profit: +1.8% over cost
TP_DEN = 1000
SL_NUM = 98                # stop-loss: -2.0% under cost
SL_DEN = 100
MAX_HOLD_BARS = 240        # time exit: sell after 240 bars held
SIZE_NUM = 95              # buy 95% of cash per trade
SIZE_DEN = 100
MAX_SLIPPAGE_BPS = 100     # 1% max slippage from the latest close
DD_HALT_NUM = 20           # halt all new trading past 20% drawdown
DD_HALT_DEN = 100
DAY_LOSS_LIMIT = -30_000_000  # -$30 realized PnL: no new entries after this
MIN_NOTIONAL = 1_000_000   # $1 in quote units (6 decimals)


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


class DipRider:
    """One bot's state: indicators, the open position, and the risk guards."""

    def __init__(self, state):
        syms = state.hello.get("symbols", [])
        self.symbol = syms[0]["symbol"] if syms else "CBBTC"
        self.highs = deque(maxlen=LOOKBACK_BARS)  # (bar index, high) pairs
        self.last_close = None
        self.rsi_seed = []          # first RSI_BARS closes' changes
        self.rsi_gain = None        # Wilder smoothed average gain
        self.rsi_loss = None        # Wilder smoothed average loss
        self.rsi_hist = deque(maxlen=RSI_CONFIRM_LAG + 1)  # last 4 RSI values
        self.bars_seen = 0          # bars of our symbol processed
        self.entry = None           # {"bar": idx, "price": fill price}
        self.pending_buy = None     # client_id of a buy awaiting its fill
        self.pending_sell = None    # {"client_id", "cost", "qty"} of a sell in flight
        self.filled_buys = 0
        self.fallback_on = False
        self.realized_pnl = 0       # cumulative realized PnL from sell fills
        self.day_loss_halted = False
        self.peak_equity = state.equity
        self.halted = False
        self.round_trips = 0
        self.wins = 0
        self.losses = 0

    # -- indicators -----------------------------------------------------

    def rolling_high(self):
        """Highest high of the last LOOKBACK_BARS bars, plus its age.

        Returns (high, age): age is how many bars have passed since the bar
        that set the high, so 0 when this bar's own high is the rolling
        high. On ties the earliest bar wins, matching the first-occurrence
        convention of a rolling max. (0, 0) before the first bar.
        """
        if not self.highs:
            return 0, 0
        bar_idx, top = max(self.highs, key=lambda pair: pair[1])
        return top, self.bars_seen - bar_idx

    def update_rsi(self, close):
        """Feed one close into Wilder's RSI(14); returns RSI or None until warm."""
        if self.last_close is None:
            self.last_close = close
            return None
        change = close - self.last_close
        self.last_close = close
        if self.rsi_gain is None:
            self.rsi_seed.append(change)
            if len(self.rsi_seed) < RSI_BARS:
                return None
            gains = sum(c for c in self.rsi_seed if c > 0)
            losses = sum(-c for c in self.rsi_seed if c < 0)
            self.rsi_gain = gains / RSI_BARS
            self.rsi_loss = losses / RSI_BARS
            return None
        up = change if change > 0 else 0
        down = -change if change < 0 else 0
        self.rsi_gain = (self.rsi_gain * (RSI_BARS - 1) + up) / RSI_BARS
        self.rsi_loss = (self.rsi_loss * (RSI_BARS - 1) + down) / RSI_BARS
        total = self.rsi_gain + self.rsi_loss
        return 50.0 if total == 0 else 100.0 * self.rsi_gain / total

    def rsi_three_bars_ago(self):
        """The RSI value RSI_CONFIRM_LAG bars ago, or None if not yet known."""
        if len(self.rsi_hist) < RSI_CONFIRM_LAG + 1:
            return None
        return self.rsi_hist[0]

    def rsi_rising(self, rsi):
        """True when RSI is defined now and RSI_CONFIRM_LAG bars ago and has risen.

        The history holds the last RSI_CONFIRM_LAG + 1 values with the
        current one last, so its first element is the value RSI_CONFIRM_LAG
        bars ago. Both must be defined: during warm-up there is no bounce
        to confirm, so the entry stays blocked.
        """
        if rsi is None:
            return False
        old = self.rsi_three_bars_ago()
        return old is not None and rsi > old

    # -- order builders -------------------------------------------------

    def buy_order(self, notional, client_id):
        return {
            "side": "buy",
            "client_id": client_id,
            "symbol": self.symbol,
            "notional_quote_units": int(notional),
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }

    def sell_order(self, qty, client_id):
        return {
            "side": "sell",
            "client_id": client_id,
            "symbol": self.symbol,
            "qty": int(qty),
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }

    # -- fills and position tracking ------------------------------------

    def settle_fills(self, state, batch):
        """Match this batch's fills against any buy or sell still in flight.

        A fill arrives one batch after the order was sent: the order decided
        on the previous batch's bar fills at this bar's open, and this batch
        reports it. A filled buy becomes the tracked entry; a filled sell
        books its realized PnL against the cost recorded when the sell was
        sent (that running total drives the day-loss halt). A rejected order
        clears its pending slot so the bot can try again on the next batch.
        """
        if self.pending_buy is None and self.pending_sell is None:
            return
        for fill in batch.get("fills", []):
            cid = fill.get("client_id")
            if cid is not None and cid == self.pending_buy:
                if fill.get("status") == "filled":
                    self.filled_buys += 1
                    # The fill price is journal-only (exits use the
                    # account's cost basis); some harnesses report it as 0,
                    # so fall back to the latest close rather than logging
                    # a bogus 0.
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
                        f"[dip2] FILLED seq={state.seq} price={self.entry['price']} "
                        f"qty={qty} buys={self.filled_buys}"
                    )
                else:
                    _log(
                        f"[dip2] BUY-REJECTED seq={state.seq} "
                        f"reason={fill.get('reason')}"
                    )
                self.pending_buy = None
            elif (
                self.pending_sell is not None
                and cid == self.pending_sell["client_id"]
            ):
                if fill.get("status") == "filled":
                    proceeds = int(fill.get("notional_quote_units", "0")) - int(
                        fill.get("fee_quote_units", "0")
                    )
                    sold_qty = int(fill.get("qty", "0"))
                    sent = self.pending_sell
                    cost_part = (
                        sent["cost"] * sold_qty // sent["qty"] if sent["qty"] else 0
                    )
                    pnl = proceeds - cost_part
                    self.realized_pnl += pnl
                    # Win/loss is tallied here, on the realized fill, not at
                    # the exit decision: the exit-time mark is only an
                    # estimate, since the sell fills at the next bar's open
                    # and can land on either side of break-even.
                    if pnl > 0:
                        self.wins += 1
                    else:
                        self.losses += 1
                    _log(
                        f"[dip2] SELL-FILLED seq={state.seq} pnl_units={pnl} "
                        f"realized={self.realized_pnl} "
                        f"W{self.wins} L{self.losses}"
                    )
                else:
                    _log(
                        f"[dip2] SELL-REJECTED seq={state.seq} "
                        f"reason={fill.get('reason')}"
                    )
                self.pending_sell = None

    def exit_position(self, state, reason, price):
        """Sell the whole position in one order; journal the round trip.

        The tracked entry is cleared when the sell is sent, and the sell's
        cost basis is recorded so its fill can book realized PnL. If the
        sell is rejected, the next batch sees a position with no tracked
        entry and no sell in flight, and the orphan rule flattens it again,
        so a rejected exit never strands the position.
        """
        held = state.positions.get(self.symbol)
        if held is None or held.qty <= 0:
            self.entry = None
            return None
        if self.entry is not None:
            value = state.value(self.symbol, held.qty, price)
            pnl = value - held.cost
            self.round_trips += 1
            # est_pnl_units is the mark at this bar's close; the win/loss
            # tally lands later, on the sell fill's realized PnL.
            _log(
                f"[dip2] EXIT {reason} seq={state.seq} price={price} "
                f"entry={self.entry['price']} est_pnl_units={pnl} "
                f"trips={self.round_trips}"
            )
        cid = state.client_id()
        self.pending_sell = {"client_id": cid, "cost": held.cost, "qty": held.qty}
        self.entry = None
        return self.sell_order(held.qty, cid)

    # -- the per-batch decision -----------------------------------------

    def decide(self, state, batch):
        """Decide the orders for one batch. Never raises: every batch gets
        exactly one reply, and a crash here would fail the whole run."""
        orders = []
        try:
            orders = self._decide(state, batch)
        except Exception as exc:  # noqa: BLE001 - a reply must go out
            _log(f"[dip2] ERROR seq={state.seq}: {exc!r}")
        return orders

    def _decide(self, state, batch):
        orders = []

        # Drawdown guard first: track the equity peak, and if equity has
        # fallen more than 20% from it, flatten any open position and halt
        # all new trading for the rest of the window. Never send a second
        # sell while one is already in flight.
        if state.equity > self.peak_equity:
            self.peak_equity = state.equity
        if (
            self.peak_equity > 0
            and (self.peak_equity - state.equity) * DD_HALT_DEN
            > self.peak_equity * DD_HALT_NUM
        ):
            if not self.halted:
                _log(f"[dip2] HALT drawdown over 20% seq={state.seq}")
                self.halted = True
            if self.pending_sell is None:
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
        self.highs.append((self.bars_seen, high))
        rsi = self.update_rsi(close)
        self.rsi_hist.append(rsi)

        # Warm-up batches build history only: orders decided on them are
        # rejected by the harness, so never send any.
        if state.warming_up or self.halted:
            return orders

        # Manage the open position: take-profit at +1.8% over cost,
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

        # Orphan position (held, but no tracked entry and no order in
        # flight): flatten it rather than leaving an unmanaged position
        # open.
        held = state.positions.get(self.symbol)
        if (
            held is not None
            and held.qty > 0
            and self.pending_buy is None
            and self.pending_sell is None
        ):
            _log(f"[dip2] ORPHAN seq={state.seq}: flattening")
            cid = state.client_id()
            self.pending_sell = {"client_id": cid, "cost": held.cost, "qty": held.qty}
            orders.append(self.sell_order(held.qty, cid))
            return orders
        if self.pending_buy is not None or self.pending_sell is not None:
            return orders  # an order is already in flight; wait for its fill

        # Day-loss halt: once realized PnL from fills is below -$30, take
        # no new entries for the rest of the window. An open position still
        # exits normally (handled above); this gate only blocks entries.
        if self.realized_pnl < DAY_LOSS_LIMIT:
            if not self.day_loss_halted:
                _log(
                    f"[dip2] DAY-LOSS-HALT seq={state.seq} "
                    f"realized={self.realized_pnl}"
                )
                self.day_loss_halted = True
            return orders

        # Entry: four conditions, all required. The close must sit at least
        # 1.1% below the rolling 200-bar high, RSI(14) must be under 35,
        # RSI must be rising versus 3 bars ago, and the rolling high must
        # be at most 120 bars old. The rolling high is only meaningful once
        # the window is full, so no entry is taken before bar 200. The
        # fallback loosens ONLY the drop to 0.7% once bar 1200 passes with
        # no filled buy, so the bot cannot end a window with zero trades
        # the way the last race's bot did; on a downtrend day the recency
        # filter still blocks entries, so the fallback never forces a trade
        # into a falling market.
        if self.bars_seen < LOOKBACK_BARS:
            return orders
        if not self.fallback_on and self.bars_seen >= FALLBACK_BAR and self.filled_buys == 0:
            self.fallback_on = True
            _log(f"[dip2] FALLBACK-ACTIVATED seq={state.seq} bars={self.bars_seen}")
        drop_num = FALLBACK_DROP_NUM if self.fallback_on else ENTRY_DROP_NUM
        drop_den = FALLBACK_DROP_DEN if self.fallback_on else ENTRY_DROP_DEN
        top, top_age = self.rolling_high()
        deep_enough = top > 0 and (top - close) * drop_den >= top * drop_num
        recent_high = top_age <= HIGH_MAX_AGE_BARS
        if (
            deep_enough
            and recent_high
            and rsi is not None
            and rsi < RSI_ENTRY_BELOW
            and self.rsi_rising(rsi)
        ):
            notional = min(state.cash * SIZE_NUM // SIZE_DEN, state.weight_room(self.symbol))
            if notional < MIN_NOTIONAL:
                return orders
            cid = state.client_id()
            self.pending_buy = cid
            _log(
                f"[dip2] ENTRY seq={state.seq} bar={self.bars_seen} close={close} "
                f"high200={top} age={top_age} rsi={rsi:.1f} "
                f"rsi_3ago={self.rsi_three_bars_ago():.1f} notional={notional} "
                f"fallback={self.fallback_on}"
            )
            orders.append(self.buy_order(notional, cid))
        return orders


BOT = None


def on_hello(state):
    global BOT
    BOT = DipRider(state)
    _log(
        f"[dip2] hello symbol={BOT.symbol} equity={state.equity} "
        f"warmup={state.warmup_bars}"
    )


def on_batch(state, batch):
    return BOT.decide(state, batch)


def on_done(state, done):
    _log(
        f"[dip2] DONE equity={state.equity} cash={state.cash} "
        f"trips={BOT.round_trips} W{BOT.wins} L{BOT.losses} "
        f"buys={BOT.filled_buys} fallback={BOT.fallback_on} "
        f"realized={BOT.realized_pnl} day_loss_halted={BOT.day_loss_halted}"
    )


makefun_bars.run(on_batch, on_hello=on_hello, on_done=on_done)
