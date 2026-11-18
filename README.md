# BTC race bot — slow trend-follower on cbBTC 1-minute bars

A bot for the make.fun bounty **btc-race-24h-20261118-1444** ("Race a BTC
trading bot live for 24 hours"). It trades Coinbase Wrapped BTC (CBBTC)
against USDC, one 1-minute bar at a time, starting from $1,000 of paper
money, long only, no leverage. It speaks the `agent-fun/bars-replay/v1`
protocol: the harness sends a `hello` line, then one `batch` per minute
(each batch carries the bar that just closed, the fills of the orders sent
on the previous batch, and the account), then `done`. The bot answers every
batch with exactly one reply echoing its `seq`, carrying its orders.

## Strategy

Only hold CBBTC while a slow uptrend is confirmed, ride it with a trailing
stop, and otherwise sit in cash:

- **Trend:** a 30-bar EMA of the 1-minute closes must sit at least 0.2%
  above a 120-bar EMA.
- **Buy:** on the bar the fast EMA crosses up through that band, or on a
  dip (close at least 0.2% under the fast EMA) while the trend is
  confirmed. One order, full cash.
- **Sell:** when the fast EMA falls back under the slow EMA, when the close
  has run 1.2% above the entry price, or when the close gives back 1.0%
  from the highest close seen since entry (trailing stop). One order, the
  whole position.

Every order is decided on one batch and fills at the next bar's open, so
the bot never chases a bar it has already seen. Orders carry a
`max_slippage_bps` of 150 against the latest close.

## Why this shape

The dev window is a grinding downtrend (BTC −2.8% over 24h) with one real
recovery leg in it. Fast crossovers (EMA 8/21 and the like) fire dozens of
times on 1-minute noise and bleed about 26 bps per round trip in fees,
slippage and price impact — the EMA-cross baseline loses $148 on the dev
window, buy-and-hold $32, momentum $24, RSI/Bollinger $40, ADX-momentum
$76. A slow pair with a confirmation band fires once, rides the recovery,
and lets the trailing stop bank the move near the top. Mean-reversion
entries were tested too and lost on this window: dips kept dipping.

Local grade (faithful replay of the kit's harness fill math, since
`grade-local --driver process` only runs Node bots and this VM has no
Docker): **net PnL +$9.83, 2 trades, fees $0.80, max drawdown 0.64%**,
deterministic across two runs, no rejected orders. The single trade buys
at $64,377.80 (bar 400) and sells at $65,062.46 (bar 508). Neighbouring
parameter choices (EMA 35/140, 40/120, 30/150; take-profit 1.0–1.2%) all
stay profitable on the dev window, so the result is not a knife-edge fit
to one wiggle — though, as ever with one 24-hour window, the race window
is a different day and nothing here guarantees it repeats.

## Files

- `main.py` — the bot (Python 3.13, standard library only). The only file
  the harness runs.
- `vendor/makefun_bars.py` — the kit's Python SDK shim, copied byte for
  byte from `sdk/makefun_bars.py`. It owns the wire: one JSON line at a
  time from stdin, exactly one reply per batch on stdout.
- `LICENSE` — MIT.

## Determinism

No clock, no network, no randomness, no files outside the repository. The
EMAs are plain float arithmetic (the same IEEE754 result on every run) and
the signal comparisons are ratios of the raw integer prices the bars
carry, so two runs on the same bars produce identical orders.

## Grade it

From the kit directory (needs Docker for a Python bot; without Docker, run
the bot through a local replay of the same protocol and fill math):

```
node cli/makefun.mjs grade-local --kit . --bot <this repo> --driver docker
```
