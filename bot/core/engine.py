import asyncio
import logging
from bot.core.config import load_config
from bot.exchange.binance_client import binance_client
from bot.exchange.websocket_client import ws_client
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
        self._ws_initialized = False

        # Update risk engine and executor with full config
        risk_config = self.config.get('risk', {})
        executor.risk_engine.target_risk_pct = risk_config.get('target_risk_per_trade_percent', 0.02)
        executor.risk_engine.max_positions = risk_config.get('max_open_positions', 3)
        executor.risk_engine.leverage = risk_config.get('leverage', 2)
        executor.risk_engine.max_drawdown_daily_pct = risk_config.get('max_drawdown_daily_percent', 5.0) / 100
        executor.risk_engine.max_position_pct = risk_config.get('max_position_percent', 0.25)

        # Set margin mode from config (ISOLATED or CROSSED)
        executor.margin_mode = risk_config.get('margin_mode', 'ISOLATED').upper()

        logger.info(f"Risk config: {risk_config.get('target_risk_per_trade_percent')*100}% risk, "
                    f"{risk_config.get('leverage')}x leverage, "
                    f"{risk_config.get('max_position_percent', 0.25)*100:.0f}% max position, "
                    f"{executor.margin_mode} margin")

    async def _initialize_websocket(self):
        """Initialize WebSocket and preload historical data."""
        if self._ws_initialized:
            return

        logger.info("Initializing WebSocket and preloading historical data...")

        # Initialize WebSocket for all symbols
        await ws_client.initialize(self.symbols, timeframes=["1h"])

        # Preload historical data via REST API (one-time)
        for symbol in self.symbols:
            try:
                df = await market_data.get_candles(symbol, "1h", limit=500)
                if not df.empty:
                    candles = df.to_dict('records')
                    await ws_client.preload_candles(symbol, "1h", candles)
                    logger.info(f"Preloaded {len(candles)} candles for {symbol}")
                # Small delay between REST calls during preload
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(f"Failed to preload {symbol}: {e}")

        self._ws_initialized = True
        logger.info("WebSocket initialization complete")

    async def start(self):
        logger.info("Starting Trading Engine Loop...")
        await db.connect()
        await binance_client.initialize()

        # Initialize WebSocket and preload historical data
        await self._initialize_websocket()

        # Start WebSocket in background task
        ws_task = asyncio.create_task(ws_client.start())

        # Wait a moment for WebSocket to connect
        await asyncio.sleep(2)

        consecutive_errors = 0
        try:
            while True:
                try:
                    await self.run_cycle()
                    consecutive_errors = 0
                except Exception as e:
                    consecutive_errors += 1
                    # If rate limited, wait longer before retry
                    if "Rate limited" in str(e) or "418" in str(e) or "429" in str(e):
                        wait_time = min(60 * (2 ** consecutive_errors), 300)
                        logger.warning(f"Trading engine backing off for {wait_time}s due to rate limit")
                        await asyncio.sleep(wait_time)
                        continue
                    logger.error(f"Error in trading cycle: {e}")

                await asyncio.sleep(60)  # Run every minute (1H Strategy base)
        finally:
            await ws_client.stop()
            ws_task.cancel()

    async def run_cycle(self):
        logger.info("Running Analysis Cycle...")

        # 0. Check Kill-Switch
        if executor.risk_engine.is_killed:
            logger.warning("Kill-switch is active. Skipping trading cycle.")
            return

        # 1. Update Account Info (Equity) - single REST call
        try:
            balance = await binance_client.get_balance()
            equity = float(balance['total']['USDT'])
            free_balance = float(balance['free']['USDT'])
            unrealized_pnl = float(balance.get('info', {}).get('totalUnrealizedProfit', 0))
        except Exception as e:
            logger.error(f"Failed to fetch balance: {e}")
            return

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

        # 3. Fetch positions ONCE for all symbols - single REST call
        try:
            current_positions = await binance_client.fetch_positions()
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            return

        # 4. Process all symbols using WebSocket cached data (NO REST calls)
        for symbol in self.symbols:
            await self.process_symbol(symbol, equity, current_positions)

    async def process_symbol(self, symbol: str, equity: float, current_positions: list):
        # A. Get candle data from WebSocket cache (NO REST API call)
        df = await ws_client.get_candles(symbol, "1h")
        if df.empty or len(df) < 50:
            candle_count = len(df) if not df.empty else 0
            logger.warning(f"[{symbol}] Insufficient candle data ({candle_count}/50 needed)")
            return

        # B. Calculate Indicators
        df = indicators.add_all(df)

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

        # E. Execution (positions already fetched once per cycle)
        if signal["side"] != "NONE":
            await executor.execute_signal(signal, equity, current_positions)

# Global Engine
trading_engine = TradingEngine()
