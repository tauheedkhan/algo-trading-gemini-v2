import pandas as pd
import logging

logger = logging.getLogger(__name__)


class RangeMeanReversionStrategy:
    def __init__(self, config: dict):
        self.config = config.get("strategies", {}).get("range_mean_reversion", {})
        self.lookback = self.config.get("divergence_lookback", 5)
        # Max allowed SL distance as percentage of entry price (e.g., 5% = 0.05)
        self.max_sl_percent = self.config.get("max_sl_percent", 0.05)

        # ATR-based stop parameters
        self.atr_mult = self.config.get("atr_mult", 1.5)
        self.min_sl_pct = self.config.get("min_sl_pct", 0.012)  # 1.2% minimum

        # S/R filter parameters
        self.sr_lookback = self.config.get("sr_lookback", 20)
        self.sr_zone_pct = self.config.get("sr_zone_pct", 0.005)

    def _check_divergence(self, df: pd.DataFrame, side: str) -> bool:
        """
        Simple RSI Divergence Check.
        Bullish: Price Lower Low, RSI Higher Low.
        Bearish: Price Higher High, RSI Lower High.
        """
        # For prototype: Skip full divergence, use Price Extremes + RSI mean reversion
        return True

    def generate_signal(self, df: pd.DataFrame, regime: str) -> dict:
        signal = {"side": "NONE", "reason": "No Signal"}

        if df.empty or "RANGE" not in regime:
            return signal

        current = df.iloc[-1]

        close = current['close']
        lower_band = current['BBL_20_2.0']
        upper_band = current['BBU_20_2.0']
        mid_band = current['BBM_20_2.0']
        rsi = current['RSI_14']
        atr = current.get('ATR_14', close * 0.02)  # Fallback to 2% if ATR not available

        # Long: Price touched Lower Band + RSI Oversold (< 40) + Closing back up
        if current['low'] < lower_band and close > lower_band:
            if rsi < 40:
                # ATR-based SL calculation
                structure_sl = current['low']
                buffer = self.atr_mult * atr
                min_dist = self.min_sl_pct * close

                # SL = structure - max(buffer, min_dist) for LONG
                stop_loss = structure_sl - max(buffer, min_dist)
                risk = close - stop_loss

                # Sanity check: SL distance should not exceed max_sl_percent
                sl_distance_pct = risk / close
                if sl_distance_pct > self.max_sl_percent:
                    logger.warning(f"Rejecting LONG signal: SL distance {sl_distance_pct:.2%} exceeds max {self.max_sl_percent:.2%}")
                    return signal

                # Target mid band, but ensure minimum RR of 1:1
                take_profit = mid_band
                potential_rr = (take_profit - close) / risk if risk > 0 else 0
                if potential_rr < 1.0:
                    logger.warning(f"Rejecting LONG signal: RR {potential_rr:.2f} too low (TP at mid-band)")
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
                    "reason": "Range Long: BB Rejection + RSI"
                }
                logger.info(f"Long signal: SL=${stop_loss:.4f} (ATR={atr:.4f}), TP=${take_profit:.4f} (mid-band), RR=1:{potential_rr:.1f}")

        # Short: Price touched Upper Band + RSI Overbought
        elif current['high'] > upper_band and close < upper_band:
            if rsi > 60:
                # ATR-based SL calculation
                structure_sl = current['high']
                buffer = self.atr_mult * atr
                min_dist = self.min_sl_pct * close

                # SL = structure + max(buffer, min_dist) for SHORT
                stop_loss = structure_sl + max(buffer, min_dist)
                risk = stop_loss - close

                # Sanity check: SL distance should not exceed max_sl_percent
                sl_distance_pct = risk / close
                if sl_distance_pct > self.max_sl_percent:
                    logger.warning(f"Rejecting SHORT signal: SL distance {sl_distance_pct:.2%} exceeds max {self.max_sl_percent:.2%}")
                    return signal

                # Target mid band, but ensure minimum RR of 1:1
                take_profit = mid_band
                potential_rr = (close - take_profit) / risk if risk > 0 else 0
                if potential_rr < 1.0:
                    logger.warning(f"Rejecting SHORT signal: RR {potential_rr:.2f} too low (TP at mid-band)")
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
                    "reason": "Range Short: BB Rejection + RSI"
                }
                logger.info(f"Short signal: SL=${stop_loss:.4f} (ATR={atr:.4f}), TP=${take_profit:.4f} (mid-band), RR=1:{potential_rr:.1f}")

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

            if low < prev_low and low < next_low:
                levels.append(low)

        return levels

    def _tp_blocked_by_sr(self, entry: float, tp: float, sr_levels: list, is_long: bool) -> bool:
        """Check if TP target is blocked by a S/R level between entry and TP."""
        if not sr_levels:
            return False

        zone_size = self.sr_zone_pct * entry

        for level in sr_levels:
            if is_long:
                if entry < level < tp:
                    if abs(tp - level) < zone_size:
                        return True
            else:
                if tp < level < entry:
                    if abs(tp - level) < zone_size:
                        return True

        return False
