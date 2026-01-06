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
    entry_time TIMESTAMP,
    exit_time TIMESTAMP,
    regime_at_entry TEXT
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

# Global DB Instance
db = Database()
