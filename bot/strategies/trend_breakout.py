import pandas as pd
import logging

logger = logging.getLogger(__name__)


class TrendBreakoutStrategy:
    """
    Breakout strategy for trend regimes.

    Entry Logic:
    - TREND_BULL: Enter LONG when price closes above recent swing high
    - TREND_BEAR: Enter SHORT when price closes below recent swing low

    Stop Loss:
    - LONG: Below recent swing low - ATR buffer
    - SHORT: Above recent swing high + ATR buffer
    """

    def __init__(self, config: dict):
        self.config = config.get("strategies", {}).get("trend_breakout", {})
        self.sr_lookback = self.config.get("sr_lookback", 10)
        self.atr_mult = self.config.get("atr_mult", 1.2)
        self.min_sl_pct = self.config.get("min_sl_pct", 0.012)
        self.max_sl_percent = self.config.get("max_sl_percent", 0.04)
        self.rr_ratio = self.config.get("rr_ratio", 2.0)
        self.cooldown_bars = self.config.get("cooldown_bars", 3)

    def generate_signal(self, df: pd.DataFrame, regime_data) -> dict:
        signal = {"side": "NONE", "reason": "No Signal"}

        # Parse regime data
        if isinstance(regime_data, dict):
            regime = regime_data.get('regime', 'NO_TRADE')
            confidence = float(regime_data.get('confidence', 0.0))
        else:
            regime = str(regime_data)
            confidence = 0.0

        # Only trade in TREND regimes
        if "TREND" not in regime or len(df) < self.sr_lookback + 5:
            return signal

        current = df.iloc[-1]
        prev = df.iloc[-2]
        close = current['close']
        atr = current.get('ATR_14', close * 0.02)

        # Find swing levels from lookback period (excluding last 2 bars for confirmation)
        lookback_df = df.iloc[-(self.sr_lookback + 2):-2]

        # LONG: Price breaks above recent swing high
        if "BULL" in regime:
            swing_highs = self._find_swing_highs(lookback_df)
            swing_lows = self._find_swing_lows(lookback_df)

            if not swing_highs:
                logger.info(f"Breakout BULL: No swing highs found in lookback")
                return signal

            recent_swing_high = max(swing_highs)

            # Check if current candle breaks above swing high
            # Require: close above swing high AND previous close was below (fresh breakout)
            breakout_confirmed = close > recent_swing_high and prev['close'] <= recent_swing_high

            if breakout_confirmed:
                # Calculate SL below recent swing low
                if swing_lows:
                    structure_sl = min(swing_lows)
                else:
                    structure_sl = lookback_df['low'].min()

                buffer = self.atr_mult * atr
                min_dist = self.min_sl_pct * close

                stop_loss = structure_sl - max(buffer, min_dist)
                risk = close - stop_loss

                # Validate SL distance
                sl_pct = risk / close
                if sl_pct > self.max_sl_percent:
                    logger.info(f"Breakout LONG rejected: SL {sl_pct:.2%} > max {self.max_sl_percent:.2%}")
                    return signal

                take_profit = close + risk * self.rr_ratio

                signal = {
                    "side": "BUY",
                    "entry_price": close,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "reason": f"Trend Breakout Long: Broke ${recent_swing_high:.2f}",
                    "regime": regime,
                    "confidence": confidence,
                    "atr": atr
                }
                logger.info(f"Breakout LONG: Entry={close:.2f}, SL={stop_loss:.2f}, TP={take_profit:.2f}, "
                           f"SwingHigh={recent_swing_high:.2f}, RR=1:{self.rr_ratio}")
            else:
                logger.info(f"Breakout BULL check: close={close:.2f}, swing_high={recent_swing_high:.2f}, "
                           f"breakout={breakout_confirmed}")

        # SHORT: Price breaks below recent swing low
        elif "BEAR" in regime:
            swing_highs = self._find_swing_highs(lookback_df)
            swing_lows = self._find_swing_lows(lookback_df)

            if not swing_lows:
                logger.info(f"Breakout BEAR: No swing lows found in lookback")
                return signal

            recent_swing_low = min(swing_lows)

            # Check if current candle breaks below swing low
            breakout_confirmed = close < recent_swing_low and prev['close'] >= recent_swing_low

            if breakout_confirmed:
                # Calculate SL above recent swing high
                if swing_highs:
                    structure_sl = max(swing_highs)
                else:
                    structure_sl = lookback_df['high'].max()

                buffer = self.atr_mult * atr
                min_dist = self.min_sl_pct * close

                stop_loss = structure_sl + max(buffer, min_dist)
                risk = stop_loss - close

                # Validate SL distance
                sl_pct = risk / close
                if sl_pct > self.max_sl_percent:
                    logger.info(f"Breakout SHORT rejected: SL {sl_pct:.2%} > max {self.max_sl_percent:.2%}")
                    return signal

                take_profit = close - risk * self.rr_ratio

                signal = {
                    "side": "SELL",
                    "entry_price": close,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "reason": f"Trend Breakout Short: Broke ${recent_swing_low:.2f}",
                    "regime": regime,
                    "confidence": confidence,
                    "atr": atr
                }
                logger.info(f"Breakout SHORT: Entry={close:.2f}, SL={stop_loss:.2f}, TP={take_profit:.2f}, "
                           f"SwingLow={recent_swing_low:.2f}, RR=1:{self.rr_ratio}")
            else:
                logger.info(f"Breakout BEAR check: close={close:.2f}, swing_low={recent_swing_low:.2f}, "
                           f"breakout={breakout_confirmed}")

        return signal

    def _find_swing_highs(self, df: pd.DataFrame) -> list:
        """Find swing highs (local maxima) in the dataframe."""
        levels = []
        if len(df) < 3:
            return levels

        for i in range(1, len(df) - 1):
            high = df.iloc[i]['high']
            prev_high = df.iloc[i - 1]['high']
            next_high = df.iloc[i + 1]['high']

            if high > prev_high and high > next_high:
                levels.append(high)

        return levels

    def _find_swing_lows(self, df: pd.DataFrame) -> list:
        """Find swing lows (local minima) in the dataframe."""
        levels = []
        if len(df) < 3:
            return levels

        for i in range(1, len(df) - 1):
            low = df.iloc[i]['low']
            prev_low = df.iloc[i - 1]['low']
            next_low = df.iloc[i + 1]['low']

            if low < prev_low and low < next_low:
                levels.append(low)

        return levels
