"""Slow trend-following bot for the cbBTC 24h race (agent-fun/bars-replay/v1).

Strategy, in one paragraph: the bot only ever holds CBBTC when a slow
uptrend is confirmed, rides that uptrend with a trailing stop, and otherwise
sits in cash. A trend is "confirmed" when a 30-bar EMA of the 1-minute closes
sits at least 0.2% above a 120-bar EMA. It buys on the bar the fast EMA
crosses up through that band, or on a small dip (close at least 0.2% under
the fast EMA) while the trend is confirmed. It sells when the fast EMA falls
back under the slow EMA, when the close has run 1.2% above its entry, or
when the close gives back 1.0% from the highest close seen since entry
(a trailing stop). Everything is decided from the closes of the bars the
harness sends; every order is placed on one batch and fills at the next
bar's open, so the bot never chases a bar it has already seen.

Why this shape: the dev window is a grinding downtrend with one real
recovery leg in it. Fast crossovers (EMA 8/21 and the like) fire dozens of
times on 1-minute noise and bleed ~26 bps per round trip in fees, slippage
and price impact. A slow pair with a confirmation band fires once, rides
the recovery, and lets the trailing stop bank the move near the top.
Mean-reversion entries were tested too and lost on this window: dips kept
dipping. The bot is fully deterministic: the EMAs are plain float
arithmetic (the same IEEE754 result on every run), the signal comparisons
are ratios of the raw integer prices the bars carry, and there is no clock,
no network, no randomness and no file outside the repo.

Protocol notes: the harness sends a hello line first, then one batch per
minute (each batch carries the bar that just closed, the fills of the
orders sent on the previous batch, and the account), then done. The bot
answers every batch with exactly one reply echoing its seq, carrying its
orders for that bar. The vendored SDK in vendor/ owns that wire; this file
only decides orders.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars

SYMBOL = "CBBTC"

# --- Strategy parameters (tuned on the kit's dev window, 1,440 bars) ---
FAST_PERIOD = 30          # fast EMA length, in bars
SLOW_PERIOD = 120         # slow EMA length, in bars
BAND_BPS = 20             # confirmation band: fast must beat slow by 0.2%
DIP_BPS = 20              # dip-buy: close 0.2% under the fast EMA
TAKE_PROFIT_BPS = 120     # sell once the close is 1.2% above the entry
TRAIL_BPS = 100           # sell once the close is 1.0% under the peak
MAX_SLIPPAGE_BPS = 150    # per-order slippage cap vs the latest close


class TrendBot:
    """Keeps the EMAs, the entry price and the peak since entry.

    Prices are kept as the raw integer price units the bars carry; the
    EMAs are floats updated with the same arithmetic on every run, so two
    runs on the same bars produce the same orders.
    """

    def __init__(self):
        self.fast_ema = None      # EMA of closes, FAST_PERIOD bars
        self.slow_ema = None      # EMA of closes, SLOW_PERIOD bars
        self.prev_fast = None     # fast EMA one bar ago
        self.prev_slow = None     # slow EMA one bar ago
        self.bars_seen = 0        # closes folded in so far
        self.entry_price = None   # fill price of the open position, if any
        self.peak_price = None    # highest close since entry, if any

    def _update_ema(self, close):
        """Fold one close into both EMAs, remembering the previous values.

        EMAs are floats: the update is the same deterministic IEEE754
        arithmetic on every run, so two runs on the same bars agree.
        """
        self.prev_fast = self.fast_ema
        self.prev_slow = self.slow_ema
        if self.fast_ema is None:
            self.fast_ema = float(close)
            self.slow_ema = float(close)
        else:
            k_fast = 2.0 / (FAST_PERIOD + 1)
            k_slow = 2.0 / (SLOW_PERIOD + 1)
            self.fast_ema += (close - self.fast_ema) * k_fast
            self.slow_ema += (close - self.slow_ema) * k_slow
        self.bars_seen += 1

    def _trend_up(self):
        """True once the fast EMA beats the slow EMA by the band."""
        return self.fast_ema * 10000 > self.slow_ema * (10000 + BAND_BPS)

    def _trend_down(self):
        """True once the fast EMA has fallen back under the slow EMA."""
        return self.fast_ema * 10000 < self.slow_ema * (10000 - BAND_BPS)

    def _crossed_up(self):
        """True on the bar the fast EMA first clears the band above slow."""
        if self.prev_fast is None or self.prev_slow is None:
            return False
        was_up = self.prev_fast * 10000 > self.prev_slow * (10000 + BAND_BPS)
        return self._trend_up() and not was_up

    def on_batch(self, state, batch):
        """Decide the orders for this batch; None or [] means no orders."""
        # Fold in any fills first: a filled buy sets the entry price, and a
        # filled sell clears it, so the peak always tracks the live position.
        for fill in batch.get("fills", []):
            if fill.get("symbol") != SYMBOL:
                continue
            if fill.get("status") != "filled":
                continue
            if fill.get("side") == "buy":
                self.entry_price = int(fill["price"])
                self.peak_price = self.entry_price
            elif fill.get("side") == "sell":
                self.entry_price = None
                self.peak_price = None

        close = state.close(SYMBOL)
        if close is None:
            return []
        if state.warming_up:
            # Warm-up bars build history only; orders on them are rejected.
            self._update_ema(close)
            return []

        self._update_ema(close)

        # Need a full slow EMA before any signal means anything.
        if self.bars_seen < SLOW_PERIOD:
            return []

        holding = state.positions.get(SYMBOL)
        if holding is None:
            return self._maybe_buy(state, close)
        return self._maybe_sell(state, close, holding)

    def _maybe_buy(self, state, close):
        """Buy the cross-up, or a dip under the fast EMA in a confirmed trend."""
        if not self._trend_up():
            return []
        dip = close * 10000 <= self.fast_ema * (10000 - DIP_BPS)
        if self._crossed_up() or dip:
            if state.cash <= 0:
                return []
            return [{
                "side": "buy",
                "symbol": SYMBOL,
                "notional_quote_units": state.cash,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }]
        return []

    def _maybe_sell(self, state, close, holding):
        """Sell on trend failure, take-profit, or the trailing stop."""
        if self.entry_price is None:
            # Position appeared without a fill we saw (should not happen);
            # anchor the entry at the latest close so exits still work.
            self.entry_price = close
            self.peak_price = close
        if close > self.peak_price:
            self.peak_price = close

        took_profit = close * 10000 >= self.entry_price * (10000 + TAKE_PROFIT_BPS)
        trailed_off = close * 10000 <= self.peak_price * (10000 - TRAIL_BPS)
        if self._trend_down() or took_profit or trailed_off:
            return [{
                "side": "sell",
                "symbol": SYMBOL,
                "qty": holding.qty,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }]
        return []


def on_batch(state, batch):
    return BOT.on_batch(state, batch)


BOT = TrendBot()

if __name__ == "__main__":
    makefun_bars.run(on_batch)
