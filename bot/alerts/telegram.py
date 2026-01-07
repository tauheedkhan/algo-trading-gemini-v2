import os
import logging
import httpx
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


class TelegramAlerter:
    def __init__(self):
        load_dotenv()
        self.bot_token = os.getenv("TG_BOT_TOKEN")
        self.chat_id = os.getenv("TG_CHAT_ID")
        self._enabled = bool(self.bot_token and self.chat_id)

        if not self._enabled:
            logger.warning("Telegram alerts disabled: TG_BOT_TOKEN or TG_CHAT_ID not set")

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    async def send_message(self, message: str, parse_mode: str = "HTML") -> bool:
        """Sends a message to the configured Telegram chat."""
        if not self._enabled:
            logger.debug(f"Telegram disabled, would send: {message[:100]}...")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": parse_mode
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, timeout=10)
                if response.status_code == 200:
                    logger.debug("Telegram message sent successfully")
                    return True
                else:
                    logger.error(f"Telegram API error: {response.status_code} - {response.text}")
                    return False
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False

    async def alert_trade_opened(self, symbol: str, side: str, size: float, entry_price: float,
                                  stop_loss: float, take_profit: float):
        """Alert when a new trade is opened."""
        emoji = "🟢" if side == "BUY" else "🔴"
        message = (
            f"{emoji} <b>Trade Opened</b>\n"
            f"Symbol: <code>{symbol}</code>\n"
            f"Side: {side}\n"
            f"Size: {size:.4f}\n"
            f"Entry: ${entry_price:,.2f}\n"
            f"SL: ${stop_loss:,.2f}\n"
            f"TP: ${take_profit:,.2f}"
        )
        await self.send_message(message)

    async def alert_trade_closed(self, symbol: str, side: str, pnl: float, exit_reason: str):
        """Alert when a trade is closed."""
        emoji = "💰" if pnl > 0 else "💸"
        pnl_sign = "+" if pnl > 0 else ""
        message = (
            f"{emoji} <b>Trade Closed</b>\n"
            f"Symbol: <code>{symbol}</code>\n"
            f"Side: {side}\n"
            f"PnL: {pnl_sign}${pnl:,.2f}\n"
            f"Reason: {exit_reason}"
        )
        await self.send_message(message)

    async def alert_kill_switch(self, reason: str):
        """Alert when kill-switch is activated."""
        message = (
            f"🚨 <b>KILL-SWITCH ACTIVATED</b> 🚨\n\n"
            f"Reason: {reason}\n\n"
            f"<i>All trading has been halted. Manual intervention required.</i>"
        )
        await self.send_message(message)

    async def alert_error(self, component: str, error: str):
        """Alert on critical errors."""
        message = (
            f"⚠️ <b>Error in {component}</b>\n"
            f"<code>{error[:500]}</code>"
        )
        await self.send_message(message)

    async def alert_reconciliation_issue(self, symbol: str, issue: str, action: str):
        """Alert on reconciliation anomalies."""
        message = (
            f"🔧 <b>Reconciliation Alert</b>\n"
            f"Symbol: <code>{symbol}</code>\n"
            f"Issue: {issue}\n"
            f"Action: {action}"
        )
        await self.send_message(message)

    async def send_heartbeat(self, status: dict):
        """Send hourly heartbeat."""
        daily_pnl = status.get('daily_pnl', 0)
        pnl_emoji = "📈" if daily_pnl >= 0 else "📉"
        pnl_sign = "+" if daily_pnl > 0 else ""

        message = (
            f"💓 <b>Heartbeat</b>\n"
            f"Status: {status.get('status', 'OK')}\n"
            f"Equity: ${status.get('equity', 0):,.2f}\n"
            f"Open Positions: {status.get('open_positions', 0)}\n"
            f"{pnl_emoji} Daily PnL: {pnl_sign}${daily_pnl:,.2f}"
        )
        await self.send_message(message)

    async def alert_startup(self, config_summary: dict):
        """Alert when bot starts up."""
        message = (
            f"🚀 <b>Trading Bot Started</b>\n"
            f"Mode: {config_summary.get('mode', 'UNKNOWN')}\n"
            f"Symbols: {', '.join(config_summary.get('symbols', []))}\n"
            f"Leverage: {config_summary.get('leverage', 1)}x\n"
            f"Risk/Trade: {config_summary.get('risk_pct', 2)}%"
        )
        await self.send_message(message)

    async def send_config(self, config: dict, env_type: str):
        """Send full configuration to Telegram on startup."""
        risk = config.get("risk", {})
        strategies = config.get("strategies", {})
        regime = config.get("regime", {})
        timeframes = config.get("timeframes", {})

        message = (
            f"⚙️ <b>Loaded Configuration</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Environment:</b> {env_type.upper()}\n"
            f"<b>Symbols:</b> {', '.join(config.get('symbols', []))}\n\n"

            f"<b>📊 Timeframes:</b>\n"
            f"  • Trend: {timeframes.get('trend', 'N/A')}\n"
            f"  • Setup: {timeframes.get('setup', 'N/A')}\n"
            f"  • Entry: {timeframes.get('entry', 'N/A')}\n\n"

            f"<b>⚠️ Risk Management:</b>\n"
            f"  • Risk/Trade: {risk.get('target_risk_per_trade_percent', 0) * 100:.1f}%\n"
            f"  • Max Position: {risk.get('max_position_percent', 0) * 100:.1f}%\n"
            f"  • Max Open Positions: {risk.get('max_open_positions', 0)}\n"
            f"  • Max Daily Drawdown: {risk.get('max_drawdown_daily_percent', 0)}%\n"
            f"  • Leverage: {risk.get('leverage', 1)}x\n"
            f"  • Margin Mode: {risk.get('margin_mode', 'N/A')}\n\n"

            f"<b>📈 Strategies:</b>\n"
            f"  • Trend Pullback: {'✅' if strategies.get('trend_pullback', {}).get('enabled') else '❌'}\n"
            f"  • Range Mean Rev: {'✅' if strategies.get('range_mean_reversion', {}).get('enabled') else '❌'}\n\n"

            f"<b>🎯 Regime Thresholds:</b>\n"
            f"  • Trend ADX: {regime.get('trend_adx_threshold', 'N/A')}\n"
            f"  • Range ADX: {regime.get('range_adx_threshold', 'N/A')}"
        )
        await self.send_message(message)

    async def alert_shutdown(self, reason: str, positions_closed: int = 0):
        """Alert when bot shuts down."""
        message = (
            f"🛑 <b>Trading Bot Stopped</b>\n"
            f"Reason: {reason}\n"
            f"Positions Closed: {positions_closed}"
        )
        await self.send_message(message)


# Global instance
telegram_alerter = TelegramAlerter()
