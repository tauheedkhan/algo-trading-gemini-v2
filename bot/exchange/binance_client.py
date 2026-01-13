import os
import time
import hmac
import hashlib
import logging
import asyncio
from urllib.parse import urlencode
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import httpx
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Binance Futures API endpoints
TESTNET_BASE_URL = "https://testnet.binancefuture.com"
MAINNET_BASE_URL = "https://fapi.binance.com"

# Rate limiting constants
MAX_REQUESTS_PER_MINUTE = 1200  # Conservative limit (Binance allows 2400 for most endpoints)
RATE_LIMIT_WINDOW = 60  # seconds


class APICache:
    """Shared cache for API responses to reduce duplicate calls."""

    def __init__(self):
        self._cache: Dict[str, Any] = {}
        self._timestamps: Dict[str, datetime] = {}
        self._ttl: Dict[str, int] = {
            'account_data': 30,  # 30 seconds - raw account data
            'balance': 30,       # 30 seconds
            'positions': 30,     # 30 seconds
            'open_orders': 30,   # 30 seconds
        }

    def get(self, key: str) -> Optional[Any]:
        """Get cached value if not expired."""
        if key not in self._cache:
            return None

        ttl = self._ttl.get(key.split(':')[0], 5)
        if datetime.now() - self._timestamps.get(key, datetime.min) > timedelta(seconds=ttl):
            return None

        return self._cache[key]

    def set(self, key: str, value: Any):
        """Cache a value."""
        self._cache[key] = value
        self._timestamps[key] = datetime.now()

    def invalidate(self, key: str = None):
        """Invalidate cache entry or all entries."""
        if key:
            self._cache.pop(key, None)
            self._timestamps.pop(key, None)
        else:
            self._cache.clear()
            self._timestamps.clear()


