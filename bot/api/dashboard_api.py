from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import APIKeyHeader
from bot.state.db import db
from bot.core.config import load_config
import os

app = FastAPI(title="Crypto Bot Dashboard")

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

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/api/positions", dependencies=[Depends(get_api_key)])
async def get_positions():
    # In a real bot, we'd fetch from DB 'positions' table or calling binance client
    # For now, let's return what's in DB
    positions = await db.fetch_all("SELECT * FROM positions")
    return {"positions": [dict(p) for p in positions]}

@app.get("/api/trades", dependencies=[Depends(get_api_key)])
async def get_trades(limit: int = 50):
    trades = await db.fetch_all("SELECT * FROM trades ORDER BY entry_time DESC LIMIT ?", (limit,))
    return {"trades": [dict(t) for t in trades]}

@app.get("/api/regimes", dependencies=[Depends(get_api_key)])
async def get_regimes():
    regimes = await db.fetch_all("SELECT symbol, regime, confidence, created_at FROM regimes GROUP BY symbol HAVING max(created_at)")
    return {"regimes": [dict(r) for r in regimes]}
