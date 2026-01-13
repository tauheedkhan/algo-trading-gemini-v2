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

        # ATR-based stop parameters
        self.atr_mult = self.config.get("atr_mult", 1.5)
        self.min_sl_pct = self.config.get("min_sl_pct", 0.012)  # 1.2% minimum
        self.rr_ratio = self.config.get("rr_ratio", 2.0)

        # S/R filter parameters
        self.sr_lookback = self.config.get("sr_lookback", 20)  # Candles to look back for S/R
        self.sr_zone_pct = self.config.get("sr_zone_pct", 0.005)  # 0.5% zone around S/R levels

    def generate_signal(self, df: pd.DataFrame, regime_data) -> dict:
        """
        Generates a trading signal based on Trend Pullback logic.
        Requires:
        1. Previous candle dipped below EMA20 (actual pullback)
        2. Current candle closed back above EMA20 (bounce confirmation)
        3. RSI in valid range (not overbought/oversold)
        """
        signal = {"side": "NONE", "reason": "No Signal"}

        # Backward compatible: accept either regime string or regime_data dict
        if isinstance(regime_data, dict):
            regime = regime_data.get('regime', 'NO_TRADE')
            confidence = float(regime_data.get('confidence', 0.0))
        else:
            regime = str(regime_data)
            confidence = 0.0

        if df.empty or len(df) < 3 or "TREND" not in regime:
            return signal

        current = df.iloc[-1]
        prev = df.iloc[-2]
        prev2 = df.iloc[-3]

        close = current['close']
        ema20 = current['EMA_20']
        rsi = current.get('RSI_14', 50)
        atr = current.get('ATR_14', close * 0.02)  # Fallback to 2% if ATR not available

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

            # Debug logging
            logger.info(f"TREND_BULL check: pullback={pullback_occurred}, bounce={bounce_confirmed}, "
                       f"rsi_valid={rsi_valid} (RSI={rsi:.1f}), was_above={was_above}, "
                       f"close={close:.2f}, EMA20={ema20:.2f}, prev_low={prev['low']:.2f}")

            if pullback_occurred and bounce_confirmed and rsi_valid and was_above:
                # ATR-based SL calculation
                structure_sl = min(current['low'], prev['low'])
                buffer = self.atr_mult * atr
                min_dist = self.min_sl_pct * close

                # SL = structure - max(buffer, min_dist) for LONG
                stop_loss = structure_sl - max(buffer, min_dist)
                risk = close - stop_loss
                take_profit = close + risk * self.rr_ratio

                # Sanity check: SL distance should not exceed max_sl_percent of entry
                sl_distance_pct = risk / close
                if sl_distance_pct > self.max_sl_percent:
                    logger.warning(f"Rejecting LONG signal: SL distance {sl_distance_pct:.2%} exceeds max {self.max_sl_percent:.2%}")
                    return signal

                # S/R filter: Check if TP is blocked by resistance
                resistance_levels = self._find_resistance_levels(df)
                if self._tp_blocked_by_sr(close, take_profit, resistance_levels, is_long=True):
                    logger.warning(f"Rejecting LONG signal: TP ${take_profit:.2f} blocked by resistance")
                    return signal

                signal = {
                    "side": "BUY",
                    "entry_price": close,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "reason": "Trend Pullback Long: EMA20 bounce confirmed",
                    "regime": regime,
                    "confidence": confidence,
                    "atr": atr
                }
                logger.info(f"Long signal: SL=${stop_loss:.4f} (ATR={atr:.4f}, buffer={buffer:.4f}), TP=${take_profit:.4f}, RR=1:{self.rr_ratio}")

        # Short Logic (TREND_BEAR)
        elif "BEAR" in regime:
            pullback_occurred = prev['high'] > ema20
            bounce_confirmed = close < ema20 and current['high'] > ema20 * 0.998
            rsi_valid = rsi > 30
            was_below = prev2['close'] < df.iloc[-3]['EMA_20']

            if pullback_occurred and bounce_confirmed and rsi_valid and was_below:
                # ATR-based SL calculation
                structure_sl = max(current['high'], prev['high'])
                buffer = self.atr_mult * atr
                min_dist = self.min_sl_pct * close

                # SL = structure + max(buffer, min_dist) for SHORT
                stop_loss = structure_sl + max(buffer, min_dist)
                risk = stop_loss - close
                take_profit = close - risk * self.rr_ratio

                # Sanity check: SL distance should not exceed max_sl_percent of entry
                sl_distance_pct = risk / close
                if sl_distance_pct > self.max_sl_percent:
                    logger.warning(f"Rejecting SHORT signal: SL distance {sl_distance_pct:.2%} exceeds max {self.max_sl_percent:.2%}")
                    return signal

                # S/R filter: Check if TP is blocked by support
                support_levels = self._find_support_levels(df)
                if self._tp_blocked_by_sr(close, take_profit, support_levels, is_long=False):
                    logger.warning(f"Rejecting SHORT signal: TP ${take_profit:.2f} blocked by support")
                    return signal

                signal = {
                    "side": "SELL",
                    "entry_price": close,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "reason": "Trend Pullback Short: EMA20 rejection confirmed",
                    "regime": regime,
                    "confidence": confidence,
                    "atr": atr
                }
                logger.info(f"Short signal: SL=${stop_loss:.4f} (ATR={atr:.4f}, buffer={buffer:.4f}), TP=${take_profit:.4f}, RR=1:{self.rr_ratio}")

        return signal

    def _find_resistance_levels(self, df: pd.DataFrame) -> list:
        """Find recent swing highs as resistance levels."""
        levels = []
        lookback = min(self.sr_lookback, len(df) - 3)

        for i in range(2, lookback):
            idx = -(i + 1)
            if idx - 1 < -len(df) or idx + 1 >= 0:
                continue

            high = df.iloc[idx]['high']
            prev_high = df.iloc[idx - 1]['high']
            next_high = df.iloc[idx + 1]['high']

            # Swing high: higher than neighbors
            if high > prev_high and high > next_high:
                levels.append(high)

        return levels

    def _find_support_levels(self, df: pd.DataFrame) -> list:
        """Find recent swing lows as support levels."""
        levels = []
        lookback = min(self.sr_lookback, len(df) - 3)

        for i in range(2, lookback):
            idx = -(i + 1)
            if idx - 1 < -len(df) or idx + 1 >= 0:
                continue

            low = df.iloc[idx]['low']
            prev_low = df.iloc[idx - 1]['low']
            next_low = df.iloc[idx + 1]['low']

            # Swing low: lower than neighbors
            if low < prev_low and low < next_low:
                levels.append(low)

        return levels

    def _tp_blocked_by_sr(self, entry: float, tp: float, sr_levels: list, is_long: bool) -> bool:
        """
        Check if TP target is blocked by a S/R level between entry and TP.
        Returns True if trade should be skipped.
        """
        if not sr_levels:
            return False

        zone_size = self.sr_zone_pct * entry

        for level in sr_levels:
            # For LONG: check if resistance is between entry and TP
            if is_long:
                if entry < level < tp:
                    # Check if TP is within the S/R zone
                    if abs(tp - level) < zone_size:
                        return True
            # For SHORT: check if support is between entry and TP
            else:
                if tp < level < entry:
                    # Check if TP is within the S/R zone
                    if abs(tp - level) < zone_size:
                        return True

        return False
