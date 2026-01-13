import logging
import asyncio
from typing import Set, Dict, List
from bot.exchange.binance_client import binance_client
from bot.data.market_data import market_data
from bot.data.indicators import indicators
from bot.state.db import db
from bot.alerts.telegram import telegram_alerter

logger = logging.getLogger(__name__)


class ReconciliationLoop:
    def __init__(self, config: dict):
        self.config = config
        # Default to 180 seconds (3 min) to reduce API load
        self.interval_seconds = config.get("reconciliation", {}).get("interval_seconds", 180)
        self.auto_add_sl = config.get("reconciliation", {}).get("auto_add_sl", True)
        self.auto_add_tp = config.get("reconciliation", {}).get("auto_add_tp", True)
        self.atr_sl_multiplier = config.get("reconciliation", {}).get("atr_sl_multiplier", 2.0)
        self.atr_tp_multiplier = config.get("reconciliation", {}).get("atr_tp_multiplier", 3.0)
        self._running = False
        self._consecutive_errors = 0

    async def start(self):
        """Starts the reconciliation loop as a background task."""
        self._running = True
        logger.info(f"Starting reconciliation loop (interval: {self.interval_seconds}s)")

        while self._running:
            try:
                await self.run_reconciliation()
                self._consecutive_errors = 0
            except Exception as e:
                self._consecutive_errors += 1
                # If rate limited, back off exponentially
                if "Rate limited" in str(e) or "418" in str(e) or "429" in str(e):
                    wait_time = min(self.interval_seconds * (2 ** self._consecutive_errors), 300)
                    logger.warning(f"Reconciliation backing off for {wait_time}s due to rate limit")
                    await asyncio.sleep(wait_time)
                    continue
                logger.error(f"Reconciliation error: {e}")
                await telegram_alerter.alert_error("Reconciliation", str(e))

            await asyncio.sleep(self.interval_seconds)

    def stop(self):
        """Stops the reconciliation loop."""
        self._running = False
        logger.info("Reconciliation loop stopped")

    async def run_reconciliation(self):
        """
        Main reconciliation logic:
        1. For each open position, verify SL/TP orders exist
        2. If SL missing, analyze market and add reasonable stop-loss
        3. For orphan reduceOnly orders (no position), cancel them
        4. Log and alert on anomalies
        """
        logger.info("Running reconciliation check...")

        # Fetch current state from exchange
        try:
            positions = await binance_client.fetch_positions()
            all_orders = await binance_client.fetch_open_orders()
        except Exception as e:
            logger.error(f"Failed to fetch positions/orders for reconciliation: {e}")
            return

        # Build position symbol set
        position_symbols: Set[str] = {p['symbol'] for p in positions}

        # Build order map by symbol
        orders_by_symbol: Dict[str, List[dict]] = {}
        for order in all_orders:
            symbol = order['symbol']
            if symbol not in orders_by_symbol:
                orders_by_symbol[symbol] = []
            orders_by_symbol[symbol].append(order)

        # 1. Check positions have protective orders
        for position in positions:
            symbol = position['symbol']
            side = position.get('side', '').lower()  # 'long' or 'short'
            contracts = abs(float(position.get('contracts', 0)))
            entry_price = float(position.get('entryPrice', 0))

            if contracts == 0:
                continue

            orders = orders_by_symbol.get(symbol, [])

            has_stop = any(
                o.get('type') in ['STOP_MARKET', 'STOP', 'stop_market', 'stop']
                and o.get('reduceOnly', False)
                for o in orders
            )
            has_tp = any(
                o.get('type') in ['TAKE_PROFIT_MARKET', 'TAKE_PROFIT', 'take_profit_market', 'take_profit']
                and o.get('reduceOnly', False)
                for o in orders
            )

            if not has_stop:
                issue = "Missing STOP_MARKET order"
                logger.warning(f"[{symbol}] {issue}")

                if self.auto_add_sl:
                    action = await self._add_stop_loss(symbol, side, contracts, entry_price)
                else:
                    action = "Alert only (auto_add_sl disabled)"

                await telegram_alerter.alert_reconciliation_issue(symbol, issue, action)
                await self._log_reconciliation_action(symbol, issue, action)

            if not has_tp:
                issue = "Missing TAKE_PROFIT_MARKET order"
                logger.warning(f"[{symbol}] {issue}")

                if self.auto_add_tp:
                    action = await self._add_take_profit(symbol, side, contracts, entry_price)
                else:
                    action = "Alert only (auto_add_tp disabled)"

                await telegram_alerter.alert_reconciliation_issue(symbol, issue, action)
                await self._log_reconciliation_action(symbol, issue, action)

        # 2. Cancel orphan orders (reduceOnly orders with no position)
        for symbol, orders in orders_by_symbol.items():
            if symbol not in position_symbols:
                for order in orders:
                    if order.get('reduceOnly', False):
                        order_id = order.get('id')
                        logger.warning(f"[{symbol}] Orphan reduceOnly order found: {order_id}")
                        try:
                            await binance_client.cancel_order(order_id, symbol)
                            await self._log_reconciliation_action(
                                symbol,
                                f"Orphan order {order_id}",
                                "Cancelled"
                            )
                        except Exception as e:
                            logger.error(f"Failed to cancel orphan order: {e}")

        logger.info("Reconciliation check complete")

    async def _add_stop_loss(self, symbol: str, side: str, size: float, entry_price: float) -> str:
        """
        Analyzes market and adds a reasonable stop-loss based on ATR.
        Returns action description.
        """
        try:
            # Fetch recent candles for ATR calculation
            df = await market_data.get_candles(symbol, "1h", limit=50)
            if df.empty:
                return "Failed: Could not fetch market data"

            indicators.add_all(df)

            # Get ATR for stop distance
            atr = df['ATR_14'].iloc[-1] if 'ATR_14' in df.columns else None
            if atr is None or atr <= 0:
                # Fallback: use 2% of entry price
                atr = entry_price * 0.02

            # Calculate stop price based on position side
            stop_distance = atr * self.atr_sl_multiplier
            if side == 'long':
                stop_price = entry_price - stop_distance
                sl_side = 'sell'
            else:  # short
                stop_price = entry_price + stop_distance
                sl_side = 'buy'

            # Round stop price to reasonable precision
            stop_price = round(stop_price, 2)

            logger.info(f"[{symbol}] Adding SL at {stop_price} (ATR: {atr:.2f}, Entry: {entry_price})")

            # Place stop-loss order
            await binance_client.create_order(
                symbol, 'STOP_MARKET', sl_side, size, None,
                {'stopPrice': stop_price, 'reduceOnly': True}
            )

            return f"Added SL at ${stop_price:,.2f} (ATR-based)"

        except Exception as e:
            logger.error(f"Failed to add stop-loss for {symbol}: {e}")
            return f"Failed: {str(e)}"

    async def _add_take_profit(self, symbol: str, side: str, size: float, entry_price: float) -> str:
        """
        Analyzes market and adds a reasonable take-profit based on ATR.
        Returns action description.
        """
        try:
            # Fetch recent candles for ATR calculation
            df = await market_data.get_candles(symbol, "1h", limit=50)
            if df.empty:
                return "Failed: Could not fetch market data"

            indicators.add_all(df)

            # Get ATR for TP distance
            atr = df['ATR_14'].iloc[-1] if 'ATR_14' in df.columns else None
            if atr is None or atr <= 0:
                # Fallback: use 3% of entry price
                atr = entry_price * 0.03

            # Calculate TP price based on position side
            tp_distance = atr * self.atr_tp_multiplier
            if side == 'long':
                tp_price = entry_price + tp_distance
                tp_side = 'sell'
            else:  # short
                tp_price = entry_price - tp_distance
                tp_side = 'buy'

            # Round TP price to reasonable precision
            tp_price = round(tp_price, 2)

            logger.info(f"[{symbol}] Adding TP at {tp_price} (ATR: {atr:.2f}, Entry: {entry_price})")

            # Place take-profit order
            await binance_client.create_order(
                symbol, 'TAKE_PROFIT_MARKET', tp_side, size, None,
                {'stopPrice': tp_price, 'reduceOnly': True}
            )

            return f"Added TP at ${tp_price:,.2f} (ATR-based)"

        except Exception as e:
            logger.error(f"Failed to add take-profit for {symbol}: {e}")
            return f"Failed: {str(e)}"

    async def _log_reconciliation_action(self, symbol: str, issue: str, action: str):
        """Logs reconciliation action to database."""
        await db.execute(
            "INSERT INTO system_errors (component, message) VALUES (?, ?)",
            ("reconciliation", f"[{symbol}] {issue} -> {action}")
        )


def create_reconciliation_loop(config: dict) -> ReconciliationLoop:
    """Factory function to create reconciliation loop."""
    return ReconciliationLoop(config)
