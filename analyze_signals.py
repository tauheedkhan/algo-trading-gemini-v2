#!/usr/bin/env python3
"""
Script to analyze recent candles and determine if trades should have been executed.
"""
import asyncio
import pandas as pd
import yaml
from datetime import datetime

# Import bot modules
from bot.data.market_data import MarketData
from bot.data.indicators import Indicators
from bot.regime.regime_classifier import RegimeClassifier
from bot.strategies.trend_pullback import TrendPullbackStrategy
from bot.strategies.range_meanrev import RangeMeanReversionStrategy


def load_config():
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)


async def fetch_and_analyze():
    config = load_config()
    market_data = MarketData()
    regime_classifier = RegimeClassifier(config)
    trend_strategy = TrendPullbackStrategy(config)
    range_strategy = RangeMeanReversionStrategy(config)

    symbols = ['BTC/USDT', 'ETH/USDT']
    timeframes = ['1h', '4h', '1d']

    for symbol in symbols:
        print(f"\n{'='*60}")
        print(f"  {symbol}")
        print(f"{'='*60}")

        for tf in timeframes:
            print(f"\n--- {tf} Timeframe ---")

            # Fetch enough candles for indicators (need at least 50 for EMA_50)
            limit = 100 if tf == '1h' else 50
            df = await market_data.get_candles(symbol, tf, limit=limit)

            if df.empty:
                print(f"  No data for {tf}")
                continue

            # Add indicators
            df = Indicators.add_all(df)

            # Get last few candles
            last_candles = df.tail(5)

            print(f"\n  Last 3 candles:")
            for idx, row in last_candles.tail(3).iterrows():
                ts = row['timestamp'].strftime('%Y-%m-%d %H:%M')
                print(f"    {ts} | O:{row['open']:.2f} H:{row['high']:.2f} L:{row['low']:.2f} C:{row['close']:.2f}")

            # Show key indicators for latest candle
            latest = df.iloc[-1]
            print(f"\n  Latest Indicators:")

            print(f"    ADX_14: {latest.get('ADX_14', 0):.2f}")
            print(f"    EMA_20: {latest.get('EMA_20', 0):.2f}")
            print(f"    EMA_50: {latest.get('EMA_50', 0):.2f}")
            print(f"    EMA_SEP: {latest.get('EMA_SEP', 0):.2f}%")
            print(f"    RSI_14: {latest.get('RSI_14', 0):.2f}")
            print(f"    BB Upper: {latest.get('BBU_20_2.0', 0):.2f}")
            print(f"    BB Lower: {latest.get('BBL_20_2.0', 0):.2f}")
            print(f"    BB Width: {latest.get('BB_WIDTH', 0):.4f}")

            # Only analyze 1h timeframe for trade signals (as per bot config)
            if tf == '1h':
                # Detect regime
                regime_result = regime_classifier.detect_regime(df, symbol)
                regime = regime_result['regime']
                confidence = regime_result['confidence']
                features = regime_result['features']

                print(f"\n  Regime Detection:")
                print(f"    Regime: {regime}")
                print(f"    Confidence: {confidence:.2f}")
                print(f"    ADX Threshold (High): {features['adx_threshold_high']:.2f}")
                print(f"    BB Width Threshold (Low): {features['bw_threshold_low']:.4f}")

                # Check for signals based on regime
                print(f"\n  Signal Analysis:")

                if regime == "NO_TRADE":
                    print(f"    Result: NO TRADE - Market conditions unclear")
                    explain_no_trade(df, features)
                elif "TREND" in regime:
                    signal = trend_strategy.generate_signal(df, regime)
                    if signal['side'] != "NONE":
                        print(f"    SIGNAL FOUND!")
                        print(f"      Side: {signal['side']}")
                        print(f"      Entry: {signal['entry_price']:.2f}")
                        print(f"      Stop Loss: {signal['stop_loss']:.2f}")
                        print(f"      Take Profit: {signal['take_profit']:.2f}")
                        print(f"      Reason: {signal['reason']}")
                    else:
                        print(f"    Result: NO SIGNAL - Trend pullback conditions not met")
                        explain_trend_signal(df, regime)
                elif regime == "RANGE":
                    signal = range_strategy.generate_signal(df, regime)
                    if signal['side'] != "NONE":
                        print(f"    SIGNAL FOUND!")
                        print(f"      Side: {signal['side']}")
                        print(f"      Entry: {signal['entry_price']:.2f}")
                        print(f"      Stop Loss: {signal['stop_loss']:.2f}")
                        print(f"      Take Profit: {signal['take_profit']:.2f}")
                        print(f"      Reason: {signal['reason']}")
                    else:
                        print(f"    Result: NO SIGNAL - Mean reversion conditions not met")
                        explain_range_signal(df)


