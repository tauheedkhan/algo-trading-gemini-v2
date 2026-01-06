import pandas as pd
import logging
from bot.exchange.binance_client import binance_client

logger = logging.getLogger(__name__)

class MarketData:
    def __init__(self, client=binance_client):
        self.client = client

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 500) -> pd.DataFrame:
        """
        Fetches OHLCV data and returns a DataFrame.
        Columns: timestamp, open, high, low, close, volume
        """
        raw_data = await self.client.fetch_ohlcv(symbol, timeframe, limit)
        
        if not raw_data:
            logger.warning(f"No data returned for {symbol} {timeframe}")
            return pd.DataFrame()

        df = pd.DataFrame(raw_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # Convert timestamp to datetime
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        
        return df

market_data = MarketData()
