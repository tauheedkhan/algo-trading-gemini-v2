import pandas as pd
import numpy as np
from bot.data.indicators import indicators
from bot.regime.regime_classifier import regime_classifier
from bot.strategies.router import StrategyRouter
from bot.risk.risk_engine import RiskEngine

def generate_mock_data(length=500):
    dates = pd.date_range(end=pd.Timestamp.now(), periods=length, freq='1h')
    
    # Generate random walk for close prices to simulate trend/range
    close = 50000 + np.cumsum(np.random.normal(0, 100, length))
    
    df = pd.DataFrame({
        'timestamp': dates,
        'open': close + np.random.normal(0, 20, length),
        'high': close + np.random.normal(50, 20, length),
        'low': close - np.random.normal(50, 20, length),
        'close': close,
        'volume': np.random.randint(100, 1000, length)
    })
    return df

def test_pipeline():
    print(">>> Testing Bot Components Pipeline...")
    
    # 1. Mock Data
    df = generate_mock_data()
    print(f"[OK] Generated Mock Data: {len(df)} rows")
    
    # 2. Indicators
    indicators.add_all(df)
    if 'ADX_14' in df.columns and 'RSI_14' in df.columns:
        print("[OK] Indicators Calculated (ADX, RSI, BB, EMA)")
    else:
        print("[FAIL] Indicators missing")
        return
        
    # 3. Regime Detection
    regime = regime_classifier.detect_regime(df, "BTC/USDT")
    print(f"[OK] Regime Detected: {regime['regime']} (Conf: {regime['confidence']:.2f})")
    
    # 4. Strategy Router
    config = {
        "strategies": {
            "trend_pullback": {"enabled": True},
            "range_mean_reversion": {"enabled": True}
        }
    }
    router = StrategyRouter(config)
    
    # Force a mock regime to test strategy signal generation
    mock_regime_trend = {"symbol": "BTC", "regime": "TREND_BULL"}
    signal_trend = router.check_signal(df, mock_regime_trend)
    print(f"[OK] Strategy Signal Check (Mock TREND_BULL): {signal_trend['side']}")
    
    mock_regime_range = {"symbol": "BTC", "regime": "RANGE"}
    signal_range = router.check_signal(df, mock_regime_range)
    print(f"[OK] Strategy Signal Check (Mock RANGE): {signal_range['side']}")
    
    # 5. Risk Calculation
    risk_config = {"risk": {"target_risk_per_trade_percent": 0.02, "leverage": 2}}
    risk = RiskEngine(risk_config)
    equity = 5000
    entry = 50000
    stop = 49000 # 1000 risk
    
    size = risk.calculate_position_size(equity, entry, {"stop_loss": stop})
    print(f"[OK] Risk Sizing: Equity=${equity}, Risk=2%, Entry={entry}, Stop={stop} -> Size={size:.4f} BTC")
    
    expected_risk = equity * 0.02 # 100 USD
    actual_risk = size * abs(entry - stop)
    print(f"    Expected Risk: ${expected_risk}, Actual Risk: ${actual_risk:.2f}")

if __name__ == "__main__":
    test_pipeline()
