import logging
import asyncio
from typing import Dict, Set
from bot.exchange.binance_client import binance_client
from bot.state.db import db
from bot.alerts.telegram import telegram_alerter

logger = logging.getLogger(__name__)


class PositionMonitor:
    """
    Monitors positions and detects closures to track TP/SL hits.
    Runs in background and updates trade records when positions close.
    """

    def __init__(self, config: dict):
        self.config = config
        # Default to 120 seconds to reduce API calls (positions don't change that fast)
        self.interval_seconds = config.get("monitoring", {}).get("position_check_interval", 120)
        self._running = False
        self._known_positions: Dict[str, dict] = {}  # symbol -> position info
        self._consecutive_errors = 0
        self._max_consecutive_errors = 5

    async def start(self):
        """Starts the position monitoring loop."""
        self._running = True
        logger.info(f"Starting position monitor (interval: {self.interval_seconds}s)")

        # Initial snapshot
        await self._update_known_positions()

        while self._running:
            try:
                await self._check_position_changes()
                self._consecutive_errors = 0
            except Exception as e:
                self._consecutive_errors += 1
                # If rate limited, increase wait time exponentially
                if "Rate limited" in str(e) or "418" in str(e) or "429" in str(e):
                    wait_time = min(self.interval_seconds * (2 ** self._consecutive_errors), 300)
                    logger.warning(f"Position monitor backing off for {wait_time}s due to rate limit")
                    await asyncio.sleep(wait_time)
                    continue
                logger.error(f"Position monitor error: {e}")

            await asyncio.sleep(self.interval_seconds)

    def stop(self):
        """Stops the position monitor."""
        self._running = False
        logger.info("Position monitor stopped")

    async def _update_known_positions(self):
        """Updates the known positions from exchange."""
        try:
            positions = await binance_client.fetch_positions()
            self._known_positions = {
                p['symbol']: {
                    'side': p['side'],
                    'contracts': p['contracts'],
                    'entryPrice': p['entryPrice'],
                    'unrealizedPnl': p['unrealizedPnl']
                }
                for p in positions
            }
            logger.debug(f"Known positions updated: {list(self._known_positions.keys())}")
        except Exception as e:
            logger.error(f"Failed to update known positions: {e}")

    async def _check_position_changes(self):
        """Checks for position changes and handles closures."""
        try:
            current_positions = await binance_client.fetch_positions()
            current_symbols: Set[str] = {p['symbol'] for p in current_positions}
            known_symbols: Set[str] = set(self._known_positions.keys())

            # Find closed positions (were known, now gone)
            closed_symbols = known_symbols - current_symbols

            for symbol in closed_symbols:
                await self._handle_position_closed(symbol)

            # Update known positions
            self._known_positions = {
                p['symbol']: {
                    'side': p['side'],
                    'contracts': p['contracts'],
                    'entryPrice': p['entryPrice'],
                    'unrealizedPnl': p['unrealizedPnl']
                }
                for p in current_positions
            }

        except Exception as e:
            logger.error(f"Error checking position changes: {e}")

    async def _handle_position_closed(self, symbol: str):
        """Handles a closed position - determines exit reason and updates DB."""
        logger.info(f"[{symbol}] Position closed detected")

        # Convert symbol format (BTCUSDT -> BTC/USDT)
        symbol_formatted = self._format_symbol(symbol)

        # Find the open trade in DB (no exit_price yet)
        trade = await db.fetch_one(
            """SELECT id, symbol, side, entry_price, size, sl_price, tp_price
               FROM trades
               WHERE symbol = ? AND exit_price IS NULL
               ORDER BY entry_time DESC LIMIT 1""",
            (symbol_formatted,)
        )

        if not trade:
            logger.warning(f"[{symbol}] No matching open trade found in DB")
            return

        trade_id = trade['id']
        entry_price = trade['entry_price']
        size = trade['size']
        side = trade['side']
        sl_price = trade['sl_price']
        tp_price = trade['tp_price']

        # Fetch recent fills to determine exit price, fees, and order type
        exit_info = await self._get_exit_info(symbol, size)
        exit_price = exit_info['exit_price']
        fee = exit_info['fee']
        order_type = exit_info.get('order_type')

        # Determine exit reason based on order type first, then price
        exit_reason = self._determine_exit_reason(
            side, entry_price, exit_price, sl_price, tp_price, order_type
        )

        # Calculate PnL
        if side == 'BUY':
            pnl = (exit_price - entry_price) * size
        else:  # SELL
            pnl = (entry_price - exit_price) * size

        # Round values
        pnl = round(pnl, 4)
        fee = round(fee, 4)

        logger.info(f"[{symbol}] Trade closed: exit_price=${exit_price:.4f}, pnl=${pnl:.2f}, fee=${fee:.4f}, reason={exit_reason}")

        # Update trade in DB
        await db.execute(
            """UPDATE trades
               SET exit_price = ?, pnl = ?, fee = ?, exit_time = datetime('now'), exit_reason = ?
               WHERE id = ?""",
            (exit_price, pnl, fee, exit_reason, trade_id)
        )

        # Send telegram alert
        await telegram_alerter.alert_trade_closed(
            symbol=symbol_formatted,
            side=side,
            pnl=pnl,
            exit_reason=exit_reason,
            entry_price=entry_price,
            exit_price=exit_price,
            fee=fee
        )

    async def _get_exit_info(self, symbol: str, size: float) -> dict:
        """Gets exit price, fees, and order type from recent trade fills."""
        try:
            # Fetch recent user trades for this symbol
            trades = await binance_client.fetch_user_trades(symbol, limit=10)

            if trades:
                # Find the most recent reduceOnly trade (closing trade)
                # Sort by time descending and find trades that closed the position
                closing_trades = []
                total_qty = 0
                total_value = 0
                total_fee = 0
                order_id = None

                for trade in sorted(trades, key=lambda t: t.get('time', 0), reverse=True):
                    # We're looking for trades that match roughly our position size
                    qty = trade.get('qty', 0)
                    price = trade.get('price', 0)
                    fee = trade.get('commission', 0)

                    closing_trades.append(trade)
                    total_qty += qty
                    total_value += qty * price
                    total_fee += fee
                    order_id = trade.get('orderId')

                    # Stop once we've accounted for our position size
                    if total_qty >= size * 0.99:  # 99% to account for rounding
                        break

                if closing_trades and total_qty > 0:
                    exit_price = total_value / total_qty  # Weighted average price

                    # Try to get the order type to determine exit reason
                    order_type = None
                    if order_id:
                        order = await binance_client.fetch_order(symbol, order_id)
                        if order:
                            order_type = order.get('origType') or order.get('type')
                            logger.debug(f"[{symbol}] Closing order type: {order_type}")

                    return {
                        'exit_price': exit_price,
                        'fee': total_fee,
                        'order_type': order_type
                    }

            # Fallback: get last price
            try:
                ohlcv = await binance_client.fetch_ohlcv(symbol, '1m', limit=1)
                exit_price = ohlcv[-1][4] if ohlcv else 0
            except Exception:
                exit_price = 0

            fee = abs(size * exit_price) * 0.0004

            return {
                'exit_price': exit_price,
                'fee': fee,
                'order_type': None
            }

        except Exception as e:
            logger.error(f"Error getting exit info: {e}")
            return {'exit_price': 0, 'fee': 0, 'order_type': None}

    def _determine_exit_reason(self, side: str, entry: float, exit: float,
                                sl: float, tp: float, order_type: str = None) -> str:
        """Determines if exit was TP hit, SL hit, or manual based on order type."""

        # First, try to determine from order type (most reliable)
        if order_type:
            order_type_upper = order_type.upper()
            if 'TAKE_PROFIT' in order_type_upper:
                return 'TP_HIT'
            elif 'STOP' in order_type_upper and 'TAKE' not in order_type_upper:
                return 'SL_HIT'
            elif order_type_upper == 'MARKET':
                # Market orders could be manual or triggered - check prices
                pass  # Fall through to price-based detection

        # Fallback: determine from price comparison
        if not exit:
            return 'MANUAL'

        # If we have TP/SL prices, compare with exit price
        if tp and tp > 0:
            tolerance = exit * 0.005  # 0.5% tolerance
            if side == 'BUY':
                # Long: TP above entry
                if exit >= tp - tolerance:
                    return 'TP_HIT'
            else:
                # Short: TP below entry
                if exit <= tp + tolerance:
                    return 'TP_HIT'

        if sl and sl > 0:
            tolerance = exit * 0.005  # 0.5% tolerance
            if side == 'BUY':
                # Long: SL below entry
                if exit <= sl + tolerance:
                    return 'SL_HIT'
            else:
                # Short: SL above entry
                if exit >= sl - tolerance:
                    return 'SL_HIT'

        return 'MANUAL'

    def _format_symbol(self, symbol: str) -> str:
        """Converts BTCUSDT to BTC/USDT format."""
        if '/' in symbol:
            return symbol
        # Common quote currencies
        for quote in ['USDT', 'BUSD', 'USD', 'BTC', 'ETH']:
            if symbol.endswith(quote):
                base = symbol[:-len(quote)]
                return f"{base}/{quote}"
        return symbol


def create_position_monitor(config: dict) -> PositionMonitor:
    """Factory function to create position monitor."""
    return PositionMonitor(config)