def explain_no_trade(df, features):
    """Explain why NO_TRADE regime was detected."""
    current = df.iloc[-1]
    adx = current.get('ADX_14', 0)
    bb_width = current.get('BB_WIDTH', 0)

    print(f"\n    Why NO_TRADE regime:")
    print(f"      - ADX ({adx:.2f}) not > {features['adx_threshold_high']:.2f} (for TREND)")
    print(f"      - ADX ({adx:.2f}) not < 40th percentile OR BB_WIDTH ({bb_width:.4f}) not < {features['bw_threshold_low']:.4f} (for RANGE)")


def explain_trend_signal(df, regime):
    """Explain why trend pullback signal wasn't generated."""
    current = df.iloc[-1]
    prev = df.iloc[-2]
    prev2 = df.iloc[-3]

    ema20 = current.get('EMA_20', 0)
    prev_ema20 = prev.get('EMA_20', 0)
    rsi = current.get('RSI_14', 50)
    close = current['close']

    print(f"\n    Why no signal (Trend Pullback):")

    if "BULL" in regime:
        pullback = prev['low'] < ema20
        bounce = close > ema20 and current['low'] < ema20 * 1.002
        rsi_ok = rsi < 70
        was_above = prev2['close'] > df.iloc[-3].get('EMA_20', 0)

        if not pullback:
            print(f"      - No pullback: Prev low ({prev['low']:.2f}) not < EMA20 ({prev_ema20:.2f})")
        if not bounce:
            print(f"      - No bounce confirmation: Close ({close:.2f}) vs EMA20 ({ema20:.2f})")
        if not rsi_ok:
            print(f"      - RSI overbought: {rsi:.1f} >= 70")
        if not was_above:
            print(f"      - Wasn't trending above EMA20 before pullback")
    else:  # BEAR
        pullback = prev['high'] > ema20
        bounce = close < ema20 and current['high'] > ema20 * 0.998
        rsi_ok = rsi > 30
        was_below = prev2['close'] < df.iloc[-3].get('EMA_20', 0)

        if not pullback:
            print(f"      - No rally: Prev high ({prev['high']:.2f}) not > EMA20 ({prev_ema20:.2f})")
        if not bounce:
            print(f"      - No rejection confirmation: Close ({close:.2f}) vs EMA20 ({ema20:.2f})")
        if not rsi_ok:
            print(f"      - RSI oversold: {rsi:.1f} <= 30")
        if not was_below:
            print(f"      - Wasn't trending below EMA20 before rally")


def explain_range_signal(df):
    """Explain why range mean reversion signal wasn't generated."""
    current = df.iloc[-1]

    upper = current.get('BBU_20_2.0', 0)
    lower = current.get('BBL_20_2.0', 0)
    rsi = current.get('RSI_14', 50)
    close = current['close']
    high = current['high']
    low = current['low']

    print(f"\n    Why no signal (Mean Reversion):")

    # Check for buy signal conditions
    touched_lower = low < lower
    closed_above_lower = close > lower
    rsi_oversold = rsi < 40

    if not touched_lower:
        print(f"      - Price didn't touch lower BB (Low: {low:.2f} not < {lower:.2f})")
    elif not closed_above_lower:
        print(f"      - Closed below lower BB (Close: {close:.2f} not > {lower:.2f})")
    elif not rsi_oversold:
        print(f"      - RSI not oversold ({rsi:.1f} not < 40)")

    # Check for sell signal conditions
    touched_upper = high > upper
    closed_below_upper = close < upper
    rsi_overbought = rsi > 60

    if not touched_upper:
        print(f"      - Price didn't touch upper BB (High: {high:.2f} not > {upper:.2f})")
    elif not closed_below_upper:
        print(f"      - Closed above upper BB (Close: {close:.2f} not < {upper:.2f})")
    elif not rsi_overbought:
        print(f"      - RSI not overbought ({rsi:.1f} not > 60)")


if __name__ == "__main__":
    print("="*60)
    print("  TRADING SIGNAL ANALYSIS")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)

    asyncio.run(fetch_and_analyze())
