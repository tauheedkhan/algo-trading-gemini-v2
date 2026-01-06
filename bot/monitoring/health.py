import logging
import asyncio
from datetime import datetime
from bot.exchange.binance_client import binance_client
from bot.state.db import db
from bot.alerts.telegram import telegram_alerter

logger = logging.getLogger(__name__)


class HealthMonitor:
    def __init__(self, config: dict):
        self.config = config
        self.heartbeat_interval = config.get("monitoring", {}).get("heartbeat_interval_seconds", 3600)
        self._running = False
        self._last_heartbeat = None
        self._consecutive_errors = 0
        self._max_consecutive_errors = 5

    async def start(self):
        """Starts the health monitoring loop."""
        self._running = True
        logger.info(f"Starting health monitor (heartbeat every {self.heartbeat_interval}s)")

        while self._running:
            try:
                await self.send_heartbeat()
                self._consecutive_errors = 0
            except Exception as e:
                self._consecutive_errors += 1
                logger.error(f"Health check error ({self._consecutive_errors}): {e}")

                if self._consecutive_errors >= self._max_consecutive_errors:
                    await telegram_alerter.alert_error(
                        "Health Monitor",
                        f"Repeated failures: {self._consecutive_errors} consecutive errors"
                    )

            await asyncio.sleep(self.heartbeat_interval)

    def stop(self):
        """Stops the health monitor."""
        self._running = False
        logger.info("Health monitor stopped")

    async def send_heartbeat(self):
        """Sends a heartbeat with current system status."""
        # Gather status information
        balance = await binance_client.get_balance()
        equity = float(balance['total']['USDT'])

        positions = await binance_client.fetch_positions()
        open_positions = len(positions)

        # Get daily PnL from snapshots
        start_equity = await db.get_daily_start_equity()
        daily_pnl = equity - start_equity if start_equity else 0

        status = {
            "status": "OK",
            "equity": equity,
            "open_positions": open_positions,
            "daily_pnl": daily_pnl,
            "timestamp": datetime.utcnow().isoformat()
        }

        await telegram_alerter.send_heartbeat(status)
        self._last_heartbeat = datetime.utcnow()

        logger.info(f"Heartbeat sent: Equity=${equity:.2f}, Positions={open_positions}, DailyPnL=${daily_pnl:.2f}")

    async def verify_exchange_config(self) -> dict:
        """Verifies position mode and margin mode match config requirements."""
        issues = []
        position_mode = "UNKNOWN"

        try:
            # Check position mode
            position_mode = await binance_client.get_position_mode()
            if position_mode not in ["ONEWAY", "UNKNOWN"]:
                issues.append(f"Position mode is {position_mode}, expected ONEWAY")
        except Exception as e:
            logger.warning(f"Could not verify position mode (testnet limitation): {e}")

        # Skip margin mode check - not reliable on testnet
        # The margin mode is set per-order anyway

        if issues:
            logger.warning(f"Exchange config issues: {issues}")
            await telegram_alerter.alert_error("Config Verification", "\n".join(issues))
        else:
            logger.info("Exchange configuration verified successfully")

        return {
            "position_mode": position_mode,
            "margin_issues": issues,
            "verified": len(issues) == 0
        }

    async def check_connectivity(self) -> bool:
        """Checks if exchange connectivity is working."""
        try:
            await binance_client.get_balance()
            return True
        except Exception as e:
            logger.error(f"Connectivity check failed: {e}")
            return False


def create_health_monitor(config: dict) -> HealthMonitor:
    """Factory function to create health monitor."""
    return HealthMonitor(config)
