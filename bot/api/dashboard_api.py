from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import APIKeyHeader
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from bot.state.db import db
from bot.exchange.binance_client import binance_client
from bot.core.config import load_config
import os
from pathlib import Path

app = FastAPI(title="Crypto Bot Dashboard")

# Get the project root directory
PROJECT_ROOT = Path(__file__).parent.parent.parent

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_KEY_NAME = "X-API-Token"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

async def get_api_key(api_key_header: str = Depends(api_key_header)):
    # Simple token check
    expected = os.getenv("DASHBOARD_TOKEN", "secret").strip()
    if api_key_header != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Token",
        )
    return api_key_header

@app.on_event("startup")
async def startup():
    print("Dashboard Startup")
    # DB is initialized by main loop usually, but for standalone API check:
    if not db.conn:
        await db.connect()
    # Initialize Binance client if not already done
    if not binance_client.client:
        await binance_client.initialize()

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serve the dashboard HTML page."""
    dashboard_path = PROJECT_ROOT / "dashboard.html"
    if dashboard_path.exists():
        return HTMLResponse(content=dashboard_path.read_text(), status_code=200)
    return HTMLResponse(content="<h1>Dashboard not found</h1>", status_code=404)

@app.get("/api/positions", dependencies=[Depends(get_api_key)])
async def get_positions():
    """Fetch live positions from Binance."""
    try:
        positions = await binance_client.fetch_positions()
        return {"positions": [
            {
                "symbol": p.get("symbol"),
                "side": p.get("side", "").upper(),
                "size": p.get("contracts"),
                "entry_price": p.get("entryPrice"),
                "mark_price": p.get("markPrice"),
                "unrealized_pnl": p.get("unrealizedPnl"),
                "leverage": p.get("leverage"),
            }
            for p in positions
        ]}
    except Exception as e:
        return {"positions": [], "error": str(e)}

@app.get("/api/trades", dependencies=[Depends(get_api_key)])
async def get_trades(limit: int = 50):
    trades = await db.fetch_all("SELECT * FROM trades ORDER BY entry_time DESC LIMIT ?", (limit,))
    return {"trades": [dict(t) for t in trades]}

@app.get("/api/regimes", dependencies=[Depends(get_api_key)])
async def get_regimes():
    regimes = await db.fetch_all("SELECT symbol, regime, confidence, created_at FROM regimes GROUP BY symbol HAVING max(created_at)")
    return {"regimes": [dict(r) for r in regimes]}


@app.get("/api/stats", dependencies=[Depends(get_api_key)])
async def get_performance_stats():
    """Get comprehensive performance statistics."""
    stats = await db.get_performance_stats()
    daily_stats = await db.get_daily_stats()
    return {
        "overall": stats,
        "today": daily_stats
    }


@app.get("/api/trade-history", dependencies=[Depends(get_api_key)])
async def get_trade_history(limit: int = 50):
    """Get detailed trade history with all fields."""
    trades = await db.get_trade_history(limit)
    return {"trades": trades}
