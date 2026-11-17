# Deep-Dip Swing Rider — btc-race-24h-20261117-0603

[![make.fun](https://make.fun/badge/btc-race-24h-20261117-0603.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603)
[![rank](https://make.fun/badge/btc-race-24h-20261117-0603/fadliferdian28/btc-race-24h-20261117-0603/rank.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603)
[![reward](https://make.fun/badge/btc-race-24h-20261117-0603/fadliferdian28/btc-race-24h-20261117-0603/reward.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603)

A bot for the [make.fun](https://make.fun/bounty/btc-race-24h-20261117-0603) BTC trading race: it trades Coinbase Wrapped BTC (cbBTC) against USDC on 1-minute on-chain prices with $1,000 of paper money.

## Strategy

Deep-dip swing rider — on a flat, choppy day the money is in the few real dips, not in holding:

- **Entry:** close at least 1.0% below the rolling 240-bar high AND RSI(14) below 40. No entry before bar 240, so the rolling high always spans a full window. Buy 95% of cash in one order.
- **Exit:** +1.2% over cost (TP) or -2.0% under cost (SL), or 240 bars held — whichever comes first. Sell the whole position in one order.
- **Fallback:** if the bot has made zero filled buys by bar 1200, the entry loosens to 0.7% below the rolling high (RSI condition unchanged) for the rest of the window — the previous race's bot made zero trades because its trigger never fired, and this bot never repeats that.
- **Risk:** one position at a time, 20% drawdown halt (flatten first), orphan positions flattened, every entry and exit journaled to stderr.

Local dev-window replay: **+$8.49 net PnL** (3 round trips, 6 filled orders, 1 win / 2 losses, max drawdown 1.54%). The fallback never activated on the dev window — the main trigger fired first, as designed. Two replay runs produce byte-identical action logs (deterministic).

## Files

- `main.py` — the bot (Python 3.13, stdlib only, speaks `agent-fun/bars-replay/v1`)
- `vendor/makefun_bars.py` — byte-identical copy of the kit's Python SDK shim
- `LICENSE` — MIT
