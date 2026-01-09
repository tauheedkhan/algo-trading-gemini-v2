import aiosqlite
import logging
import os

logger = logging.getLogger(__name__)

DB_PATH = "bot_data.db"

INIT_SCRIPT = """
-- Regimes Table: Tracks market regime changes
CREATE TABLE IF NOT EXISTS regimes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    regime TEXT NOT NULL, -- 'TREND', 'RANGE', 'BREAKOUT', 'NO_TRADE'
    confidence REAL,
    features_json TEXT, -- Snapshotted feature values
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Trades Table: Closed trades with PnL
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    strategy TEXT,
    side TEXT NOT NULL, -- 'BUY', 'SELL'
    entry_price REAL,
    exit_price REAL,
    size REAL,
    pnl REAL,
    fee REAL DEFAULT 0,
    entry_time TIMESTAMP,
    exit_time TIMESTAMP,
    exit_reason TEXT, -- 'TP_HIT', 'SL_HIT', 'MANUAL', 'LIQUIDATION'
    regime_at_entry TEXT,
    sl_price REAL,
    tp_price REAL
);

-- Orders Table: Active and historical orders
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    client_order_id TEXT,
    symbol TEXT NOT NULL,
    type TEXT, -- 'MARKET', 'LIMIT', 'STOP_MARKET', etc.
    side TEXT,
    price REAL,
    amount REAL,
    status TEXT, -- 'NEW', 'FILLED', 'CANCELED'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Positions Table: Current Open Positions (Snapshot)
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    side TEXT,
    size REAL,
    entry_price REAL,
    mark_price REAL,
    unrealized_pnl REAL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Errors Table: For critical logs/errors
CREATE TABLE IF NOT EXISTS system_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component TEXT,
    message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Equity Snapshots for Drawdown Tracking
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    balance REAL NOT NULL,
    equity REAL NOT NULL,
    unrealized_pnl REAL,
    snapshot_date DATE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- System Events (kill-switch, pause, resume, startup, shutdown)
CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Index for daily equity lookups
CREATE INDEX IF NOT EXISTS idx_equity_date ON equity_snapshots(snapshot_date);

-- Index for trade queries
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_exit_reason ON trades(exit_reason);
"""

