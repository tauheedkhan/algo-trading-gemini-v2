import os
import time
import hmac
import hashlib
import logging
from urllib.parse import urlencode
from typing import Optional
import httpx
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Binance Futures API endpoints
TESTNET_BASE_URL = "https://testnet.binancefuture.com"
MAINNET_BASE_URL = "https://fapi.binance.com"


class BinanceClient:
    def __init__(self):
        self._load_env()
        self.client: Optional[httpx.AsyncClient] = None
        self._symbol_info: dict = {}  # Cache for symbol precision info

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

    async def _request(self, method: str, endpoint: str, params: dict = None, signed: bool = True) -> dict:
        """Makes an API request to Binance."""
        if not self.client:
            await self.initialize()

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
            return response.json()

        except httpx.HTTPStatusError as e:
            error_data = e.response.json() if e.response.content else {}
            logger.error(f"Binance API error: {e.response.status_code} - {error_data}")
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

    async def get_balance(self) -> dict:
        """Fetches account balance."""
        data = await self._request("GET", "/fapi/v2/account")

        usdt_balance = 0
        usdt_available = 0
        unrealized_pnl = float(data.get('totalUnrealizedProfit', 0))

        for asset in data.get('assets', []):
            if asset.get('asset') == 'USDT':
                usdt_balance = float(asset.get('walletBalance', 0))
                usdt_available = float(asset.get('availableBalance', 0))
                break

        return {
            'total': {'USDT': usdt_balance + unrealized_pnl},
            'free': {'USDT': usdt_available},
            'info': {'totalUnrealizedProfit': unrealized_pnl}
        }

    async def fetch_positions(self) -> list:
        """Fetches all open positions."""
        data = await self._request("GET", "/fapi/v2/account")
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
        return result

    async def fetch_open_orders(self, symbol: str = None) -> list:
        """Fetches all open orders."""
        params = {}
        if symbol:
            params['symbol'] = symbol.replace("/", "")

        data = await self._request("GET", "/fapi/v1/openOrders", params)

        return [
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
        if 'stopPrice' in params:
            order_params['stopPrice'] = self.round_price(symbol, params['stopPrice'])

        # Add reduce only
        if params.get('reduceOnly'):
            order_params['reduceOnly'] = 'true'

        data = await self._request("POST", "/fapi/v1/order", order_params)

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
            logger.debug(f"Could not get position mode: {e}")
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
