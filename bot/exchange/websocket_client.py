import os
import json
import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional, Callable
from collections import deque
import pandas as pd
import websockets
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Binance Futures WebSocket endpoints
TESTNET_WS_URL = "wss://stream.binancefuture.com/ws"
MAINNET_WS_URL = "wss://fstream.binance.com/ws"


class BinanceWebSocketClient:
    """
    WebSocket client for Binance Futures that maintains real-time candle data.

    Features:
    - Subscribes to kline streams for multiple symbols
    - Maintains local candle cache (last N candles per symbol)
    - Auto-reconnection on disconnect
    - Thread-safe candle access
    """

    def __init__(self, max_candles: int = 500):
        load_dotenv()
        self.env_type = os.getenv("BINANCE_ENV", "testnet").lower()
        self.ws_url = TESTNET_WS_URL if self.env_type == "testnet" else MAINNET_WS_URL

        self.max_candles = max_candles
        self._candle_cache: Dict[str, Dict[str, deque]] = {}  # {symbol: {timeframe: deque}}
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._subscribed_streams: List[str] = []
        self._lock = asyncio.Lock()
        self._callbacks: List[Callable] = []
        self._reconnect_delay = 5  # seconds
        self._last_update: Dict[str, datetime] = {}

    def _symbol_to_stream(self, symbol: str) -> str:
        """Convert symbol format: BTC/USDT -> btcusdt"""
        return symbol.replace("/", "").lower()

    def _stream_to_symbol(self, stream_symbol: str) -> str:
        """Convert stream format back: btcusdt -> BTCUSDT"""
        return stream_symbol.upper()

    async def initialize(self, symbols: List[str], timeframes: List[str] = ["1h"]):
        """
        Initialize WebSocket connection and subscribe to kline streams.

        Args:
            symbols: List of symbols like ["BTC/USDT", "ETH/USDT"]
            timeframes: List of timeframes like ["1h", "4h", "1d"]
        """
        # Initialize candle cache for each symbol/timeframe
        for symbol in symbols:
            symbol_clean = symbol.replace("/", "")
            self._candle_cache[symbol_clean] = {}
            for tf in timeframes:
                self._candle_cache[symbol_clean][tf] = deque(maxlen=self.max_candles)

        # Build subscription streams
        self._subscribed_streams = []
        for symbol in symbols:
            stream_symbol = self._symbol_to_stream(symbol)
            for tf in timeframes:
                self._subscribed_streams.append(f"{stream_symbol}@kline_{tf}")

        logger.info(f"WebSocket initialized for {len(symbols)} symbols, {len(timeframes)} timeframes")
        logger.info(f"Streams: {self._subscribed_streams}")

    async def start(self):
        """Start the WebSocket connection and message handler."""
        self._running = True

        while self._running:
            try:
                await self._connect_and_subscribe()
                await self._message_loop()
            except websockets.ConnectionClosed as e:
                logger.warning(f"WebSocket connection closed: {e}. Reconnecting in {self._reconnect_delay}s...")
            except Exception as e:
                logger.error(f"WebSocket error: {e}. Reconnecting in {self._reconnect_delay}s...")

            if self._running:
                await asyncio.sleep(self._reconnect_delay)

    async def _connect_and_subscribe(self):
        """Establish connection and subscribe to streams."""
        # Build combined stream URL
        streams = "/".join(self._subscribed_streams)
        url = f"{self.ws_url}/{streams}"

        logger.info(f"Connecting to WebSocket: {self.ws_url}")
        self._ws = await websockets.connect(url, ping_interval=20, ping_timeout=10)
        logger.info(f"WebSocket connected successfully in {self.env_type.upper()} mode")

    async def _message_loop(self):
        """Process incoming WebSocket messages."""
        async for message in self._ws:
            try:
                data = json.loads(message)
                await self._process_message(data)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse message: {e}")
            except Exception as e:
                logger.error(f"Error processing message: {e}")

    async def _process_message(self, data: dict):
        """Process a kline message and update candle cache."""
        if 'e' not in data or data['e'] != 'kline':
            return

        kline = data['k']
        symbol = self._stream_to_symbol(data['s'])
        timeframe = kline['i']
        is_closed = kline['x']  # Is this kline closed?

        candle = {
            'timestamp': pd.to_datetime(kline['t'], unit='ms'),
            'open': float(kline['o']),
            'high': float(kline['h']),
            'low': float(kline['l']),
            'close': float(kline['c']),
            'volume': float(kline['v']),
            'is_closed': is_closed
        }

        async with self._lock:
            if symbol in self._candle_cache and timeframe in self._candle_cache[symbol]:
                cache = self._candle_cache[symbol][timeframe]

                # Update or append candle
                if len(cache) > 0 and cache[-1]['timestamp'] == candle['timestamp']:
                    # Update existing candle (still forming)
                    cache[-1] = candle
                else:
                    # New candle
                    cache.append(candle)

                self._last_update[f"{symbol}_{timeframe}"] = datetime.now()

        # Trigger callbacks if candle closed
        if is_closed:
            for callback in self._callbacks:
                try:
                    await callback(symbol, timeframe, candle)
                except Exception as e:
                    logger.error(f"Callback error: {e}")

    def on_candle_close(self, callback: Callable):
        """Register a callback for when a candle closes."""
        self._callbacks.append(callback)

    async def get_candles(self, symbol: str, timeframe: str = "1h") -> pd.DataFrame:
        """
        Get cached candles as a DataFrame.

        Args:
            symbol: Symbol like "BTC/USDT" or "BTCUSDT"
            timeframe: Timeframe like "1h"

        Returns:
            DataFrame with columns: timestamp, open, high, low, close, volume
        """
        symbol_clean = symbol.replace("/", "")

        async with self._lock:
            if symbol_clean not in self._candle_cache:
                logger.warning(f"No cache for {symbol_clean}")
                return pd.DataFrame()

            if timeframe not in self._candle_cache[symbol_clean]:
                logger.warning(f"No cache for {symbol_clean} {timeframe}")
                return pd.DataFrame()

            cache = self._candle_cache[symbol_clean][timeframe]
            if len(cache) == 0:
                return pd.DataFrame()

            df = pd.DataFrame(list(cache))
            # Remove the is_closed column for strategy use
            if 'is_closed' in df.columns:
                df = df.drop(columns=['is_closed'])

            return df

    async def get_candle_count(self, symbol: str, timeframe: str = "1h") -> int:
        """Get number of cached candles for a symbol/timeframe."""
        symbol_clean = symbol.replace("/", "")

        async with self._lock:
            if symbol_clean in self._candle_cache and timeframe in self._candle_cache[symbol_clean]:
                return len(self._candle_cache[symbol_clean][timeframe])
        return 0

    async def preload_candles(self, symbol: str, timeframe: str, candles: List[dict]):
        """
        Preload historical candles into the cache (from REST API).
        Call this before starting WebSocket to have historical data.
        """
        symbol_clean = symbol.replace("/", "")

        async with self._lock:
            if symbol_clean not in self._candle_cache:
                self._candle_cache[symbol_clean] = {}
            if timeframe not in self._candle_cache[symbol_clean]:
                self._candle_cache[symbol_clean][timeframe] = deque(maxlen=self.max_candles)

            cache = self._candle_cache[symbol_clean][timeframe]
            cache.clear()

            for candle in candles:
                cache.append({
                    'timestamp': candle['timestamp'] if isinstance(candle['timestamp'], pd.Timestamp) else pd.to_datetime(candle['timestamp']),
                    'open': float(candle['open']),
                    'high': float(candle['high']),
                    'low': float(candle['low']),
                    'close': float(candle['close']),
                    'volume': float(candle['volume']),
                    'is_closed': True
                })

        logger.info(f"Preloaded {len(candles)} candles for {symbol} {timeframe}")

    async def wait_for_data(self, min_candles: int = 50, timeout: int = 60) -> bool:
        """
        Wait until we have minimum candle data for all symbols.

        Args:
            min_candles: Minimum candles needed per symbol
            timeout: Max seconds to wait

        Returns:
            True if data is ready, False if timeout
        """
        start = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start < timeout:
            all_ready = True

            async with self._lock:
                for symbol, timeframes in self._candle_cache.items():
                    for tf, cache in timeframes.items():
                        if len(cache) < min_candles:
                            all_ready = False
                            break
                    if not all_ready:
                        break

            if all_ready:
                return True

            await asyncio.sleep(1)

        return False

    def get_status(self) -> dict:
        """Get WebSocket connection status."""
        return {
            "connected": self._ws is not None and self._ws.open if self._ws else False,
            "running": self._running,
            "streams": len(self._subscribed_streams),
            "last_updates": {k: v.isoformat() for k, v in self._last_update.items()}
        }

    async def stop(self):
        """Stop the WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
            logger.info("WebSocket connection closed")


# Global instance
ws_client = BinanceWebSocketClient()
