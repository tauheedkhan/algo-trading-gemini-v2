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

    def generate_signal(self, df: pd.DataFrame, regime_data) -> dict:
        signal = {"side": "NONE", "reason": "No Signal"}

        # Backward compatible: accept either regime string or regime_data dict
        if isinstance(regime_data, dict):
            regime = regime_data.get('regime', 'NO_TRADE')
            confidence = float(regime_data.get('confidence', 0.0))
            features = regime_data.get('features', {}) or {}
        else:
            regime = str(regime_data)
            confidence = 0.0
            features = {}

        if df.empty or "RANGE" not in regime:
            return signal

        current = df.iloc[-1]

        close = current['close']
        lower_band = current['BBL_20_2.0']
        upper_band = current['BBU_20_2.0']
        mid_band = current['BBM_20_2.0']
        rsi = current['RSI_14']
        atr = current.get('ATR_14', close * 0.02)  # Fallback to 2% if ATR not available

        # Range validity filter: avoid mean reversion during trend transitions
        try:
            lookback = int(self.config.get('range_cross_lookback', 20))
            min_crosses = int(self.config.get('range_min_crosses', 3))
            mid_slope_max = float(self.config.get('range_mid_slope_max', 0.0025))
            recent = df.tail(lookback)
            mid = recent['BBM_20_2.0']
            # Count crossings of close around mid band
            sign = (recent['close'] - mid).apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
            crosses = int((sign.diff().fillna(0).abs() > 1).sum())
            # Mid-band slope proxy (normalized)
            mid_slope = abs(float(mid.iloc[-1] - mid.iloc[0])) / max(1e-9, float(recent['close'].iloc[-1]))
            if crosses < min_crosses or mid_slope > mid_slope_max:
                return {"side": "NONE", "reason": f"Range invalid (crosses={crosses}, mid_slope={mid_slope:.4f})"}
        except Exception:
            # If indicators missing, fail safe: do not trade mean reversion
            return {"side": "NONE", "reason": "Range invalid (missing indicators)"}


        # Long: Price touched Lower Band + RSI Oversold (< 40) + Closing back up
        if current['low'] < lower_band and close > lower_band:
            if rsi < self.config.get('rsi_oversold', 30):
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
                    "reason": "Range Long: BB Rejection + RSI",
                    "regime": regime,
                    "confidence": confidence,
                    "atr": atr
                }
                logger.info(f"Long signal: SL=${stop_loss:.4f} (ATR={atr:.4f}), TP=${take_profit:.4f} (mid-band), RR=1:{potential_rr:.1f}")

        # Short: Price touched Upper Band + RSI Overbought
        elif current['high'] > upper_band and close < upper_band:
            if rsi > self.config.get('rsi_overbought', 70):
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
                    "reason": "Range Short: BB Rejection + RSI",
                    "regime": regime,
                    "confidence": confidence,
                    "atr": atr
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
