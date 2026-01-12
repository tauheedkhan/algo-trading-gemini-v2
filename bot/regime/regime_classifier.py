import pandas as pd
import numpy as np
import logging

logger = logging.getLogger(__name__)

class RegimeClassifier:
    def __init__(self, config: dict = None):
        self.config = config or {}
        self.confirmed_regime = "NO_TRADE"
        self.pending_regime = None
        self.pending_count = 0
        self.min_duration = self.config.get("regime", {}).get("min_duration_bars", 3)

    def _confirm_with_hysteresis(self, new_regime: str) -> str:
        # If same as confirmed, reset pending
        if new_regime == self.confirmed_regime:
            self.pending_regime = None
            self.pending_count = 0
            return self.confirmed_regime

        # If new pending regime changes, reset counter
        if new_regime != self.pending_regime:
            self.pending_regime = new_regime
            self.pending_count = 1
            return self.confirmed_regime

        # Same pending regime repeats
        self.pending_count += 1
        if self.pending_count >= self.min_duration:
            self.confirmed_regime = new_regime
            self.pending_regime = None
            self.pending_count = 0

        return self.confirmed_regime

    def detect_regime(self, df: pd.DataFrame, symbol: str) -> dict:
        if df.empty or len(df) < 50:
            return {"symbol": symbol, "regime": "NO_TRADE", "reason": "Insufficient Data"}

        current = df.iloc[-1]

        lookback = self.config.get("regime", {}).get("volatility_percentile_window", 200)
        recent = df.tail(lookback)

        adx = float(current["ADX_14"])
        bb_width = float(current["BB_WIDTH"])
        ema_sep = float(current.get("EMA_SEP", 0.0))

        adx_high = float(recent["ADX_14"].quantile(0.60))
        adx_low  = float(recent["ADX_14"].quantile(0.40))
        bw_low   = float(recent["BB_WIDTH"].quantile(0.20))

        # Optional thresholds
        sep_min = float(self.config.get("regime", {}).get("ema_sep_min", 0.0))

        proposed = "NO_TRADE"
        confidence = 0.0
        reason = ""

        # 1) TREND: ADX strong + direction meaningful
        if adx > adx_high and abs(ema_sep) > sep_min:
            direction = "BULL" if ema_sep > 0 else "BEAR"
            proposed = f"TREND_{direction}"

            # Confidence: distance above adx_high normalized by recent ADX range
            adx_min = float(recent["ADX_14"].min())
            adx_max = float(recent["ADX_14"].max())
            denom = max(1e-9, (adx_max - adx_high))
            confidence = (adx - adx_high) / denom
            reason = "ADX strong + EMA separation"

        # 2) SQUEEZE: volatility compression (can be range, but usually breakout-ready)
        elif bb_width < bw_low:
            proposed = "SQUEEZE"
            bw_med = float(recent["BB_WIDTH"].median())
            denom = max(1e-9, bw_med)
            confidence = (bw_low - bb_width) / denom
            reason = "BB bandwidth compressed"

        # 3) RANGE: ADX weak (mean-reversion opportunity), even if BB width not squeezed
        elif adx < adx_low:
            proposed = "RANGE"
            denom = max(1e-9, adx_low)
            confidence = (adx_low - adx) / denom
            reason = "ADX weak (non-trending)"

        # 4) Otherwise: transition / messy -> NO_TRADE
        else:
            proposed = "NO_TRADE"
            confidence = 0.0
            reason = "Transition zone"

        confirmed = self._confirm_with_hysteresis(proposed)

        return {
            "symbol": symbol,
            "regime": confirmed,
            "proposed_regime": proposed,
            "confidence": float(np.clip(confidence, 0.0, 1.0)),
            "features": {
                "adx": adx,
                "bb_width": bb_width,
                "ema_sep": ema_sep,
                "adx_threshold_high": adx_high,
                "adx_threshold_low": adx_low,
                "bw_threshold_low": bw_low,
                "pending_regime": self.pending_regime,
                "pending_count": self.pending_count,
                "min_duration_bars": self.min_duration,
            },
            "reason": reason,
        }

def create_regime_classifier(config: dict = None) -> RegimeClassifier:
    """Factory function to create regime classifier with config."""
    return RegimeClassifier(config)


# Will be re-initialized with config by engine
regime_classifier = RegimeClassifier()
