import logging
import asyncio
from typing import Optional
from bot.exchange.binance_client import binance_client
from bot.state.db import db
from bot.risk.risk_engine import RiskEngine
from bot.alerts.telegram import telegram_alerter

logger = logging.getLogger(__name__)


class Executor:
    def __init__(self, config: dict):
        self.risk_engine = RiskEngine(config)
        self.client = binance_client
        self.leverage_set_cache = set()
        self.max_retries = 3
        self.retry_delay = 1.0  # seconds

    async def execute_signal(self, signal: dict, equity: float, current_positions: list) -> Optional[dict]:
        """
        Orchestrates the execution of a trading signal with retry logic.

        Returns executed order info or None if execution failed/skipped.
        """
        symbol = signal.get("symbol")
        side = signal.get("side")

        if not symbol or side == "NONE":
            return None

        # Check kill-switch
        if self.risk_engine.is_killed:
            logger.warning(f"Kill-switch active, rejecting signal for {symbol}")
            return None

        # Check if we already have a position in this symbol
        symbol_clean = symbol.replace("/", "")
        for pos in current_positions:
            if pos.get('symbol') == symbol_clean:
                logger.info(f"[{symbol}] Already have open position, skipping signal")
                return None

        # Risk Check
        if not self.risk_engine.check_new_trade_allowed(current_positions):
            return None

        entry_price = signal.get("entry_price")
        stop_loss = signal.get("stop_loss")
        take_profit = signal.get("take_profit")
        strategy = signal.get("reason", "Unknown")
        regime = signal.get("regime", "UNKNOWN")

        # Validate that stop_loss and take_profit are present
        if stop_loss is None or take_profit is None:
            logger.error(f"[{symbol}] Signal missing SL or TP - stop_loss={stop_loss}, take_profit={take_profit}")
            return None

        size = self.risk_engine.calculate_position_size(
            equity,
            entry_price,
            {"stop_loss": stop_loss}
        )

        if size <= 0:
            logger.warning(f"[{symbol}] Calculated position size is 0. Aborting.")
            return None

        logger.info(f"EXECUTING [{symbol}] {side} Size: {size:.4f} @ {entry_price} SL: {stop_loss} TP: {take_profit}")

        try:
            # Set Leverage (idempotent)
            await self._ensure_leverage(symbol)

            # Place Market Entry with retry
            order_side = 'buy' if side == "BUY" else 'sell'
            order = await self._execute_with_retry(
                self.client.create_order,
                symbol, 'market', order_side, size
            )

            if not order:
                raise Exception("Entry order failed after retries")

            filled_price = order.get('average') or entry_price
            order_id = order.get('id')

            # Place SL/TP with retry
            sl_side = 'sell' if side == "BUY" else 'buy'

            sl_order = await self._execute_with_retry(
                self.client.create_order,
                symbol, 'STOP_MARKET', sl_side, size, None,
                {'stopPrice': stop_loss, 'reduceOnly': True}
            )

            tp_order = await self._execute_with_retry(
                self.client.create_order,
                symbol, 'TAKE_PROFIT_MARKET', sl_side, size, None,
                {'stopPrice': take_profit, 'reduceOnly': True}
            )

            # Verify protective orders were placed (reconciliation loop will auto-add if missing)
            if not sl_order:
                logger.error(f"[{symbol}] Failed to place SL order - reconciliation will auto-add")

            if not tp_order:
                logger.warning(f"[{symbol}] Failed to place TP order - reconciliation will auto-add")

            # DB Logging
            await db.execute(
                """INSERT INTO trades
                   (symbol, strategy, side, entry_price, size, regime_at_entry, entry_time)
                   VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
                (symbol, strategy, side, filled_price, size, regime)
            )

            # Send Telegram alert
            await telegram_alerter.alert_trade_opened(
                symbol=symbol,
                side=side,
                size=size,
                entry_price=filled_price,
                stop_loss=stop_loss,
                take_profit=take_profit
            )

            logger.info(f"Execution Complete for {symbol} (Order ID: {order_id})")
            return order

        except Exception as e:
            logger.error(f"Execution Failed for {symbol}: {e}")
            await telegram_alerter.alert_error("Executor", f"{symbol}: {e}")
            await db.execute(
                "INSERT INTO system_errors (component, message) VALUES (?, ?)",
                ("executor", f"[{symbol}] {e}")
            )
            return None

    async def _ensure_leverage(self, symbol: str):
        """Sets leverage if not already cached."""
        if symbol not in self.leverage_set_cache:
            await self.client.set_leverage(symbol, self.risk_engine.leverage)
            self.leverage_set_cache.add(symbol)

    async def _execute_with_retry(self, func, *args, **kwargs) -> Optional[dict]:
        """Executes a function with exponential backoff retry."""
        last_error = None

        for attempt in range(self.max_retries):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_error = e
                wait_time = self.retry_delay * (2 ** attempt)
                logger.warning(f"Retry {attempt + 1}/{self.max_retries} after {wait_time}s: {e}")
                await asyncio.sleep(wait_time)

        logger.error(f"All {self.max_retries} retries failed: {last_error}")
        return None


# Will be re-initialized with proper config
executor = Executor({})
