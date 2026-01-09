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
        self.interval_seconds = config.get("monitoring", {}).get("position_check_interval", 30)
        self._running = False
        self._known_positions: Dict[str, dict] = {}  # symbol -> position info

    async def start(self):
        """Starts the position monitoring loop."""
        self._running = True
        logger.info(f"Starting position monitor (interval: {self.interval_seconds}s)")

        # Initial snapshot
        await self._update_known_positions()

        while self._running:
            try:
                await self._check_position_changes()
            except Exception as e:
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

        # Fetch recent fills to determine exit price and fees
        exit_info = await self._get_exit_info(symbol, size)
        exit_price = exit_info['exit_price']
        fee = exit_info['fee']

        # Determine exit reason based on exit price
        exit_reason = self._determine_exit_reason(
            side, entry_price, exit_price, sl_price, tp_price
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
        """Gets exit price and fees from recent order fills."""
        try:
            # Fetch recent orders for this symbol
            orders = await binance_client.fetch_open_orders(symbol)

            # If no orders, try to get from account trades
            # For now, estimate from position close
            # In production, you'd fetch from /fapi/v1/userTrades

            # Get last price as fallback
            try:
                ohlcv = await binance_client.fetch_ohlcv(symbol, '1m', limit=1)
                exit_price = ohlcv[-1][4] if ohlcv else 0  # Close price
            except Exception:
                exit_price = 0

            # Estimate fee (0.04% taker fee for futures)
            fee = abs(size * exit_price) * 0.0004

            return {
                'exit_price': exit_price,
                'fee': fee
            }

        except Exception as e:
            logger.error(f"Error getting exit info: {e}")
            return {'exit_price': 0, 'fee': 0}

    def _determine_exit_reason(self, side: str, entry: float, exit: float,
                                sl: float, tp: float) -> str:
        """Determines if exit was TP hit, SL hit, or manual."""
        if not sl or not tp or not exit:
            return 'MANUAL'

        # Tolerance for price matching (0.1%)
        tolerance = exit * 0.001

        if side == 'BUY':
            # Long position: TP is above entry, SL is below
            if tp and abs(exit - tp) <= tolerance:
                return 'TP_HIT'
            elif sl and abs(exit - sl) <= tolerance:
                return 'SL_HIT'
            elif exit >= tp * 0.995:  # Within 0.5% of TP
                return 'TP_HIT'
            elif exit <= sl * 1.005:  # Within 0.5% of SL
                return 'SL_HIT'
        else:  # SELL (short)
            # Short position: TP is below entry, SL is above
            if tp and abs(exit - tp) <= tolerance:
                return 'TP_HIT'
            elif sl and abs(exit - sl) <= tolerance:
                return 'SL_HIT'
            elif exit <= tp * 1.005:  # Within 0.5% of TP
                return 'TP_HIT'
            elif exit >= sl * 0.995:  # Within 0.5% of SL
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
