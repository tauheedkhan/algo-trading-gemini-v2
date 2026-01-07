import asyncio
import signal
import sys
import os
import uvicorn
from dotenv import load_dotenv

from bot.core.logging_config import setup_logging
from bot.core.config import load_config
from bot.core.engine import trading_engine
from bot.api.dashboard_api import app
from bot.exchange.binance_client import binance_client
from bot.state.db import db
from bot.monitoring.reconciliation import create_reconciliation_loop
from bot.monitoring.health import create_health_monitor
from bot.alerts.telegram import telegram_alerter

# Setup logging first (before any other imports that might log)
setup_logging(log_level="INFO", use_colors=True)

import logging
logger = logging.getLogger(__name__)

# Global state for graceful shutdown
shutdown_event = asyncio.Event()
reconciliation_loop = None
health_monitor = None


async def start_api():
    """Starts the FastAPI dashboard server."""
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


async def graceful_shutdown(reason: str = "User requested"):
    """
    Performs graceful shutdown:
    1. Stop all loops
    2. Close all positions
    3. Cancel all pending orders
    4. Send shutdown alert
    5. Close connections
    """
    logger.info(f"Initiating graceful shutdown: {reason}")

    # Stop monitoring loops
    if reconciliation_loop:
        reconciliation_loop.stop()
    if health_monitor:
        health_monitor.stop()

    positions_closed = 0
    config = load_config("config.yaml")
    graceful_config = config.get("graceful_shutdown", {})

    try:
        # Close all positions if configured
        if graceful_config.get("close_positions", True):
            logger.info("Closing all open positions...")
            positions = await binance_client.fetch_positions()

            for position in positions:
                symbol = position['symbol']
                side = position.get('side', '').lower()
                contracts = abs(float(position.get('contracts', 0)))

                if contracts == 0:
                    continue

                close_side = 'sell' if side == 'long' else 'buy'

                try:
                    await binance_client.create_order(
                        symbol, 'market', close_side, contracts, None,
                        {'reduceOnly': True}
                    )
                    positions_closed += 1
                    logger.info(f"Closed position for {symbol}")
                except Exception as e:
                    logger.error(f"Failed to close position for {symbol}: {e}")

        # Cancel all pending orders if configured
        if graceful_config.get("cancel_orders", True):
            logger.info("Cancelling all open orders...")
            orders = await binance_client.fetch_open_orders()

            for order in orders:
                try:
                    await binance_client.cancel_order(order['id'], order['symbol'])
                    logger.info(f"Cancelled order {order['id']}")
                except Exception as e:
                    logger.error(f"Failed to cancel order {order['id']}: {e}")

    except Exception as e:
        logger.error(f"Error during shutdown: {e}")

    # Log shutdown event
    await db.log_system_event("SHUTDOWN", reason)

    # Send Telegram alert
    await telegram_alerter.alert_shutdown(reason, positions_closed)

    # Close connections
    await binance_client.close()
    await db.close()

    logger.info(f"Graceful shutdown complete. Closed {positions_closed} positions.")


def signal_handler(sig, frame):
    """Handle shutdown signals."""
    sig_name = signal.Signals(sig).name
    logger.info(f"Received signal {sig_name}")
    shutdown_event.set()


def print_config(config: dict, env_type: str):
    """Print loaded configuration to console."""
    risk = config.get("risk", {})
    strategies = config.get("strategies", {})
    regime = config.get("regime", {})
    timeframes = config.get("timeframes", {})

    print("\n" + "=" * 50)
    print("           LOADED CONFIGURATION")
    print("=" * 50)
    print(f"Environment: {env_type.upper()}")
    print(f"Symbols: {', '.join(config.get('symbols', []))}")
    print("-" * 50)
    print("TIMEFRAMES:")
    print(f"  Trend:    {timeframes.get('trend', 'N/A')}")
    print(f"  Setup:    {timeframes.get('setup', 'N/A')}")
    print(f"  Entry:    {timeframes.get('entry', 'N/A')}")
    print("-" * 50)
    print("RISK MANAGEMENT:")
    print(f"  Risk/Trade:         {risk.get('target_risk_per_trade_percent', 0) * 100:.1f}%")
    print(f"  Max Position:       {risk.get('max_position_percent', 0) * 100:.1f}%")
    print(f"  Max Open Positions: {risk.get('max_open_positions', 0)}")
    print(f"  Max Daily Drawdown: {risk.get('max_drawdown_daily_percent', 0)}%")
    print(f"  Leverage:           {risk.get('leverage', 1)}x")
    print(f"  Margin Mode:        {risk.get('margin_mode', 'N/A')}")
    print("-" * 50)
    print("STRATEGIES:")
    print(f"  Trend Pullback:     {'Enabled' if strategies.get('trend_pullback', {}).get('enabled') else 'Disabled'}")
    print(f"  Range Mean Rev:     {'Enabled' if strategies.get('range_mean_reversion', {}).get('enabled') else 'Disabled'}")
    print("-" * 50)
    print("REGIME THRESHOLDS:")
    print(f"  Trend ADX:          {regime.get('trend_adx_threshold', 'N/A')}")
    print(f"  Range ADX:          {regime.get('range_adx_threshold', 'N/A')}")
    print("=" * 50 + "\n")


async def main():
    global reconciliation_loop, health_monitor

    load_dotenv()
    config = load_config("config.yaml")

    # Log startup
    env_type = os.getenv("BINANCE_ENV", "testnet")
    logger.info(f"Starting Trading Bot in {env_type.upper()} mode")

    # Print loaded config to console
    print_config(config, env_type)

    # Initialize database
    await db.connect()
    await db.log_system_event("START", f"Bot started in {env_type} mode")

    # Initialize exchange client
    await binance_client.initialize()

    # Create monitoring components
    reconciliation_loop = create_reconciliation_loop(config)
    health_monitor = create_health_monitor(config)

    # Verify exchange configuration (non-critical on testnet)
    try:
        verification = await health_monitor.verify_exchange_config()
        if not verification['verified']:
            logger.warning(f"Exchange config issues detected: {verification['margin_issues']}")
    except Exception as e:
        logger.warning(f"Could not verify exchange config (testnet limitation): {e}")

    # Send startup alert and config to Telegram
    await telegram_alerter.alert_startup({
        "mode": env_type.upper(),
        "symbols": config.get("symbols", []),
        "leverage": config.get("risk", {}).get("leverage", 1),
        "risk_pct": config.get("risk", {}).get("target_risk_per_trade_percent", 0.02) * 100
    })
    await telegram_alerter.send_config(config, env_type)

    # Create tasks
    tasks = [
        asyncio.create_task(trading_engine.start(), name="trading_engine"),
        asyncio.create_task(start_api(), name="api"),
        asyncio.create_task(reconciliation_loop.start(), name="reconciliation"),
        asyncio.create_task(health_monitor.start(), name="health"),
    ]

    # Wait for shutdown signal
    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass

    # Cancel all running tasks
    logger.info("Stopping all tasks...")
    for task in tasks:
        task.cancel()

    # Wait for tasks to finish cancelling
    await asyncio.gather(*tasks, return_exceptions=True)

    # Perform graceful shutdown
    await graceful_shutdown("Signal received")


if __name__ == "__main__":
    # Setup signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by keyboard interrupt.")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