class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.conn = None

    async def connect(self):
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await self._init_schema()
        logger.info(f"Connected to SQLite DB at {self.db_path}")

    async def _init_schema(self):
        if not self.conn:
            raise RuntimeError("Database not connected")
        await self.conn.executescript(INIT_SCRIPT)
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()
            logger.info("Database connection closed")

    async def execute(self, query: str, params: tuple = ()):
        async with self.conn.execute(query, params) as cursor:
            await self.conn.commit()
            return cursor.lastrowid

    async def fetch_all(self, query: str, params: tuple = ()):
        async with self.conn.execute(query, params) as cursor:
            return await cursor.fetchall()
    
    async def fetch_one(self, query: str, params: tuple = ()):
        async with self.conn.execute(query, params) as cursor:
            return await cursor.fetchone()

    async def save_equity_snapshot(self, balance: float, equity: float, unrealized_pnl: float = 0):
        """Saves equity snapshot. Updates if one exists for today, otherwise inserts."""
        from datetime import date
        today = date.today().isoformat()

        # Check if snapshot exists for today
        existing = await self.fetch_one(
            "SELECT id FROM equity_snapshots WHERE snapshot_date = ?", (today,)
        )

        if existing:
            await self.execute(
                "UPDATE equity_snapshots SET balance = ?, equity = ?, unrealized_pnl = ? WHERE snapshot_date = ?",
                (balance, equity, unrealized_pnl, today)
            )
        else:
            await self.execute(
                "INSERT INTO equity_snapshots (balance, equity, unrealized_pnl, snapshot_date) VALUES (?, ?, ?, ?)",
                (balance, equity, unrealized_pnl, today)
            )

    async def get_daily_start_equity(self) -> float:
        """Gets the first equity snapshot of the day (start of day balance)."""
        from datetime import date
        today = date.today().isoformat()

        result = await self.fetch_one(
            "SELECT equity FROM equity_snapshots WHERE snapshot_date = ? ORDER BY created_at ASC LIMIT 1",
            (today,)
        )
        return result['equity'] if result else None

    async def get_peak_equity(self) -> float:
        """Gets the highest equity recorded (for max drawdown calculation)."""
        result = await self.fetch_one(
            "SELECT MAX(equity) as peak FROM equity_snapshots"
        )
        return result['peak'] if result and result['peak'] else None

    async def log_system_event(self, event_type: str, reason: str = None):
        """Logs a system event (KILL_SWITCH, PAUSE, RESUME, START, STOP)."""
        await self.execute(
            "INSERT INTO system_events (event_type, reason) VALUES (?, ?)",
            (event_type, reason)
        )

    async def get_performance_stats(self) -> dict:
        """Gets comprehensive performance statistics."""
        # Total trades opened (all trades in DB)
        total_opened = await self.fetch_one(
            "SELECT COUNT(*) as count FROM trades"
        )

        # Closed trades (have exit_price)
        closed_trades = await self.fetch_one(
            "SELECT COUNT(*) as count FROM trades WHERE exit_price IS NOT NULL"
        )

        # TP hits
        tp_hits = await self.fetch_one(
            "SELECT COUNT(*) as count FROM trades WHERE exit_reason = 'TP_HIT'"
        )

        # SL hits
        sl_hits = await self.fetch_one(
            "SELECT COUNT(*) as count FROM trades WHERE exit_reason = 'SL_HIT'"
        )

        # Total realized PnL
        total_pnl = await self.fetch_one(
            "SELECT COALESCE(SUM(pnl), 0) as total FROM trades WHERE pnl IS NOT NULL"
        )

        # Total fees paid
        total_fees = await self.fetch_one(
            "SELECT COALESCE(SUM(fee), 0) as total FROM trades WHERE fee IS NOT NULL"
        )

        # Win rate calculation
        winning_trades = await self.fetch_one(
            "SELECT COUNT(*) as count FROM trades WHERE pnl > 0 AND pnl IS NOT NULL"
        )

        # Average win and loss
        avg_win = await self.fetch_one(
            "SELECT COALESCE(AVG(pnl), 0) as avg FROM trades WHERE pnl > 0"
        )
        avg_loss = await self.fetch_one(
            "SELECT COALESCE(AVG(pnl), 0) as avg FROM trades WHERE pnl < 0"
        )

        # Net PnL (after fees)
        total_pnl_val = total_pnl['total'] if total_pnl else 0
        total_fees_val = total_fees['total'] if total_fees else 0
        net_pnl = total_pnl_val - total_fees_val

        closed_count = closed_trades['count'] if closed_trades else 0
        winning_count = winning_trades['count'] if winning_trades else 0
        win_rate = (winning_count / closed_count * 100) if closed_count > 0 else 0

        return {
            'total_trades_opened': total_opened['count'] if total_opened else 0,
            'total_trades_closed': closed_count,
            'open_trades': (total_opened['count'] if total_opened else 0) - closed_count,
            'tp_hits': tp_hits['count'] if tp_hits else 0,
            'sl_hits': sl_hits['count'] if sl_hits else 0,
            'winning_trades': winning_count,
            'losing_trades': closed_count - winning_count,
            'win_rate': round(win_rate, 2),
            'total_pnl': round(total_pnl_val, 2),
            'total_fees': round(total_fees_val, 2),
            'net_pnl': round(net_pnl, 2),
            'avg_win': round(avg_win['avg'] if avg_win else 0, 2),
            'avg_loss': round(avg_loss['avg'] if avg_loss else 0, 2),
        }

    async def get_trade_history(self, limit: int = 50) -> list:
        """Gets recent trade history with all details."""
        trades = await self.fetch_all(
            """SELECT id, symbol, strategy, side, entry_price, exit_price, size,
                      pnl, fee, entry_time, exit_time, exit_reason, sl_price, tp_price
               FROM trades
               ORDER BY entry_time DESC
               LIMIT ?""",
            (limit,)
        )
        return [dict(t) for t in trades] if trades else []

    async def get_daily_stats(self) -> dict:
        """Gets today's trading statistics."""
        from datetime import date
        today = date.today().isoformat()

        # Trades opened today
        opened_today = await self.fetch_one(
            "SELECT COUNT(*) as count FROM trades WHERE DATE(entry_time) = ?",
            (today,)
        )

        # Trades closed today
        closed_today = await self.fetch_one(
            "SELECT COUNT(*) as count FROM trades WHERE DATE(exit_time) = ?",
            (today,)
        )

        # PnL today
        pnl_today = await self.fetch_one(
            "SELECT COALESCE(SUM(pnl), 0) as total FROM trades WHERE DATE(exit_time) = ?",
            (today,)
        )

        # Fees today
        fees_today = await self.fetch_one(
            "SELECT COALESCE(SUM(fee), 0) as total FROM trades WHERE DATE(exit_time) = ?",
            (today,)
        )

        # TP/SL hits today
        tp_today = await self.fetch_one(
            "SELECT COUNT(*) as count FROM trades WHERE DATE(exit_time) = ? AND exit_reason = 'TP_HIT'",
            (today,)
        )
        sl_today = await self.fetch_one(
            "SELECT COUNT(*) as count FROM trades WHERE DATE(exit_time) = ? AND exit_reason = 'SL_HIT'",
            (today,)
        )

        return {
            'trades_opened': opened_today['count'] if opened_today else 0,
            'trades_closed': closed_today['count'] if closed_today else 0,
            'pnl': round(pnl_today['total'] if pnl_today else 0, 2),
            'fees': round(fees_today['total'] if fees_today else 0, 2),
            'tp_hits': tp_today['count'] if tp_today else 0,
            'sl_hits': sl_today['count'] if sl_today else 0,
        }


# Global DB Instance
db = Database()
