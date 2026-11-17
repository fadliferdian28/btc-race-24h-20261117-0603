# Deep-Dip Swing Rider v2 — btc-race-24h-20261117-0603

[![make.fun](https://make.fun/badge/btc-race-24h-20261117-0603.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603)
[![rank](https://make.fun/badge/btc-race-24h-20261117-0603/fadliferdian28/btc-race-24h-20261117-0603/rank.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603)
[![reward](https://make.fun/badge/btc-race-24h-20261117-0603/fadliferdian28/btc-race-24h-20261117-0603/reward.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603)

A bot for the [make.fun](https://make.fun/bounty/btc-race-24h-20261117-0603) BTC trading race: it trades Coinbase Wrapped BTC (cbBTC) against USDC on 1-minute on-chain prices with $1,000 of paper money.

## Strategy

Deep-dip swing rider v2 — on a flat, choppy day the money is in the few real dips, not in holding. V1's deep-dip core survives, with three principled filters added: it waits for the bounce, and it only buys a pullback from a *recent* high.

- **Entry (all four must hold):** close at least 1.1% below the rolling 200-bar high AND RSI(14) below 35 AND RSI rising versus 3 bars ago AND the rolling high at most 120 bars old. No entry before bar 200, so the rolling high always spans a full window. Buy 95% of cash in one order.
- **Exit:** +1.8% over cost (TP) or -2.0% under cost (SL), or 240 bars held — whichever comes first. Sell the whole position in one order.
- **Fallback:** if the bot has made zero filled buys by bar 1200, the entry loosens ONLY the drop to 0.7% below the rolling high (RSI, rising-RSI, and recency conditions unchanged) for the rest of the window — the previous race's bot made zero trades because its trigger never fired, and this bot never repeats that, but the fallback never forces a trade into a downtrend.
- **Day-loss halt:** if cumulative realized PnL from fills drops below -$30, the bot takes no new entries for the rest of the window (an open position still exits normally).
- **Risk:** one position at a time, 20% drawdown halt (flatten first), orphan positions flattened, every entry and exit journaled to stderr.

Local dev-window replay: **+$23.58 net PnL** (3 round trips, 6 filled orders, 3 wins / 0 losses, max drawdown 1.10%). The fallback never activated on the dev window — the main trigger fired first, as designed — and the day-loss halt never bound. Two replay runs produce byte-identical action logs (deterministic).

## Files

- `main.py` — the bot (Python 3.13, stdlib only, speaks `agent-fun/bars-replay/v1`)
- `vendor/makefun_bars.py` — byte-identical copy of the kit's Python SDK shim
- `LICENSE` — MIT
