import pandas as pd
import numpy as np
import logging

logger = logging.getLogger(__name__)

class RegimeClassifier:
    def __init__(self, config: dict = None):
        self.config = config or {}
        self.last_regime = "NO_TRADE"
        self.regime_duration = 0
        self.min_duration = 3  # Hysteresis: regime must hold for N bars to switch confirmed
    
    def detect_regime(self, df: pd.DataFrame, symbol: str) -> dict:
        """
        Determines the market regime based on the latest available data.
        Returns a dict with regime, confidence, and internal metrics.
        """
        if df.empty or len(df) < 50:
            return {"regime": "NO_TRADE", "reason": "Insufficient Data"}
        
        # Get latest row
        current = df.iloc[-1]
        
        # Dynamic Thresholds (Percentiles over last N periods)
        lookback = self.config.get("regime", {}).get("volatility_percentile_window", 200)
        recent_history = df.tail(lookback)
        
        # ADX Thresholds
        adx_high = recent_history['ADX_14'].quantile(0.60) # Top 40% strength
        adx_low = recent_history['ADX_14'].quantile(0.40)  # Bottom 40% strength
        
        # Bandwidth Thresholds
        bw_low = recent_history['BB_WIDTH'].quantile(0.20) # Extreme squeeze
        
        # Current Values
        adx = current['ADX_14']
        bb_width = current['BB_WIDTH']
        ema_sep = current.get('EMA_SEP', 0)
        
        # Classification Logic
        new_regime = "NO_TRADE"
        confidence = 0.0
        
        # 1. TREND: Strong ADX
        if adx > adx_high:
            direction = "BULL" if ema_sep > 0 else "BEAR"
            new_regime = f"TREND_{direction}"
            confidence = (adx - adx_high) / (100 - adx_high) # Simple confidence score
            
        # 2. RANGE: Weak ADX + Low Volatility
        elif adx < adx_low and bb_width < bw_low:
            new_regime = "RANGE"
            confidence = (adx_low - adx) / adx_low
            
        # 3. BREAKOUT (Simple): Volatility Expansion from Squeeze
        # Logic: If prev was Range/Squeeze and now Width is expanding fast
        # Keeping it simple for now: if ADX is rising fast but below trend threshold?
        # For now, let's stick to Trend vs Range. Breakout is often the start of Trend.
        
        # Hysteresis Check
        if new_regime != self.last_regime:
            self.regime_duration = 0
            self.last_regime = new_regime # Instant switch for now, but in prod we might wait 1-2 bars
        else:
            self.regime_duration += 1
            
        return {
            "symbol": symbol,
            "regime": new_regime,
            "confidence": min(max(confidence, 0.0), 1.0),
            "features": {
                "adx": adx,
                "bb_width": bb_width,
                "adx_threshold_high": adx_high,
                "bw_threshold_low": bw_low
            }
        }

regime_classifier = RegimeClassifier()
