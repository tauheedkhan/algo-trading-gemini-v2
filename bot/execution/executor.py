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
        self.margin_mode_cache = set()  # Track symbols with margin mode set
        self.margin_mode = config.get('risk', {}).get('margin_mode', 'ISOLATED').upper()
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

        # Hard safety: never place a position without both SL and TP
        if entry_price is None or stop_loss is None or take_profit is None:
            logger.warning(f"[{symbol}] Missing entry/SL/TP in signal. Aborting execution.")
            return None

        # Validate SL/TP are on correct sides of entry
        side_clean = str(side).upper()
        if side_clean in ("BUY", "LONG"):
            if not (stop_loss < entry_price < take_profit):
                logger.warning(f"[{symbol}] Invalid BUY levels: SL={stop_loss}, entry={entry_price}, TP={take_profit}")
                return None
        elif side_clean in ("SELL", "SHORT"):
            if not (take_profit < entry_price < stop_loss):
                logger.warning(f"[{symbol}] Invalid SELL levels: SL={stop_loss}, entry={entry_price}, TP={take_profit}")
                return None

        confidence = signal.get('confidence')
        atr = signal.get('atr')
        strategy = signal.get("reason", "Unknown")
        regime = signal.get("regime", "UNKNOWN")

        # Convert numpy floats to Python floats (fixes JSON serialization issues)
        if hasattr(entry_price, 'item'):
            entry_price = entry_price.item()
        if hasattr(stop_loss, 'item'):
            stop_loss = stop_loss.item()
        if hasattr(take_profit, 'item'):
            take_profit = take_profit.item()

        # Validate that stop_loss and take_profit are present
        if stop_loss is None or take_profit is None:
            logger.error(f"[{symbol}] Signal missing SL or TP - stop_loss={stop_loss}, take_profit={take_profit}")
            return None

        # Fetch available margin from exchange
        try:
            balance = await self.client.get_balance()
            available_margin = balance.get('free', {}).get('USDT', 0)
            logger.info(f"[{symbol}] Available margin: ${available_margin:.2f}")
        except Exception as e:
            logger.warning(f"[{symbol}] Could not fetch available margin: {e}")
            available_margin = None

        size = self.risk_engine.calculate_position_size(
            equity,
            entry_price,
            {"stop_loss": stop_loss, "confidence": confidence, "atr": atr},
            available_margin=available_margin
        )

        if size <= 0:
            logger.warning(f"[{symbol}] Calculated position size is 0 or insufficient margin. Aborting.")
            return None

        logger.info(f"EXECUTING [{symbol}] {side} Size: {size:.4f} @ {entry_price} SL: {stop_loss} TP: {take_profit}")

        try:
            # Set Margin Mode and Leverage (idempotent)
            await self._ensure_margin_mode(symbol)
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

            logger.info(f"[{symbol}] Placing SL order: side={sl_side}, size={size}, stopPrice={stop_loss}")
            sl_order = await self._execute_with_retry(
                self.client.create_order,
                symbol, 'STOP_MARKET', sl_side, size, None,
                {'stopPrice': stop_loss, 'reduceOnly': True}
            )

            logger.info(f"[{symbol}] Placing TP order: side={sl_side}, size={size}, stopPrice={take_profit}")
            tp_order = await self._execute_with_retry(
                self.client.create_order,
                symbol, 'TAKE_PROFIT_MARKET', sl_side, size, None,
                {'stopPrice': take_profit, 'reduceOnly': True}
            )

            # Verify protective orders were placed (reconciliation loop will auto-add if missing)
            if not sl_order:
                logger.error(f"[{symbol}] Failed to place SL order at {stop_loss} - reconciliation will auto-add")
            else:
                logger.info(f"[{symbol}] SL order placed successfully: {sl_order.get('id')}")

            if not tp_order:
                logger.error(f"[{symbol}] Failed to place TP order at {take_profit} - reconciliation will auto-add")
            else:
                logger.info(f"[{symbol}] TP order placed successfully: {tp_order.get('id')}")

            # DB Logging - include SL/TP prices for tracking
            await db.execute(
                """INSERT INTO trades
                   (symbol, strategy, side, entry_price, size, regime_at_entry, entry_time, sl_price, tp_price)
                   VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)""",
                (symbol, strategy, side, filled_price, size, regime, stop_loss, take_profit)
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

    async def _ensure_margin_mode(self, symbol: str):
        """Sets margin mode (ISOLATED/CROSS) if not already cached."""
        if symbol not in self.margin_mode_cache:
            success = await self.client.set_margin_mode(symbol, self.margin_mode)
            if success:
                self.margin_mode_cache.add(symbol)
                logger.info(f"[{symbol}] Margin mode set to {self.margin_mode}")
            else:
                logger.warning(f"[{symbol}] Failed to set margin mode to {self.margin_mode}")

    async def _ensure_leverage(self, symbol: str):
        """Sets leverage if not already cached."""
        if symbol not in self.leverage_set_cache:
            await self.client.set_leverage(symbol, self.risk_engine.leverage)
            self.leverage_set_cache.add(symbol)

    async def _execute_with_retry(self, func, *args, **kwargs) -> Optional[dict]:
        """Executes a function with exponential backoff retry."""
        last_error = None
        func_name = getattr(func, '__name__', str(func))

        for attempt in range(self.max_retries):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_error = e
                wait_time = self.retry_delay * (2 ** attempt)
                logger.warning(f"Retry {attempt + 1}/{self.max_retries} for {func_name} after {wait_time}s: {e}")
                logger.warning(f"  Args: {args[:3]}...")  # Log first 3 args for context
                await asyncio.sleep(wait_time)

        logger.error(f"All {self.max_retries} retries failed for {func_name}: {last_error}")
        return None


# Will be re-initialized with proper config
executor = Executor({})