class BinanceClient:
    def __init__(self):
        self._load_env()
        self.client: Optional[httpx.AsyncClient] = None
        self._symbol_info: dict = {}  # Cache for symbol precision info

        # Rate limiting
        self._request_times: list = []
        self._rate_limit_lock = asyncio.Lock()

        # Backoff state
        self._backoff_until: Optional[datetime] = None
        self._consecutive_errors = 0
        self._max_backoff_seconds = 300  # 5 minutes max backoff

        # Shared cache
        self.cache = APICache()

    def _load_env(self):
        load_dotenv()
        self.api_key = os.getenv("BINANCE_API_KEY")
        self.api_secret = os.getenv("BINANCE_API_SECRET")
        self.env_type = os.getenv("BINANCE_ENV", "testnet").lower()

        if self.env_type == "testnet":
            self.base_url = TESTNET_BASE_URL
        else:
            self.base_url = MAINNET_BASE_URL

        if not self.api_key or not self.api_secret:
            logger.warning("Binance API credentials missing! Check .env")

    def _sign(self, params: dict) -> str:
        """Creates HMAC SHA256 signature for request."""
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature

    def _get_timestamp(self) -> int:
        """Returns current timestamp in milliseconds."""
        return int(time.time() * 1000)

    async def initialize(self):
        """Initializes the HTTP client."""
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-MBX-APIKEY": self.api_key},
            timeout=30.0
        )
        logger.info(f"Binance Client initialized in {self.env_type.upper()} mode")

    async def close(self):
        """Closes the HTTP client."""
        if self.client:
            await self.client.aclose()
            logger.info("Binance client connection closed")

    async def _check_rate_limit(self):
        """Check and enforce rate limiting."""
        async with self._rate_limit_lock:
            now = time.time()
            # Remove old requests outside the window
            self._request_times = [t for t in self._request_times if now - t < RATE_LIMIT_WINDOW]

            if len(self._request_times) >= MAX_REQUESTS_PER_MINUTE:
                wait_time = RATE_LIMIT_WINDOW - (now - self._request_times[0])
                if wait_time > 0:
                    logger.warning(f"Rate limit approaching, waiting {wait_time:.1f}s")
                    await asyncio.sleep(wait_time)

            self._request_times.append(now)

    async def _check_backoff(self):
        """Check if we're in backoff period."""
        if self._backoff_until and datetime.now() < self._backoff_until:
            wait_seconds = (self._backoff_until - datetime.now()).total_seconds()
            logger.warning(f"In backoff period, waiting {wait_seconds:.1f}s")
            await asyncio.sleep(wait_seconds)

    def _calculate_backoff(self) -> float:
        """Calculate exponential backoff time."""
        base_delay = 2
        backoff = min(base_delay * (2 ** self._consecutive_errors), self._max_backoff_seconds)
        return backoff

    async def _request(self, method: str, endpoint: str, params: dict = None, signed: bool = True) -> dict:
        """Makes an API request to Binance with rate limiting and backoff."""
        if not self.client:
            await self.initialize()

        # Check backoff first
        await self._check_backoff()

        # Then check rate limit
        await self._check_rate_limit()

        params = params or {}

        if signed:
            params['timestamp'] = self._get_timestamp()
            params['signature'] = self._sign(params)

        try:
            if method == "GET":
                response = await self.client.get(endpoint, params=params)
            elif method == "POST":
                response = await self.client.post(endpoint, params=params)
            elif method == "DELETE":
                response = await self.client.delete(endpoint, params=params)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            response.raise_for_status()

            # Reset error count on success
            self._consecutive_errors = 0
            self._backoff_until = None

            return response.json()

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            error_data = e.response.json() if e.response.content else {}

            # Handle rate limiting (429) and IP ban (418)
            if status_code in [418, 429]:
                self._consecutive_errors += 1
                backoff_seconds = self._calculate_backoff()

                # Check for Retry-After header
                retry_after = e.response.headers.get('Retry-After')
                if retry_after:
                    try:
                        backoff_seconds = max(backoff_seconds, int(retry_after))
                    except ValueError:
                        pass

                self._backoff_until = datetime.now() + timedelta(seconds=backoff_seconds)
                logger.error(f"Rate limited (HTTP {status_code}). Backing off for {backoff_seconds}s. "
                            f"Consecutive errors: {self._consecutive_errors}")

                raise Exception(f"Rate limited: {error_data.get('msg', str(e))}. Retry after {backoff_seconds}s")

            logger.error(f"Binance API error: {status_code} - {error_data}")
            raise Exception(f"Binance API error: {error_data.get('msg', str(e))}")
        except Exception as e:
            logger.error(f"Request failed: {e}")
            raise

    # ============ Symbol Info ============

    async def load_symbol_info(self, symbol: str) -> dict:
        """Loads and caches symbol trading rules (precision, min qty, etc)."""
        symbol_clean = symbol.replace("/", "")

        if symbol_clean in self._symbol_info:
            return self._symbol_info[symbol_clean]

        try:
            data = await self._request("GET", "/fapi/v1/exchangeInfo", signed=False)

            for s in data.get('symbols', []):
                sym = s.get('symbol')
                filters = {f['filterType']: f for f in s.get('filters', [])}

                # Extract precision info
                lot_size = filters.get('LOT_SIZE', {})
                price_filter = filters.get('PRICE_FILTER', {})

                self._symbol_info[sym] = {
                    'quantityPrecision': s.get('quantityPrecision', 3),
                    'pricePrecision': s.get('pricePrecision', 2),
                    'minQty': float(lot_size.get('minQty', 0.001)),
                    'stepSize': float(lot_size.get('stepSize', 0.001)),
                    'tickSize': float(price_filter.get('tickSize', 0.01)),
                }

            return self._symbol_info.get(symbol_clean, {
                'quantityPrecision': 3,
                'pricePrecision': 2,
                'minQty': 0.001,
                'stepSize': 0.001,
                'tickSize': 0.01
            })

        except Exception as e:
            logger.warning(f"Could not load symbol info: {e}")
            return {'quantityPrecision': 3, 'pricePrecision': 2}

    def round_quantity(self, symbol: str, quantity: float) -> float:
        """Rounds quantity to valid precision for the symbol."""
        # Convert numpy floats to Python floats
        if hasattr(quantity, 'item'):
            quantity = quantity.item()
        quantity = float(quantity)

        symbol_clean = symbol.replace("/", "")
        info = self._symbol_info.get(symbol_clean, {})
        precision = info.get('quantityPrecision', 3)
        step_size = info.get('stepSize', 0.001)

        # Round to step size
        rounded = round(quantity / step_size) * step_size
        # Then round to precision
        return round(rounded, precision)

    def round_price(self, symbol: str, price: float) -> float:
        """Rounds price to valid precision for the symbol."""
        # Convert numpy floats to Python floats
        if hasattr(price, 'item'):
            price = price.item()
        price = float(price)

        symbol_clean = symbol.replace("/", "")
        info = self._symbol_info.get(symbol_clean, {})
        precision = info.get('pricePrecision', 2)
        tick_size = info.get('tickSize', 0.01)

        # Round to tick size
        rounded = round(price / tick_size) * tick_size
        return round(rounded, precision)

    # ============ Public Endpoints ============

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200) -> list:
        """Fetches OHLCV candlestick data."""
        # Convert symbol format: BTC/USDT -> BTCUSDT
        symbol_clean = symbol.replace("/", "")

        params = {
            "symbol": symbol_clean,
            "interval": timeframe,
            "limit": limit
        }

        data = await self._request("GET", "/fapi/v1/klines", params, signed=False)

        # Convert to standard format: [timestamp, open, high, low, close, volume]
        return [
            [
                candle[0],              # timestamp
                float(candle[1]),       # open
                float(candle[2]),       # high
                float(candle[3]),       # low
                float(candle[4]),       # close
                float(candle[5])        # volume
            ]
            for candle in data
        ]

    # ============ Account Endpoints ============

    async def _fetch_account_data(self, use_cache: bool = True) -> dict:
        """Fetches account data with caching to reduce duplicate API calls."""
        cache_key = 'account_data'

        if use_cache:
            cached = self.cache.get(cache_key)
            if cached:
                logger.info("Using cached account data")
                return cached

        data = await self._request("GET", "/fapi/v2/account")
        self.cache.set(cache_key, data)
        return data

    async def get_balance(self, use_cache: bool = True) -> dict:
        """Fetches account balance with caching."""
        cache_key = 'balance'

        if use_cache:
            cached = self.cache.get(cache_key)
            if cached:
                logger.info("Using cached balance")
                return cached

        data = await self._fetch_account_data(use_cache=False)

        usdt_balance = 0
        usdt_available = 0
        unrealized_pnl = float(data.get('totalUnrealizedProfit', 0))

        for asset in data.get('assets', []):
            if asset.get('asset') == 'USDT':
                usdt_balance = float(asset.get('walletBalance', 0))
                usdt_available = float(asset.get('availableBalance', 0))
                break

        result = {
            'total': {'USDT': usdt_balance + unrealized_pnl},
            'free': {'USDT': usdt_available},
            'info': {'totalUnrealizedProfit': unrealized_pnl}
        }

        self.cache.set(cache_key, result)
        return result

    async def fetch_positions(self, use_cache: bool = True) -> list:
        """Fetches all open positions with caching."""
        cache_key = 'positions'

        if use_cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                logger.info("Using cached positions")
                return cached

        data = await self._fetch_account_data(use_cache=False)
        positions = data.get('positions', [])

        result = []
        for p in positions:
            amt = float(p.get('positionAmt', 0))
            if amt != 0:
                result.append({
                    'symbol': p.get('symbol'),
                    'contracts': abs(amt),
                    'side': 'long' if amt > 0 else 'short',
                    'entryPrice': float(p.get('entryPrice', 0)),
                    'markPrice': float(p.get('markPrice', 0)),
                    'unrealizedPnl': float(p.get('unrealizedProfit', 0)),
                    'marginMode': p.get('marginType', '').lower(),
                    'leverage': int(p.get('leverage', 1)),
                    'info': p
                })

        self.cache.set(cache_key, result)
        return result

    async def fetch_open_orders(self, symbol: str = None, use_cache: bool = True) -> list:
        """Fetches all open orders with caching."""
        cache_key = f'open_orders:{symbol or "all"}'

        if use_cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                logger.info(f"Using cached open orders for {symbol or 'all'}")
                return cached

        params = {}
        if symbol:
            params['symbol'] = symbol.replace("/", "")

        data = await self._request("GET", "/fapi/v1/openOrders", params)

        result = [
            {
                'id': str(order.get('orderId')),
                'symbol': order.get('symbol'),
                'type': order.get('type'),
                'side': order.get('side').lower(),
                'price': float(order.get('price', 0)),
                'amount': float(order.get('origQty', 0)),
                'stopPrice': float(order.get('stopPrice', 0)),
                'reduceOnly': order.get('reduceOnly', False),
                'status': order.get('status'),
                'info': order
            }
            for order in data
        ]

        self.cache.set(cache_key, result)
        return result

    async def fetch_user_trades(self, symbol: str, limit: int = 10) -> list:
        """Fetches recent user trades for a symbol to determine fill info."""
        symbol_clean = symbol.replace("/", "")

        params = {
            'symbol': symbol_clean,
            'limit': limit
        }

        try:
            data = await self._request("GET", "/fapi/v1/userTrades", params)

            return [
                {
                    'id': str(trade.get('id')),
                    'orderId': str(trade.get('orderId')),
                    'symbol': trade.get('symbol'),
                    'side': trade.get('side').lower(),
                    'price': float(trade.get('price', 0)),
                    'qty': float(trade.get('qty', 0)),
                    'realizedPnl': float(trade.get('realizedPnl', 0)),
                    'commission': float(trade.get('commission', 0)),
                    'commissionAsset': trade.get('commissionAsset'),
                    'time': trade.get('time'),
                    'buyer': trade.get('buyer', False),
                    'maker': trade.get('maker', False),
                    'info': trade
                }
                for trade in data
            ]
        except Exception as e:
            logger.error(f"Failed to fetch user trades for {symbol}: {e}")
            return []

    async def fetch_order(self, symbol: str, order_id: str) -> dict:
        """Fetches a specific order by ID to get its type."""
        symbol_clean = symbol.replace("/", "")

        params = {
            'symbol': symbol_clean,
            'orderId': order_id
        }

        try:
            data = await self._request("GET", "/fapi/v1/order", params)

            return {
                'id': str(data.get('orderId')),
                'symbol': data.get('symbol'),
                'type': data.get('type'),
                'origType': data.get('origType'),  # Original order type
                'side': data.get('side').lower(),
                'price': float(data.get('price', 0)),
                'avgPrice': float(data.get('avgPrice', 0)),
                'stopPrice': float(data.get('stopPrice', 0)),
                'status': data.get('status'),
                'reduceOnly': data.get('reduceOnly', False),
                'info': data
            }
        except Exception as e:
            logger.error(f"Failed to fetch order {order_id} for {symbol}: {e}")
            return {}

    # ============ Trading Endpoints ============

    async def create_order(self, symbol: str, order_type: str, side: str, amount: float,
                           price: float = None, params: dict = None) -> dict:
        """Creates a new order with proper precision handling."""
        symbol_clean = symbol.replace("/", "")
        params = params or {}

        # Load symbol info for precision
        await self.load_symbol_info(symbol)

        # Round quantity to valid precision
        rounded_amount = self.round_quantity(symbol, amount)

        order_params = {
            "symbol": symbol_clean,
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": rounded_amount
        }

        # Add price for limit orders
        if price and order_type.upper() in ['LIMIT', 'STOP', 'TAKE_PROFIT']:
            order_params['price'] = self.round_price(symbol, price)
            order_params['timeInForce'] = 'GTC'

        # Add stop price for stop orders
        if 'stopPrice' in params and params['stopPrice'] is not None:
            order_params['stopPrice'] = self.round_price(symbol, params['stopPrice'])
        elif order_type.upper() in ['STOP_MARKET', 'TAKE_PROFIT_MARKET', 'STOP', 'TAKE_PROFIT']:
            raise ValueError(f"stopPrice is required for {order_type} orders but was None")

        # Add reduce only
        if params.get('reduceOnly'):
            order_params['reduceOnly'] = 'true'

        logger.info(f"Creating order: {order_params}")
        data = await self._request("POST", "/fapi/v1/order", order_params)
        logger.info(f"Order response: {data}")

        # Invalidate cache after order creation
        self.cache.invalidate()

        return {
            'id': str(data.get('orderId')),
            'symbol': data.get('symbol'),
            'type': data.get('type'),
            'side': data.get('side'),
            'amount': float(data.get('origQty', 0)),
            'price': float(data.get('price', 0)),
            'average': float(data.get('avgPrice', 0)),
            'status': data.get('status'),
            'info': data
        }

    async def cancel_order(self, order_id: str, symbol: str) -> dict:
        """Cancels an order by ID."""
        symbol_clean = symbol.replace("/", "")

        params = {
            "symbol": symbol_clean,
            "orderId": order_id
        }

        data = await self._request("DELETE", "/fapi/v1/order", params)

        # Invalidate cache after order cancellation
        self.cache.invalidate()

        return {
            'id': str(data.get('orderId')),
            'symbol': data.get('symbol'),
            'status': data.get('status'),
            'info': data
        }

    async def set_leverage(self, symbol: str, leverage: int):
        """Sets leverage for a symbol."""
        symbol_clean = symbol.replace("/", "")

        params = {
            "symbol": symbol_clean,
            "leverage": leverage
        }

        try:
            await self._request("POST", "/fapi/v1/leverage", params)
            logger.info(f"Leverage set to {leverage}x for {symbol}")
        except Exception as e:
            logger.warning(f"Could not set leverage for {symbol}: {e}")

    async def set_margin_mode(self, symbol: str, margin_mode: str) -> bool:
        """Sets margin mode for a symbol (ISOLATED or CROSSED)."""
        symbol_clean = symbol.replace("/", "")

        params = {
            "symbol": symbol_clean,
            "marginType": margin_mode.upper()
        }

        try:
            await self._request("POST", "/fapi/v1/marginType", params)
            logger.info(f"Margin mode set to {margin_mode} for {symbol}")
            return True
        except Exception as e:
            error_msg = str(e)
            if "No need to change" in error_msg:
                return True
            logger.warning(f"Could not set margin mode for {symbol}: {e}")
            return False

    async def get_position_mode(self) -> str:
        """Returns current position mode (ONEWAY or HEDGE)."""
        try:
            data = await self._request("GET", "/fapi/v1/positionSide/dual")
            return "HEDGE" if data.get('dualSidePosition') else "ONEWAY"
        except Exception as e:
            logger.info(f"Could not get position mode: {e}")
            return "ONEWAY"

    async def get_margin_mode(self, symbol: str) -> str:
        """Returns margin mode for a symbol (ISOLATED or CROSSED)."""
        try:
            positions = await self.fetch_positions()
            for p in positions:
                if p.get('symbol') == symbol.replace("/", ""):
                    return p.get('marginMode', '').upper()
            return "ISOLATED"
        except Exception:
            return "ISOLATED"


# Global Client Instance
binance_client = BinanceClient()
