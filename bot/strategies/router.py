import pandas as pd
import logging

from bot.strategies.trend_pullback import TrendPullbackStrategy
from bot.strategies.range_meanrev import RangeMeanReversionStrategy

logger = logging.getLogger(__name__)


class StrategyRouter:
    """
    Strategy router that:
    - Avoids whipsaw by requiring proposed_regime == confirmed_regime (configurable)
    - Skips NO_TRADE / SQUEEZE regimes (and any other explicitly configured no-trade regimes)
    - Applies confidence gating centrally
    - Passes full regime_data to strategies so they can use confidence/features/ATR
    - Enforces per-strategy enabled flags
    """

    def __init__(self, config: dict):
        self.config = config or {}
        self.trend_strat = TrendPullbackStrategy(self.config)
        self.range_strat = RangeMeanReversionStrategy(self.config)

    def _is_enabled(self, strategy_key: str) -> bool:
        return bool(self.config.get("strategies", {}).get(strategy_key, {}).get("enabled", True))

    def check_signal(self, df: pd.DataFrame, regime_data: dict) -> dict:
        symbol = regime_data.get("symbol", "UNKNOWN")

        confirmed = regime_data.get("regime", "NO_TRADE")
        proposed = regime_data.get("proposed_regime", confirmed)
        confidence = float(regime_data.get("confidence", 0.0))

        # Router behavior knobs
        trade_only_when_confirmed = bool(self.config.get("regime", {}).get("trade_only_when_confirmed", True))
        no_trade_regimes = set(self.config.get("regime", {}).get("no_trade_regimes", ["NO_TRADE", "SQUEEZE"]))

        # 1) Transition guard (avoid regime flip-flops)
        if trade_only_when_confirmed and proposed != confirmed:
            return {
                "side": "NONE",
                "symbol": symbol,
                "regime": confirmed,
                "confidence": confidence,
                "reason": f"Transition: proposed={proposed}, confirmed={confirmed}",
            }

        # 2) Explicit no-trade regimes
        if confirmed in no_trade_regimes:
            return {
                "side": "NONE",
                "symbol": symbol,
                "regime": confirmed,
                "confidence": confidence,
                "reason": f"Regime {confirmed}",
            }

        # 3) Confidence filter (central)
        min_conf = float(self.config.get("risk", {}).get("min_confidence_threshold", 0.0))
        if confidence < min_conf:
            return {
                "side": "NONE",
                "symbol": symbol,
                "regime": confirmed,
                "confidence": confidence,
                "reason": f"Low confidence {confidence:.2f} < {min_conf:.2f}",
            }

        # 4) Route to strategy (full regime_data, not just regime string)
        try:
            if confirmed.startswith("TREND_"):
                if not self._is_enabled("trend_pullback"):
                    return {"side": "NONE", "symbol": symbol, "regime": confirmed, "confidence": confidence, "reason": "trend_pullback disabled"}
                signal = self.trend_strat.generate_signal(df, regime_data)

            elif confirmed == "RANGE":
                if not self._is_enabled("range_mean_reversion"):
                    return {"side": "NONE", "symbol": symbol, "regime": confirmed, "confidence": confidence, "reason": "range_mean_reversion disabled"}
                signal = self.range_strat.generate_signal(df, regime_data)

            else:
                return {
                    "side": "NONE",
                    "symbol": symbol,
                    "regime": confirmed,
                    "confidence": confidence,
                    "reason": f"Unhandled regime {confirmed}",
                }

        except Exception as e:
            logger.exception("Strategy error for %s in regime %s", symbol, confirmed)
            return {
                "side": "NONE",
                "symbol": symbol,
                "regime": confirmed,
                "confidence": confidence,
                "reason": f"Strategy exception: {type(e).__name__}",
            }

        # 5) Standardize output
        if not isinstance(signal, dict):
            return {"side": "NONE", "symbol": symbol, "regime": confirmed, "confidence": confidence, "reason": "Invalid signal type"}

        signal.setdefault("symbol", symbol)
        signal.setdefault("regime", confirmed)
        signal.setdefault("confidence", confidence)

        if signal.get("side") != "NONE":
            logger.info("SIGNAL FOUND [%s] %s conf=%.2f: %s", symbol, confirmed, confidence, signal)

        return signal


def get_router(config: dict) -> StrategyRouter:
    return StrategyRouter(config)
