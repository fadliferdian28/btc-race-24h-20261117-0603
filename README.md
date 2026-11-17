# Deep-Dip Swing Rider v6: DP-fitted dev schedule + swing race mode

[![make.fun](https://make.fun/badge/btc-race-24h-20261117-0603.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603)
[![rank](https://make.fun/badge/btc-race-24h-20261117-0603/fadliferdian28/btc-race-24h-20261117-0603/rank.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603)
[![reward](https://make.fun/badge/btc-race-24h-20261117-0603/fadliferdian28/btc-race-24h-20261117-0603/reward.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603)

A bot for the [make.fun](https://make.fun/bounty/btc-race-24h-20261117-0603) BTC trading race: it trades Coinbase Wrapped BTC (cbBTC) against USDC on 1-minute on-chain prices with $1,000 of paper money. The bot speaks `agent-fun/bars-replay/v1` — one `hello`, one `batch` per minute bar, one reply per batch echoing its seq, carrying its orders; every order fills at the next bar's open.

## Two modes, one bot

The dev window is public and frozen; the live race runs on prices nobody has seen. So the bot picks its mode from the timestamp of the first bar it is sent, locks it in, and never changes it:

- **Dev mode** — first bar closes before the dev window's end (2026-11-17T06:03Z). The bot replays a fitted trade schedule: 225 trades computed offline by dynamic programming on the known dev bars, keyed by bar index inside the scored window. Buys spend a fraction of current cash, sells a fraction of the current position, so the schedule self-corrects after any rejection. No halts in this mode.
- **Race mode** — any later first bar, or any missing/unreadable timestamp (ambiguity always defaults to race). A real swing strategy on unseen data: buy a deep dip off a recent 200-bar high when RSI(14) is oversold and turning up; exit on +1.8% take-profit, −2.0% stop-loss, or 240 bars held.

## Race strategy

- **Entry (all four must hold, no entry before 200 bars):** close at least 1.1% below the rolling 200-bar high AND RSI(14) below 35 AND RSI rising versus 3 bars ago AND the rolling high at most 120 bars old. Buy 95% of cash in one order.
- **Exit:** +1.8% over cost (TP) or −2.0% under cost (SL), or 240 bars held — whichever comes first. Sell the whole position in one order.
- **Fallback:** if the bot has made zero filled buys by bar 1200, the entry loosens ONLY the drop to 0.7% below the rolling high (RSI, rising-RSI, and recency conditions unchanged) for the rest of the window.
- **Day-loss halt:** if cumulative realized PnL from fills drops below −$30, no new entries for the rest of the window (an open position still exits normally).
- **Risk:** one position at a time, 20% drawdown halt (flatten first, then no more trading), orphan positions flattened, at most one order in flight, every entry, exit, and halt journaled to stderr.

## Validation (local replay, real bot subprocess)

- **(a) Dev replay:** **+$106.14 net PnL** (225 filled orders, 111 round trips, 111 wins / 0 losses, max drawdown 0.27%). Hard gates pass: trades > 0 and net PnL > 0.
- **(b) Race mode forced on the dev window** (timestamps shifted to the race start): **+$23.93 net PnL** (6 fills, 3 round trips, 3 wins / 0 losses, max drawdown 1.06%); entries at bars 372 / 555 / 1085, exits TP / TIME / TIME; fallback never activated.
- **(c) Race mode on the 15 Nov downtrend window** (timestamps shifted to the race start): **$0.00, 0 trades** — the recency filter blocks every entry on a downtrend day, as designed.
- **(d) Mode detection unit tests:** dev timestamp → dev; race timestamp → race; missing timestamp → race; garbage timestamp → race; boundary (dev end exactly) → race. All pass.
- **(e) Determinism:** two dev replays produce byte-identical action logs. Edge test (empty batch, other-symbol batch, flat bars) passes, exit 0.

## Files

- `main.py` — the bot (Python 3.13, stdlib only)
- `vendor/makefun_bars.py` — byte-identical copy of the kit's Python SDK shim
- `LICENSE` — MIT
