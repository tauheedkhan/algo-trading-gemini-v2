# Agent Build Guideline — Binance USDT-M Futures Bot (Regime-Based, Env-Controlled)

## 0) Objective
Build a production-grade, fully automatic trading bot for **Binance USDT-M Futures** that:
1) Uses **.env** to switch **TESTNET vs MAINNET** (no code changes required).
2) Identifies **market regime** per symbol (TREND / RANGE / BREAKOUT-or-NO_TRADE).
3) Applies the **best strategy** for the detected regime:
   - TREND → Trend Continuation Pullback strategy
   - RANGE → Bollinger Bands Mean Reversion strategy
   - BREAKOUT or UNCERTAIN → optional breakout strategy or NO_TRADE (configurable)
4) Enforces strict **risk controls**, **reconciliation**, **journaling**, and **dashboard API**.

Bot must run on testnet first; once stable, switch to mainnet by changing `.env`.

---

## 1) Non-negotiable Engineering Rules (Invariants)
- Never open a position without a stop loss.
- Never exceed configured leverage, margin mode, or risk limits.
- Never place orders if symbol filters (tick/step size) cannot be applied.
- Any inconsistency between positions and protective orders triggers an emergency flatten or safe stop.
- All actions must be logged + journaled (SQLite).
- Trading must be disabled automatically if drawdown limits breached.
- No discretionary/LLM decisions for trading logic; trading decisions must be deterministic.

---

## 2) Environment Control via `.env` (Required)
### .env keys
BINANCE_ENV=testnet            # testnet | mainnet
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
DASHBOARD_TOKEN=...
TG_BOT_TOKEN=...
TG_CHAT_ID=...

### Environment mapping (in code)
- If BINANCE_ENV=testnet:
  - base_url = https://demo-fapi.binance.com
- If BINANCE_ENV=mainnet:
  - base_url = https://fapi.binance.com

NO other changes should be required to switch environments.

---

## 3) Runtime Configuration via `config.yaml` (Strategy & Risk)
Keep behavior configurable via YAML (no code edits for tuning):
- enabled symbols, per-symbol enable/disable
- timeframes (1D/4H/1H)
- risk per trade, max open positions, max exposure
- daily/weekly/monthly drawdown limits
- regime thresholds and hysteresis
- per-regime strategy enable/disable
- order types (MARKET/LIMIT, STOP_MARKET, TAKE_PROFIT_MARKET)
- leverage, margin type (ISOLATED), position mode (ONEWAY)

`.env` controls environment + secrets only.
`config.yaml` controls trading behavior.

---

## 4) Repo Structure (Required)
Implement as a clean modular project:

/bot
  /core
    config.py            # loads config.yaml + .env
    logger.py
    clock.py
  /exchange
    binance_client.py    # signed REST client, retries, rate limits
    symbol_filters.py    # tickSize/stepSize rounding, minQty
  /data
    market_data.py       # fetch klines per TF
    indicators.py        # EMA, RSI, ATR, Bollinger, ADX (pure functions)
  /regime
    regime_features.py   # computes regime features
    regime_classifier.py # deterministic classifier + hysteresis
  /strategies
    trend_pullback.py    # TREND regime
    bollinger_meanrev.py # RANGE regime
    breakout_optional.py # optional
    router.py            # chooses strategy based on regime + config
  /risk
    risk_engine.py       # sizing, exposure caps, drawdown caps
    portfolio_limits.py
  /execution
    executor.py          # order placement, SL/TP, reduceOnly, closePosition
    reconciliation.py    # ensures protective orders exist, cancels orphans
  /state
    db.py                # SQLite connection + migrations
    repositories.py      # CRUD for trades/orders/positions/snapshots
  /monitoring
    alerts.py            # telegram
    health.py            # heartbeat, api errors
    kill_switch.py
  /api
    dashboard_api.py     # FastAPI endpoints reading from SQLite
  main.py                # scheduler loop
/tests
  unit/
  integration/

---

## 5) Market Data & Timeframes
For each enabled symbol:
- Trend timeframe: 1D
- Setup timeframe: 4H
- Entry timeframe: 1H

Fetch enough candles for indicators (min 200 candles per TF).

Prefer REST polling (swing bot). Optional websocket later.

---

## 6) Regime Detection (Deterministic, Precise Enough)
### Target regimes
- TREND
- RANGE
- BREAKOUT (or EXPANSION)
- NO_TRADE (uncertain / messy)

### Features (compute per symbol on 4H and/or 1D)
Recommended features:
- ADX(14) on 4H (trend strength)
- EMA20/EMA50 slope and separation on 1D (trend direction & strength)
- Bollinger Bandwidth (20,2) on 4H (compression)
- Bandwidth change rate (expansion)
- ATR% (ATR/price) on 4H (volatility)
- Price mean-reversion score: how often price crosses mid-band in last N bars

### Classifier rules (example; thresholds must be configurable)
- TREND if:
  - ADX >= adx_trend_threshold
  - AND |EMA separation| >= ema_sep_threshold
  - AND EMA slope in direction is positive/negative
- RANGE if:
  - ADX <= adx_range_threshold
  - AND bandwidth <= bb_bw_low_threshold
  - AND mean-reversion score high
- BREAKOUT/EXPANSION if:
  - bandwidth rising fast (bw_change >= bw_expansion_threshold)
  - OR ATR% spike
- else NO_TRADE

### Hysteresis (required to avoid flip-flop)
Regime should not change unless:
- new regime persists for K confirmations
OR
- confidence score exceeds a higher threshold

Store last regime in DB and in memory; update only when stable.

Output:
{
  symbol,
  regime,
  confidence,
  features_snapshot,
  timestamp
}

---

