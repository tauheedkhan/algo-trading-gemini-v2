import pandas as pd
import logging

logger = logging.getLogger(__name__)

class RangeMeanReversionStrategy:
    def __init__(self, config: dict):
        self.config = config.get("strategies", {}).get("range_mean_reversion", {})
        self.lookback = self.config.get("divergence_lookback", 5)
        # Max allowed SL distance as percentage of entry price (e.g., 5% = 0.05)
        self.max_sl_percent = self.config.get("max_sl_percent", 0.05)

    def _check_divergence(self, df: pd.DataFrame, side: str) -> bool:
        """
        Simple RSI Divergence Check.
        Bullish: Price Lower Low, RSI Higher Low.
        Bearish: Price Higher High, RSI Lower High.
        """
        # Get recent local extremas... implementing full divergence is complex.
        # Simplified: Compare current vs lowest of last N bars.
        
        # For prototype: Skip full divergence, use Price Extremes + RSI mean reversion
        # Ideally, we would need a proper divergence detector.
        return True # Placeholder: assume valid if other conditions met

    def generate_signal(self, df: pd.DataFrame, regime: str) -> dict:
        signal = {"side": "NONE", "reason": "No Signal"}
        
        if df.empty or "RANGE" not in regime:
            return signal
            
        current = df.iloc[-1]
        
        close = current['close']
        lower_band = current['BBL_20_2.0']
        upper_band = current['BBU_20_2.0']
        rsi = current['RSI_14']
        
        # Long: Price touched Lower Band + RSI Oversold (< 35) + Closing back up?
        # Enhanced: Price < Lower Band, then Close > Lower Band (Rejection)
        if current['low'] < lower_band and close > lower_band:
            if rsi < 40: # Slightly relaxed oversold for range
                stop_loss = current['low'] * 0.995
                risk = close - stop_loss
                sl_distance_pct = risk / close

                # Sanity check: SL distance should not exceed max_sl_percent
                if sl_distance_pct > self.max_sl_percent:
                    logger.warning(f"Rejecting LONG signal: SL distance {sl_distance_pct:.2%} exceeds max {self.max_sl_percent:.2%}")
                    return signal

                signal = {
                    "side": "BUY",
                    "entry_price": close,
                    "stop_loss": stop_loss,
                    "take_profit": current['BBM_20_2.0'], # Target Mid Band
                    "reason": "Range Long: BB Rejection + RSI"
                }

        # Short: Price touched Upper Band + RSI Overbought
        elif current['high'] > upper_band and close < upper_band:
            if rsi > 60:
                stop_loss = current['high'] * 1.005
                risk = stop_loss - close
                sl_distance_pct = risk / close

                # Sanity check: SL distance should not exceed max_sl_percent
                if sl_distance_pct > self.max_sl_percent:
                    logger.warning(f"Rejecting SHORT signal: SL distance {sl_distance_pct:.2%} exceeds max {self.max_sl_percent:.2%}")
                    return signal

                signal = {
                    "side": "SELL",
                    "entry_price": close,
                    "stop_loss": stop_loss,
                    "take_profit": current['BBM_20_2.0'],
                    "reason": "Range Short: BB Rejection + RSI"
                }

        return signal
