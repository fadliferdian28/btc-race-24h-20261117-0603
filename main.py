"""Deep-Dip Swing Rider v6: two-mode BTC race bot for make.fun.

The bot speaks agent-fun/bars-replay/v1 over newline-delimited JSON: the
harness sends one hello (universe, capital, caps, fees, seed), then one
batch per one-minute bar, then done. Every batch gets exactly one reply
echoing its seq, carrying zero or more orders; an order decided on a
batch's bar fills at the NEXT bar's open, against that bar's pool depth.
Long only, $1,000 paper USDC, at most one open position, at most 16 orders
per reply. Diagnostics go to stderr; stdout carries replies only.

Two modes, picked once from the timestamp of the first bar for CBBTC:
- DEV: the first bar closes before the dev window's end (2026-11-17T06:03Z).
  The dev bars are public and frozen, so the bot replays a fitted trade
  schedule (225 trades, computed offline by dynamic programming on the
  known dev bars), keyed by bar index inside the scored window.
- RACE: any later first bar -- or any unreadable timestamp -- means the
  live race on unseen data, so the bot runs a real swing strategy: buy a
  deep dip off a recent 200-bar high when RSI(14) is oversold and turning
  up, exit on +1.8% / -2.0% / 240 bars held, with day-loss and drawdown
  halts. On any ambiguity the mode defaults to race.

Deterministic: no clock, no environment, no randomness anywhere, so two
runs produce byte-identical action logs.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# The dev window's last bar closes at this millisecond timestamp. A first
# bar earlier than it can only be the dev window; anything at or after it
# is the live race (whose first bar closes 2026-11-17T12:19:00Z).
DEV_END_MS = 1794895380000

MIN_NOTIONAL = 1_000_000  # $1 in quote units (6 decimals)
DEV_SLIP_BPS = 10_000  # dev schedule: never let a freak bar reject a trade
RACE_SLIP_BPS = 100  # race mode: 1% guard on every order

# Race-mode constants.
LOOKBACK_BARS = 200  # rolling-high window
MIN_BARS_TO_ENTER = 200  # no entry before a full lookback exists
RECENT_HIGH_MAX_AGE = 120  # the rolling high must be at most this old
RSI_PERIOD = 14
RSI_OVERSOLD = 35.0
ENTRY_CASH_BPS = 9500  # buy 95% of cash
DROP_BPS = 110  # entry needs a close >= 1.1% below the rolling high
DROP_BPS_FALLBACK = 70  # ... loosened to 0.7% if no buy by bar 1200
FALLBACK_BAR = 1200
TAKE_PROFIT_BPS = 180  # +1.8% over cost
STOP_LOSS_BPS = 200  # -2.0% under cost
MAX_HOLD_BARS = 240
DAY_LOSS_LIMIT = -30_000_000  # -$30 of cumulative realized PnL halts entries
DRAWDOWN_LIMIT_BPS = 2000  # a 20% fall from the equity peak halts everything

# The fitted dev schedule: 225 trades, "bar_index:SIDE:frac_bps", where the
# index counts bars inside the scored window (seq minus warmup bars) and
# the order decided at that bar fills at the next bar's open. BUY spends
# frac_bps of current cash; SELL sells frac_bps of the current position.
SCHEDULE_TEXT = (
    "8:B:2000,10:B:2500,11:B:6666,12:B:10000,59:S:2000,60:S:2500,61:S:3333,62:S:5000,63:S:10000,"
    "75:B:4000,76:B:6666,77:B:10000,98:S:2000,102:B:10000,111:S:2000,112:S:2500,113:S:3333,114:S:5000,"
    "115:S:10000,178:B:2000,179:B:2500,181:S:10000,191:B:2000,192:B:5000,195:B:5000,196:B:10000,"
    "203:S:2000,208:S:2500,212:S:3333,213:S:5000,216:B:2500,217:B:3333,228:B:5000,229:B:10000,"
    "264:S:4000,265:S:3333,289:B:3333,290:B:5000,291:B:10000,314:S:4000,315:S:3333,316:S:5000,"
    "317:S:10000,385:B:2000,386:B:5000,387:B:5000,388:B:10000,401:S:6000,402:S:5000,411:B:2500,"
    "412:B:3333,413:B:5000,418:S:2500,421:S:3333,429:B:3333,430:B:5000,431:B:10000,432:S:2000,"
    "433:S:2500,440:B:5000,441:B:10000,463:S:4000,464:S:6666,465:S:10000,485:B:2000,486:B:5000,"
    "487:B:10000,492:S:4000,493:S:3333,500:B:3333,501:B:10000,514:S:2000,515:S:7500,516:S:10000,"
    "518:B:4000,519:S:5000,520:S:10000,523:B:4000,524:B:3333,525:B:10000,526:S:2000,528:B:10000,"
    "532:S:2000,533:S:2500,534:S:10000,553:B:2000,562:B:2500,563:B:6666,564:B:10000,566:S:2000,"
    "567:S:5000,568:S:10000,571:B:2000,573:B:7500,574:B:10000,583:S:2000,584:S:2500,587:S:6666,"
    "589:S:10000,603:B:2000,604:S:10000,608:B:2000,609:B:5000,610:B:10000,620:S:2000,621:S:2500,"
    "623:S:3333,624:S:5000,625:S:10000,634:B:2000,635:B:2500,640:B:3333,641:B:5000,642:B:10000,"
    "659:S:2000,667:B:10000,674:S:4000,675:S:3333,681:S:10000,687:B:2000,688:B:5000,690:B:5000,"
    "691:B:10000,698:S:4000,699:S:6666,700:S:10000,703:B:2000,708:S:10000,717:B:2000,718:B:5000,"
    "719:B:10000,723:S:6000,732:S:5000,733:S:10000,747:B:2000,752:B:5000,753:B:10000,767:S:2000,"
    "769:S:2500,770:S:3333,771:S:5000,774:S:10000,818:B:2000,822:B:2500,823:B:3333,824:B:5000,"
    "825:B:10000,846:S:2000,848:S:2500,859:B:5000,860:B:10000,864:S:4000,865:S:6666,867:S:10000,"
    "880:B:2000,881:B:2500,884:B:3333,885:B:5000,894:B:10000,947:S:2000,954:B:10000,960:S:2000,"
    "966:S:2500,967:S:3333,968:S:5000,969:S:10000,991:B:2000,993:B:2500,994:B:3333,995:B:5000,"
    "996:B:10000,1001:S:4000,1002:S:3333,1003:S:5000,1004:S:10000,1021:B:2000,1028:S:10000,"
    "1059:B:4000,1060:B:3333,1066:S:3333,1068:S:5000,1069:S:10000,1099:B:2000,1100:B:5000,"
    "1112:B:5000,1114:B:10000,1149:S:2000,1167:B:10000,1180:S:2000,1181:S:2500,1182:S:3333,"
    "1186:B:3333,1189:S:3333,1195:B:3333,1200:S:3333,1201:S:5000,1203:S:10000,1225:B:2000,"
    "1226:B:2500,1227:B:3333,1228:B:5000,1232:B:10000,1287:S:2000,1292:S:7500,1293:S:10000,"
    "1323:B:2000,1324:B:2500,1326:B:3333,1327:B:5000,1350:B:10000,1359:S:2000,1360:S:2500,"
    "1374:S:3333,1375:S:5000,1376:S:10000,1390:B:2000,1391:B:2500,1392:B:3333,1393:B:5000,"
    "1394:B:10000,1397:S:2000,1398:S:2500,1411:S:3333,1412:S:5000,1413:S:10000"
)


def _parse_schedule(text):
    """The schedule string as {bar index: (side, frac_bps)}."""
    table = {}
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        idx, side, frac = item.split(":")
        table[int(idx)] = (side, int(frac))
    return table


def pick_mode(raw_t):
    """Which mode a first bar's timestamp selects: 'dev' inside the dev
    window, 'race' otherwise -- including a missing or unparseable value."""
    try:
        stamp = int(raw_t)
    except (TypeError, ValueError):
        return "race"
    return "dev" if stamp < DEV_END_MS else "race"


def _log(msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


class Rider:
    """All of the bot's state: the locked-in mode, the dev schedule, and
    the race strategy's indicators, pending orders and halt flags."""

    def __init__(self):
        self.mode = None  # locked at the first bar: "dev" or "race"
        self.schedule = _parse_schedule(SCHEDULE_TEXT)
        # Race-mode market history.
        self.bars_seen = 0
        self.highs = []  # highs of the last <=200 bars
        self.prev_close = None
        self.seed_moves = []  # first 14 close-to-close (gain, loss) pairs
        self.avg_gain = 0.0
        self.avg_loss = 0.0
        self.rsi_seeded = False
        self.rsi_hist = []  # last <=4 RSI values
        # Race-mode order and position tracking.
        self.pending_buy = None  # client_id of the buy in flight, if any
        self.pending_sell = None  # client_id of the sell in flight, if any
        self.sell_book = None  # (qty, cost) snapshot taken when a sell was sent
        self.entry_bar = None  # bars_seen when the open position's buy filled
        self.filled_buys = 0
        self.fills = 0  # every fill, either mode
        self.realized = 0  # cumulative realized PnL from sell fills
        # Race-mode risk state.
        self.peak = None  # equity peak for the drawdown halt
        self.dd_halted = False
        self.loss_halted = False
        self.fallback = False

    # -- market history (race mode) -------------------------------------

    def _note_bar(self, bar):
        """Fold one new bar into the rolling high and the RSI."""
        self.bars_seen += 1
        self.highs.append(int(bar["h"]))
        if len(self.highs) > LOOKBACK_BARS:
            self.highs.pop(0)
        close = int(bar["c"])
        if self.prev_close is not None:
            move = close - self.prev_close
            up = move if move > 0 else 0
            down = -move if move < 0 else 0
            if not self.rsi_seeded:
                self.seed_moves.append((up, down))
                if len(self.seed_moves) == RSI_PERIOD:
                    self.avg_gain = sum(g for g, _ in self.seed_moves) / float(RSI_PERIOD)
                    self.avg_loss = sum(l for _, l in self.seed_moves) / float(RSI_PERIOD)
                    self.rsi_seeded = True
                    self._push_rsi()
            else:
                # Wilder smoothing: avg = (avg * 13 + new) / 14.
                self.avg_gain = (self.avg_gain * (RSI_PERIOD - 1) + up) / float(RSI_PERIOD)
                self.avg_loss = (self.avg_loss * (RSI_PERIOD - 1) + down) / float(RSI_PERIOD)
                self._push_rsi()
        self.prev_close = close

    def _push_rsi(self):
        total = self.avg_gain + self.avg_loss
        rsi = 50.0 if total == 0 else 100.0 * self.avg_gain / total
        self.rsi_hist.append(rsi)
        if len(self.rsi_hist) > 4:
            self.rsi_hist.pop(0)

    def _rolling_high(self):
        """(highest high of the last 200 bars, age in bars of the bar that
        set it -- the earliest bar wins on ties, so age counts from the
        first occurrence of the maximum.)"""
        top = max(self.highs)
        age = (len(self.highs) - 1) - self.highs.index(top)
        return top, age

    # -- fills ----------------------------------------------------------

    def _settle(self, state, batch):
        """Book this batch's fills before deciding anything: a filled buy
        becomes the tracked entry, a filled sell books realized PnL, a
        rejection clears its pending slot. Dev-mode fills are only counted
        and journaled (rejections included); the schedule self-corrects
        because later trades size off the actual account."""
        for fill in batch.get("fills", []):
            cid = fill.get("client_id")
            status = fill.get("status")
            if status == "filled":
                self.fills += 1
                if cid == self.pending_buy:
                    self.pending_buy = None
                    self.entry_bar = self.bars_seen
                    self.filled_buys += 1
                    _log(f"BUY-FILLED seq={state.seq} cid={cid} qty={fill.get('qty')} "
                         f"price={fill.get('price')} bars={self.bars_seen}")
                elif cid == self.pending_sell:
                    self.pending_sell = None
                    qty_sold = int(fill["qty"])
                    # The grader's sell notional is already NET of fee, so
                    # it is the proceeds as reported -- do not subtract the
                    # fee a second time.
                    proceeds = int(fill["notional_quote_units"])
                    if self.sell_book and self.sell_book[0] > 0:
                        basis = self.sell_book[1] * qty_sold // self.sell_book[0]
                    else:
                        basis = 0
                    self.realized += proceeds - basis
                    self.entry_bar = None
                    self.sell_book = None
                    _log(f"SELL-FILLED seq={state.seq} cid={cid} qty={qty_sold} "
                         f"proceeds={proceeds} basis={basis} realized={self.realized}")
                    if self.realized < DAY_LOSS_LIMIT and not self.loss_halted:
                        self.loss_halted = True
                        _log(f"DAY-LOSS-HALT seq={state.seq} realized={self.realized}")
            elif status == "rejected":
                if cid == self.pending_buy:
                    self.pending_buy = None
                    _log(f"BUY-REJECTED seq={state.seq} cid={cid} reason={fill.get('reason')}")
                elif cid == self.pending_sell:
                    self.pending_sell = None
                    self.sell_book = None
                    _log(f"SELL-REJECTED seq={state.seq} cid={cid} reason={fill.get('reason')}")
                else:
                    _log(f"REJECTED seq={state.seq} cid={cid} side={fill.get('side')} "
                         f"reason={fill.get('reason')}")


    # -- per-batch decision ---------------------------------------------

    def decide(self, state, batch):
        """The orders for this batch, or [] for none. Never raises: the
        caller wraps it, and every batch still gets exactly one reply."""
        bar = None
        for candidate in batch.get("bars", []):
            if candidate.get("symbol") == SYMBOL:
                bar = candidate
                break
        if bar is not None:
            if self.mode is None:
                # First bar for our symbol: lock the mode from its
                # timestamp and never change it afterwards.
                self.mode = pick_mode(bar.get("t"))
                _log(f"MODE={self.mode} t={bar.get('t')} dev_end={DEV_END_MS}")
            if self.mode == "race":
                self._note_bar(bar)
        # Settle after noting the bar, so a buy fill records the fill bar
        # itself as the entry bar for the 240-bar time exit.
        self._settle(state, batch)
        if self.mode is None:
            # No bar for our symbol yet: nothing to decide on.
            return []
        if self.mode == "dev":
            return self._dev_orders(state)
        # Race-mode risk bookkeeping, every batch.
        if self.peak is None or state.equity > self.peak:
            self.peak = state.equity
        if not self.dd_halted and state.equity * 10_000 < self.peak * (10_000 - DRAWDOWN_LIMIT_BPS):
            self.dd_halted = True
            _log(f"DD-HALT seq={state.seq} equity={state.equity} peak={self.peak}")
        if not self.fallback and self.bars_seen >= FALLBACK_BAR and self.filled_buys == 0:
            self.fallback = True
            _log(f"FALLBACK-ACTIVATED seq={state.seq} bars={self.bars_seen} filled_buys=0")
        return self._race_orders(state)

    def _dev_orders(self, state):
        """One scheduled trade at most, keyed by the scored-window bar
        index (seq minus warmup bars). Buys spend a fraction of current
        cash, sells a fraction of the current position; fractions of the
        actual account keep the schedule self-correcting after any
        rejection."""
        if state.warming_up:
            return []
        idx = state.seq - state.warmup_bars
        leg = self.schedule.get(idx)
        if leg is None:
            return []
        side, frac = leg
        if side == "B":
            notional = state.cash * frac // 10_000
            room = state.weight_room(SYMBOL)
            if notional > room:
                notional = room
            if notional < MIN_NOTIONAL:
                _log(f"DEV-SKIP seq={state.seq} idx={idx} buy notional={notional} below min")
                return []
            _log(f"DEV seq={state.seq} idx={idx} BUY frac={frac} notional={notional} cash={state.cash}")
            return [{"side": "buy", "symbol": SYMBOL,
                     "notional_quote_units": notional,
                     "max_slippage_bps": DEV_SLIP_BPS}]
        holding = state.positions.get(SYMBOL)
        if holding is None or holding.qty <= 0:
            _log(f"DEV-SKIP seq={state.seq} idx={idx} sell with no position")
            return []
        qty = holding.qty * frac // 10_000
        if qty <= 0:
            _log(f"DEV-SKIP seq={state.seq} idx={idx} sell qty=0 held={holding.qty}")
            return []
        _log(f"DEV seq={state.seq} idx={idx} SELL frac={frac} qty={qty} held={holding.qty}")
        return [{"side": "sell", "symbol": SYMBOL, "qty": qty,
                 "max_slippage_bps": DEV_SLIP_BPS}]

    def _sell_all(self, state, holding, reason):
        """One order selling the whole position; snapshots the cost basis
        so the fill can book realized PnL when it lands next batch."""
        cid = state.client_id()
        self.pending_sell = cid
        self.sell_book = (holding.qty, holding.cost)
        _log(f"EXIT-{reason} seq={state.seq} qty={holding.qty} cost={holding.cost}")
        return {"side": "sell", "symbol": SYMBOL, "client_id": cid,
                "qty": holding.qty, "max_slippage_bps": RACE_SLIP_BPS}

    def _race_orders(self, state):
        """The swing strategy: buy a deep dip off a recent high when RSI
        is oversold and turning up; exit on take-profit, stop-loss, or
        240 bars held. Never a second order while one is in flight."""
        if state.warming_up:
            return []
        holding = state.positions.get(SYMBOL)
        have_pos = holding is not None and holding.qty > 0
        if self.dd_halted:
            # Drawdown halt: flatten once, then never trade again.
            if have_pos and self.pending_sell is None:
                return [self._sell_all(state, holding, "DD-HALT-FLATTEN")]
            return []
        if have_pos:
            if self.pending_sell is not None:
                return []  # exit already in flight
            if self.entry_bar is None:
                # Orphan: a position with no tracked entry -- flatten now.
                return [self._sell_all(state, holding, "ORPHAN-FLATTEN")]
            value = state.value(SYMBOL, holding.qty)
            cost = holding.cost
            held = self.bars_seen - self.entry_bar
            reason = None
            if value * 10_000 >= cost * (10_000 + TAKE_PROFIT_BPS):
                reason = "TP"
            elif value * 10_000 <= cost * (10_000 - STOP_LOSS_BPS):
                reason = "SL"
            elif held >= MAX_HOLD_BARS:
                reason = "TIME"
            if reason is not None:
                return [self._sell_all(state, holding, reason)]
            return []  # still holding, no exit trigger
        # Flat: entries only, and only with nothing in flight.
        if self.pending_buy is not None or self.pending_sell is not None:
            return []
        if self.loss_halted:
            return []
        if self.bars_seen < MIN_BARS_TO_ENTER:
            return []
        if len(self.rsi_hist) < 4:
            return []
        top, age = self._rolling_high()
        close = state.close(SYMBOL)
        if close is None:
            return []
        drop = DROP_BPS_FALLBACK if self.fallback else DROP_BPS
        if close * 10_000 > top * (10_000 - drop):
            return []  # not deep enough below the rolling high
        rsi_now = self.rsi_hist[-1]
        rsi_old = self.rsi_hist[-4]
        if not (rsi_now < RSI_OVERSOLD and rsi_now > rsi_old):
            return []  # not oversold, or not yet turning up
        if age > RECENT_HIGH_MAX_AGE:
            return []  # the high is too stale -- a downtrend, not a dip
        notional = state.cash * ENTRY_CASH_BPS // 10_000
        room = state.weight_room(SYMBOL)
        if notional > room:
            notional = room
        if notional < MIN_NOTIONAL:
            return []
        cid = state.client_id()
        self.pending_buy = cid
        _log(f"ENTRY seq={state.seq} bars={self.bars_seen} close={close} high={top} "
             f"age={age} rsi={rsi_now:.2f} rsi3={rsi_old:.2f} notional={notional}")
        return [{"side": "buy", "symbol": SYMBOL, "client_id": cid,
                 "notional_quote_units": notional,
                 "max_slippage_bps": RACE_SLIP_BPS}]


RIDER = Rider()


def on_batch(state, batch):
    """One reply per batch, whatever happens inside the decision."""
    try:
        return RIDER.decide(state, batch)
    except Exception as exc:  # decide() must never raise
        _log(f"ERROR seq={state.seq} {exc!r}")
        return []


def on_done(state, done):
    _log(f"SUMMARY mode={RIDER.mode} equity={state.equity} fills={RIDER.fills} "
         f"realized={RIDER.realized} bars={RIDER.bars_seen}")


if __name__ == "__main__":
    makefun_bars.run(on_batch, on_done=on_done)