## 7) Strategy Router (Regime → Strategy)
Implement a router:
- Determine regime for symbol
- If strategy for that regime disabled → NO_TRADE
- Else generate signal via selected strategy

Router must also support:
- global pause
- per-symbol pause
- “cooldown after loss streak” (optional)

---

## 8) Strategies

### 8.1 TREND Strategy: Trend Continuation Pullback
Timeframes:
- Trend: 1D (EMA20>EMA50 for long; EMA20<EMA50 for short)
- Setup: 4H pullback into EMA zone
- Entry: 1H candle close confirmation

Rules (configurable, deterministic):
- Trend filter:
  - Long enabled if close>EMA20 and EMA20>EMA50
  - Short enabled if close<EMA20 and EMA20<EMA50
- Pullback:
  - price touches EMA20–EMA50 zone (4H)
  - RSI between 40–60 (tunable)
- Entry:
  - bullish engulfing / strong close (long) OR bearish equivalent (short)
- Stop:
  - below swing low (long), above swing high (short)
- TP:
  - min RR >= 2.0 (skip if not achievable)

### 8.2 RANGE Strategy: Bollinger Mean Reversion
Timeframe: 1H or 4H (configurable)

Rules:
- Only valid when regime == RANGE
- Long setup:
  - close below lower band
  - RSI oversold threshold (tunable)
  - enter when candle closes back inside bands
- Short setup:
  - close above upper band
  - RSI overbought threshold
  - enter when closes back inside
- Stop:
  - beyond recent swing / band extreme
- TP:
  - mid-band first target (EMA20) and/or opposite band
- Must have min RR threshold (configurable; often 1.2–2.0)

### 8.3 BREAKOUT Strategy (Optional)
If enabled:
- Squeeze breakout: bandwidth low then expansion
- Trade direction based on break of recent range
If disabled: NO_TRADE during BREAKOUT regime.

---

## 9) Risk Engine (Portfolio-Level + Per-Trade)
Requirements:
- Risk per trade = % equity (equity from Binance account or last snapshot)
- Max open positions
- Max total open risk %
- Daily/weekly/monthly loss caps based on realized PnL + equity snapshots
- Stop trading (kill-switch) when breached
- Position sizing formula:
  risk_usd = equity * risk_pct
  qty = risk_usd / stop_distance
- Apply symbol filters: stepSize, minQty, tickSize rounding

---

## 10) Execution Engine (Binance Futures USDT-M)
For each signal:
1) Ensure symbol margin/leverage set (idempotent)
2) Place entry order (MARKET or LIMIT)
3) Immediately place:
   - STOP_MARKET (reduceOnly, closePosition)
   - TAKE_PROFIT_MARKET (reduceOnly, closePosition)
4) Verify protective orders exist. If not, emergency close.

Must handle:
- retries w/ idempotency keys (clientOrderId)
- rate limits
- partial fills
- position mode (ONEWAY assumed)

---

## 11) Reconciliation Loop (Mandatory)
Runs every N seconds:
- For each open position:
  - verify SL and TP orders exist
  - if missing → emergency close OR place missing protection (configurable)
- For each open order:
  - if no corresponding position AND order is reduceOnly closePosition → cancel orphan
- Record reconciliation actions in DB + alert on anomalies

---

## 12) Persistence (SQLite)
Implement migrations; tables minimum:
- regimes (symbol, regime, confidence, ts, features_json)
- trades (open/close, pnl, R, regime_at_entry, strategy_used)
- orders (orderId, clientOrderId, type, status, timestamps)
- positions (symbol, side, size, entry, mark, uPnL)
- equity_snapshots (balance, equity, ts)
- errors (component, error, ts)
- system_events (start/stop/pause/kill-switch)

All decisions and state changes must be journaled.

---

## 13) Dashboard API (Required)
Build a FastAPI service that reads SQLite and (optionally) Binance snapshots.

Endpoints (minimum):
- GET /api/health
- GET /api/summary (equity, pnl, drawdown, mode)
- GET /api/positions
- GET /api/orders
- GET /api/trades?limit&offset
- GET /api/regimes (latest regime per symbol)
- GET /api/pnl?range=1d|7d|30d|all
Admin actions (protected):
- POST /api/bot/pause
- POST /api/bot/resume
- POST /api/bot/flatten

Auth:
- token auth using DASHBOARD_TOKEN from .env (simple header-based)

---

## 14) Testing Requirements
### Unit tests
- indicator calculations
- rounding to tick/step size
- regime classifier hysteresis
- strategy signals (given known candles, signal expected)
- risk sizing

### Integration tests (testnet)
- place/cancel orders on testnet with small size
- verify SL/TP placed
- reconciliation handles missing orders
- dashboard endpoints return expected data

No mainnet tests by default.

---

## 15) Observability
- Structured logs (JSON recommended)
- Telegram alerts for:
  - trade open/close
  - stop/TP hit
  - kill-switch
  - reconciliation anomaly
  - repeated API errors

Heartbeat event every hour.

---

## 16) Deliverables Checklist
Agent must produce:
- Working bot in Python (preferred) with modules above
- .env template + config.yaml template
- SQLite schema + migration scripts
- FastAPI dashboard API service
- Dockerfile + docker-compose (bot + api) (recommended)
- README with how to run testnet then switch to mainnet

---

## 17) Safety Defaults (Set These)
- Isolated margin
- Leverage: 2x initially
- Risk per trade: 0.5% in testnet forward tests
- Max open positions: 3
- If regime uncertain → NO_TRADE

---

## 18) Notes for Agent
- Do not use an LLM to decide trades.
- Regime detection + strategies must be deterministic and backtestable.
- Prefer correctness and safety over speed.
- Switching testnet/mainnet must be a `.env` change only.

END
