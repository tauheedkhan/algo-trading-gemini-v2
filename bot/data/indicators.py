import pandas as pd
import pandas_ta as ta
import logging

logger = logging.getLogger(__name__)


def find_column(df: pd.DataFrame, prefix: str) -> str:
    """Find a column that starts with the given prefix."""
    for col in df.columns:
        if col.startswith(prefix):
            return col
    return None


class Indicators:
    @staticmethod
    def add_all(df: pd.DataFrame) -> pd.DataFrame:
        """
        Adds all necessary indicators to the DataFrame in-place.
        """
        if df.empty:
            return df

        try:
            # Trend Indicators
            df.ta.adx(append=True, length=14)
            df.ta.ema(close='close', length=20, append=True)
            df.ta.ema(close='close', length=50, append=True)

            # Momentum
            df.ta.rsi(close='close', length=14, append=True)

            # Volatility
            df.ta.bbands(close='close', length=20, std=2, append=True)
            df.ta.atr(length=14, append=True)

            # Find Bollinger Band columns (naming varies by pandas_ta version)
            bbu_col = find_column(df, 'BBU_')
            bbl_col = find_column(df, 'BBL_')
            bbm_col = find_column(df, 'BBM_')

            if bbu_col and bbl_col and bbm_col:
                df['BB_WIDTH'] = (df[bbu_col] - df[bbl_col]) / df[bbm_col]
                # Standardize column names for strategies
                df['BBU_20_2.0'] = df[bbu_col]
                df['BBL_20_2.0'] = df[bbl_col]
                df['BBM_20_2.0'] = df[bbm_col]
            else:
                logger.warning(f"BB columns not found. Available: {[c for c in df.columns if 'BB' in c]}")
                df['BB_WIDTH'] = 0.0

            # Find EMA columns and standardize names
            ema20_col = find_column(df, 'EMA_20')
            ema50_col = find_column(df, 'EMA_50')

            if ema20_col and ema50_col:
                df['EMA_SEP'] = (df[ema20_col] - df[ema50_col]) / df[ema50_col] * 100
                # Standardize column names for strategies
                df['EMA_20'] = df[ema20_col]
                df['EMA_50'] = df[ema50_col]
            else:
                logger.warning(f"EMA columns not found. Available: {[c for c in df.columns if 'EMA' in c]}")
                df['EMA_SEP'] = 0.0

            # Find RSI column and standardize
            rsi_col = find_column(df, 'RSI_')
            if rsi_col:
                df['RSI_14'] = df[rsi_col]

            # Rename ATR column for consistency (ATRr_14 -> ATR_14)
            atr_col = find_column(df, 'ATRr_')
            if atr_col:
                df['ATR_14'] = df[atr_col]

        except Exception as e:
            logger.error(f"Error calculating indicators: {e}")

        return df


indicators = Indicators()
