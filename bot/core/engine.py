import asyncio
import logging
from bot.core.config import load_config
from bot.exchange.binance_client import binance_client
from bot.data.market_data import market_data
from bot.data.indicators import indicators
from bot.regime.regime_classifier import regime_classifier
from bot.strategies.router import StrategyRouter
from bot.execution.executor import executor
from bot.state.db import db

logger = logging.getLogger(__name__)

class TradingEngine:
    def __init__(self):
        self.config = load_config("config.yaml")
        self.symbols = self.config.get("symbols", [])
        self.router = StrategyRouter(self.config)

        # Update risk engine with full config
        risk_config = self.config.get('risk', {})
        executor.risk_engine.target_risk_pct = risk_config.get('target_risk_per_trade_percent', 0.02)
        executor.risk_engine.max_positions = risk_config.get('max_open_positions', 3)
        executor.risk_engine.leverage = risk_config.get('leverage', 2)
        executor.risk_engine.max_drawdown_daily_pct = risk_config.get('max_drawdown_daily_percent', 5.0) / 100
        executor.risk_engine.max_position_pct = risk_config.get('max_position_percent', 0.25)

        logger.info(f"Risk config: {risk_config.get('target_risk_per_trade_percent')*100}% risk, "
                    f"{risk_config.get('leverage')}x leverage, "
                    f"{risk_config.get('max_position_percent', 0.25)*100:.0f}% max position")
        
    async def start(self):
        logger.info("Starting Trading Engine Loop...")
        await db.connect()
        await binance_client.initialize()
        
        while True:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.error(f"Error in trading cycle: {e}")
            
            await asyncio.sleep(60) # Run every minute (1H Strategy base)

    async def run_cycle(self):
        logger.info("Running Analysis Cycle...")

        # 0. Check Kill-Switch
        if executor.risk_engine.is_killed:
            logger.warning("Kill-switch is active. Skipping trading cycle.")
            return

        # 1. Update Account Info (Equity)
        balance = await binance_client.get_balance()
        equity = float(balance['total']['USDT'])
        free_balance = float(balance['free']['USDT'])
        unrealized_pnl = float(balance.get('info', {}).get('totalUnrealizedProfit', 0))

        # 2. Save equity snapshot and check drawdown
        await db.save_equity_snapshot(
            balance=free_balance,
            equity=equity,
            unrealized_pnl=unrealized_pnl
        )

        start_equity = await db.get_daily_start_equity()
        if start_equity:
            if not executor.risk_engine.check_daily_drawdown(start_equity, equity):
                await db.log_system_event("KILL_SWITCH", executor.risk_engine.kill_switch_reason)
                logger.critical("Trading halted due to drawdown limit breach.")
                return

        # 3. Process Symbols
        for symbol in self.symbols:
            await self.process_symbol(symbol, equity)
            
    async def process_symbol(self, symbol: str, equity: float):
        # A. Fetch Data
        df = await market_data.get_candles(symbol, "1h", limit=500)
        if df.empty:
            return

        # B. Calculate Indicators
        indicators.add_all(df)
        
        # C. Detect Regime
        regime_info = regime_classifier.detect_regime(df, symbol)
        
        # Save Regime to DB
        await db.execute(
            "INSERT INTO regimes (symbol, regime, confidence, features_json) VALUES (?, ?, ?, ?)",
            (symbol, regime_info['regime'], regime_info['confidence'], str(regime_info['features']))
        )
        
        logger.info(f"[{symbol}] Regime: {regime_info['regime']} (Conf: {regime_info['confidence']:.2f})")
        
        # D. Strategy Signal
        signal = self.router.check_signal(df, regime_info)
        signal['symbol'] = symbol
        
        # E. Execution
        # Fetch current positions from exchange
        try:
            current_positions = await binance_client.fetch_positions()
        except Exception as e:
            logger.error(f"Failed to fetch positions, skipping execution for {symbol}: {e}")
            return

        if signal["side"] != "NONE":
            await executor.execute_signal(signal, equity, current_positions)

# Global Engine
trading_engine = TradingEngine()
