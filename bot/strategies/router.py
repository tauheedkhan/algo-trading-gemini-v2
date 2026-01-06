import pandas as pd
from bot.strategies.trend_pullback import TrendPullbackStrategy
from bot.strategies.range_meanrev import RangeMeanReversionStrategy
import logging

logger = logging.getLogger(__name__)

class StrategyRouter:
    def __init__(self, config: dict):
        self.config = config
        self.trend_strat = TrendPullbackStrategy(config)
        self.range_strat = RangeMeanReversionStrategy(config)
        
    def check_signal(self, df: pd.DataFrame, regime_data: dict) -> dict:
        """
        Routes the data to the correct strategy.
        """
        regime = regime_data.get("regime", "NO_TRADE")
        symbol = regime_data.get("symbol", "UNKNOWN")
        
        if "NO_TRADE" in regime:
            return {"side": "NONE", "reason": "Regime NO_TRADE"}
            
        signal = {"side": "NONE"}
        
        if "TREND" in regime:
            signal = self.trend_strat.generate_signal(df, regime)
        elif "RANGE" in regime:
            signal = self.range_strat.generate_signal(df, regime)
            
        if signal["side"] != "NONE":
            logger.info(f"SIGNAL FOUND [{symbol}]: {signal}")
            
        return signal

# Singleton not needed necessarily, but useful if stateful
def get_router(config):
    return StrategyRouter(config)
