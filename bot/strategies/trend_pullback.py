import pandas as pd
import logging

logger = logging.getLogger(__name__)


class TrendPullbackStrategy:
    def __init__(self, config: dict):
        self.config = config.get("strategies", {}).get("trend_pullback", {})
        self.ema_fast = self.config.get("ema_fast", 20)
        self.ema_slow = self.config.get("ema_slow", 50)
        self.rsi_min = self.config.get("rsi_min", 40)
        self.rsi_max = self.config.get("rsi_max", 60)
        # Max allowed SL distance as percentage of entry price (e.g., 5% = 0.05)
        self.max_sl_percent = self.config.get("max_sl_percent", 0.05)

    def generate_signal(self, df: pd.DataFrame, regime: str) -> dict:
        """
        Generates a trading signal based on Trend Pullback logic.
        Requires:
        1. Previous candle dipped below EMA20 (actual pullback)
        2. Current candle closed back above EMA20 (bounce confirmation)
        3. RSI in valid range (not overbought/oversold)
        """
        signal = {"side": "NONE", "reason": "No Signal"}

        if df.empty or len(df) < 3 or "TREND" not in regime:
            return signal

        current = df.iloc[-1]
        prev = df.iloc[-2]
        prev2 = df.iloc[-3]

        close = current['close']
        ema20 = current['EMA_20']
        rsi = current.get('RSI_14', 50)

        # Long Logic (TREND_BULL)
        if "BULL" in regime:
            # Conditions for a valid pullback entry:
            # 1. Previous candle touched/crossed below EMA20 (real pullback)
            # 2. Current candle closed above EMA20 (bounce)
            # 3. RSI not overbought (< 70)
            # 4. Price was above EMA20 before the pullback

            pullback_occurred = prev['low'] < ema20  # Prev candle dipped below EMA20
            bounce_confirmed = close > ema20 and current['low'] < ema20 * 1.002  # Close above, low near EMA
            rsi_valid = rsi < 70
            was_above = prev2['close'] > df.iloc[-3]['EMA_20']  # Was trending above before

            if pullback_occurred and bounce_confirmed and rsi_valid and was_above:
                stop_loss = min(current['low'], prev['low']) * 0.995
                risk = close - stop_loss

                # Sanity check: SL distance should not exceed max_sl_percent of entry
                sl_distance_pct = risk / close
                if sl_distance_pct > self.max_sl_percent:
                    logger.warning(f"Rejecting LONG signal: SL distance {sl_distance_pct:.2%} exceeds max {self.max_sl_percent:.2%}")
                    return signal

                signal = {
                    "side": "BUY",
                    "entry_price": close,
                    "stop_loss": stop_loss,
                    "take_profit": close + risk * 2,  # RR 1:2
                    "reason": "Trend Pullback Long: EMA20 bounce confirmed"
                }
                logger.debug(f"Long signal: pullback={pullback_occurred}, bounce={bounce_confirmed}, RSI={rsi:.1f}")

        # Short Logic (TREND_BEAR)
        elif "BEAR" in regime:
            pullback_occurred = prev['high'] > ema20
            bounce_confirmed = close < ema20 and current['high'] > ema20 * 0.998
            rsi_valid = rsi > 30
            was_below = prev2['close'] < df.iloc[-3]['EMA_20']

            if pullback_occurred and bounce_confirmed and rsi_valid and was_below:
                stop_loss = max(current['high'], prev['high']) * 1.005
                risk = stop_loss - close

                # Sanity check: SL distance should not exceed max_sl_percent of entry
                sl_distance_pct = risk / close
                if sl_distance_pct > self.max_sl_percent:
                    logger.warning(f"Rejecting SHORT signal: SL distance {sl_distance_pct:.2%} exceeds max {self.max_sl_percent:.2%}")
                    return signal

                signal = {
                    "side": "SELL",
                    "entry_price": close,
                    "stop_loss": stop_loss,
                    "take_profit": close - risk * 2,
                    "reason": "Trend Pullback Short: EMA20 rejection confirmed"
                }

        return signal
