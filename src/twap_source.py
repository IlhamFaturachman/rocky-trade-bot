"""Polymarket RTDS Chainlink TWAP price source.

Background thread subscribes to Polymarket's Real-Time Data Stream WebSocket
for 30-second Chainlink TWAP BTC/USD prices — the exact settlement source
Polymarket uses for 5-min crypto markets (eff Aug 7 2026).

Caches recent TWAP values in a thread-safe deque. Rocky's resolve function
looks up the TWAP value closest to the 5-min window end. Falls back to
Binance 1s klines proxy if cache miss (e.g. bot just started, WS down).
"""
import asyncio
import json
import logging
import threading
import time
from collections import deque
from typing import Optional

logger = logging.getLogger("rocky.twap")

_RTDS_URL = "wss://ws-live-data.polymarket.com"
_SUBSCRIBE_FRAME = {
    "action": "subscribe",
    "subscriptions": [{"topic": "crypto_prices_twap_thirty", "type": "update"}],
}
_MAX_CACHE = 900  # 15 min of ~1s updates (covers 5-min window + resolve delay)
_RECONNECT_DELAY = 5  # seconds between reconnect attempts


class TwapSource:
    """Thread-safe cache of recent Chainlink TWAP prices from Polymarket RTDS.

    Start in a daemon thread; resolve reads get_twap_at() with Binance fallback.
    """

    def __init__(self) -> None:
        self._cache: deque[tuple[int, float]] = deque(maxlen=_MAX_CACHE)
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def cache_size(self) -> int:
        with self._lock:
            return len(self._cache)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="twap-rtds"
        )
        self._thread.start()
        logger.info("TWAP RTDS source thread started")

    def stop(self) -> None:
        self._running = False
        self._connected = False

    def _run_loop(self) -> None:
        while self._running:
            try:
                asyncio.run(self._connect_and_stream())
            except Exception as e:
                logger.warning(f"TWAP RTDS stream disconnected: {e}")
                self._connected = False
            if self._running:
                time.sleep(_RECONNECT_DELAY)

    async def _connect_and_stream(self) -> None:
        import websockets

        async with websockets.connect(_RTDS_URL, ping_interval=20, ping_timeout=10) as ws:
            await ws.send(json.dumps(_SUBSCRIBE_FRAME))
            self._connected = True
            logger.info(
                f"TWAP RTDS connected to Polymarket (wss://ws-live-data.polymarket.com) "
                f"— streaming Chainlink 30s TWAP BTC/USD"
            )
            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw)
                    topic = msg.get("topic")
                    if topic != "crypto_prices_twap_thirty":
                        continue
                    if msg.get("type") != "update":
                        continue
                    payload = msg.get("payload", {})
                    if payload.get("symbol") != "btc/usd":
                        continue
                    ts = int(payload.get("timestamp", 0))
                    val_str = payload.get("value")
                    if val_str is not None and ts > 0:
                        price = float(str(val_str))
                        if price > 0:
                            with self._lock:
                                self._cache.append((ts, price))
                except (json.JSONDecodeError, ValueError, TypeError, KeyError):
                    continue

    def get_twap_at(self, target_timestamp: float, tolerance: int = 10) -> Optional[float]:
        """Get the TWAP price closest to target_timestamp (within tolerance seconds).

        Polymarket RTDS emits ~1 TWAP update/second. We find the cached entry
        whose timestamp is closest to the 5-min window end. tolerance=10s
        handles minor clock skew / network delay.

        Returns None if no cached value within tolerance (caller falls back
        to Binance 1s proxy).
        """
        target = int(target_timestamp)
        with self._lock:
            if not self._cache:
                return None
            best_price = None
            best_diff = tolerance + 1
            for ts, price in self._cache:
                diff = abs(ts - target)
                if diff < best_diff:
                    best_diff = diff
                    best_price = price
        if best_price is not None and best_diff <= tolerance:
            return best_price
        return None
