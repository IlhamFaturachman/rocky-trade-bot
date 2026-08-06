"""Polymarket RTDS — Chainlink + Binance-relayed price feeds via WebSocket.

Bypasses Kominfo Binance DNS block completely — all data comes from
Polymarket's WebSocket (wss://ws-live-data.polymarket.com), same source
Polymarket uses for settlement and market pricing.

Three feeds subscribed:
1. crypto_prices_chainlink   — Chainlink BTC/USD spot (real-time oracle)
2. crypto_prices             — Binance BTC/USDT relayed through Polymarket
3. crypto_prices_twap_thirty — Chainlink 30s TWAP (settlement price)

Caches:
- _spot_cache:   (ts_sec, price) deque — real-time Chainlink spot ticks
- _twap_cache:   (ts_sec, price) deque — 30s TWAP settlement values
- _klines_1m:   list of {open,high,low,close,volume} dicts built from spot ticks

Rocky's intelligence.py reads spot price + klines from here instead of
Binance REST API. main.py resolve reads TWAP cache. No HTTP calls to
Binance at all — 100% Polymarket data.
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
    "subscriptions": [
        {"topic": "crypto_prices_chainlink", "type": "update"},
        {"topic": "crypto_prices", "type": "update"},
        {"topic": "crypto_prices_twap_thirty", "type": "update"},
    ],
}
_MAX_CACHE = 1800       # 30 min of ~1s updates
_MAX_KLINES = 120       # 2 hours of 1m candles
_RECONNECT_DELAY = 5


class TwapSource:
    """Thread-safe RTDS price cache from Polymarket WebSocket.

    Provides:
    - get_spot() → latest Chainlink BTC/USD spot price (real-time)
    - get_klines_1m() → built 1-minute OHLC candles from spot ticks
    - get_twap_at(ts) → 30s TWAP settlement price at timestamp
    - get_price_at(ts) → Chainlink spot closest to timestamp (for candle open)
    """

    def __init__(self) -> None:
        self._spot_cache: deque[tuple[int, float]] = deque(maxlen=_MAX_CACHE)
        self._twap_cache: deque[tuple[int, float]] = deque(maxlen=_MAX_CACHE)
        self._binance_cache: deque[tuple[int, float]] = deque(maxlen=_MAX_CACHE)
        self._klines_1m: list[dict] = []
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
            return len(self._spot_cache)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="rtds-ws"
        )
        self._thread.start()
        logger.info("RTDS source thread started")

    def stop(self) -> None:
        self._running = False
        self._connected = False

    def _run_loop(self) -> None:
        while self._running:
            try:
                asyncio.run(self._connect_and_stream())
            except Exception as e:
                logger.warning(f"RTDS stream disconnected: {e}")
                self._connected = False
            if self._running:
                time.sleep(_RECONNECT_DELAY)

    async def _connect_and_stream(self) -> None:
        import websockets

        async with websockets.connect(_RTDS_URL, ping_interval=20, ping_timeout=10) as ws:
            await ws.send(json.dumps(_SUBSCRIBE_FRAME))
            self._connected = True
            logger.info(
                "RTDS connected to Polymarket — streaming Chainlink spot + "
                "Binance relay + TWAP 30s BTC/USD"
            )
            last_tick = time.time()
            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=45)
                except asyncio.TimeoutError:
                    # No data for 45s — connection is "alive" (pings) but
                    # Polymarket stopped sending ticks. Force reconnect.
                    gap = time.time() - last_tick
                    logger.warning(
                        f"RTDS no ticks for {gap:.0f}s — force reconnecting"
                    )
                    self._connected = False
                    return
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                    if msg.get("type") != "update":
                        continue
                    topic = msg.get("topic")
                    payload = msg.get("payload", {})
                    ts_ms = int(payload.get("timestamp", 0))
                    if ts_ms <= 0:
                        continue
                    ts_sec = ts_ms // 1000
                    last_tick = time.time()

                    if topic == "crypto_prices_chainlink":
                        if payload.get("symbol") != "btc/usd":
                            continue
                        price = float(str(payload.get("value", 0)))
                        if price > 0:
                            self._on_spot_tick(ts_sec, price, source="chainlink")

                    elif topic == "crypto_prices":
                        sym = payload.get("symbol", "")
                        if sym.lower() not in ("btcusdt", "btc/usdt", "btcusd"):
                            continue
                        price = float(str(payload.get("value", payload.get("price", 0))))
                        if price > 0:
                            with self._lock:
                                self._binance_cache.append((ts_sec, price))

                    elif topic == "crypto_prices_twap_thirty":
                        if payload.get("symbol") != "btc/usd":
                            continue
                        price = float(str(payload.get("value", 0)))
                        if price > 0:
                            with self._lock:
                                self._twap_cache.append((ts_sec, price))

                except (json.JSONDecodeError, ValueError, TypeError, KeyError):
                    continue

    def _on_spot_tick(self, ts_sec: int, price: float, source: str = "chainlink") -> None:
        """Process a spot price tick: cache + aggregate into 1m klines."""
        with self._lock:
            self._spot_cache.append((ts_sec, price))
            self._aggregate_kline(ts_sec, price)

    def _aggregate_kline(self, ts_sec: int, price: float) -> None:
        """Build/update 1-minute OHLC candle from spot ticks.

        Candle bucket = floor(ts_sec / 60) * 60.
        Each bucket: open=first tick, high=max, low=min, close=last, volume=count.
        """
        bucket = (ts_sec // 60) * 60
        if self._klines_1m and self._klines_1m[-1]["open_time"] == bucket:
            k = self._klines_1m[-1]
            k["high"] = max(k["high"], price)
            k["low"] = min(k["low"], price)
            k["close"] = price
            k["volume"] += 1
        else:
            self._klines_1m.append({
                "open_time": bucket,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 1.0,
            })
            if len(self._klines_1m) > _MAX_KLINES:
                self._klines_1m = self._klines_1m[-_MAX_KLINES:]

    def get_spot(self) -> Optional[float]:
        """Latest Chainlink BTC/USD spot price (real-time)."""
        with self._lock:
            if self._spot_cache:
                return self._spot_cache[-1][1]
        return None

    def get_spot_age_seconds(self) -> float:
        """Age of the latest Chainlink spot tick (staleness detection)."""
        with self._lock:
            if self._spot_cache:
                return max(0.0, time.time() - self._spot_cache[-1][0])
        return 999.0  # no data = very stale

    def get_chainlink_binance_div_bps(self) -> float:
        """Divergence between Chainlink and Binance-relayed spot (bps)."""
        with self._lock:
            chainlink = self._spot_cache[-1][1] if self._spot_cache else None
            binance = self._binance_cache[-1][1] if self._binance_cache else None
        if chainlink and binance and chainlink > 0:
            return ((chainlink - binance) / chainlink) * 10_000.0
        return 0.0

    def get_binance_spot(self) -> Optional[float]:
        """Latest Binance BTC/USDT spot (relayed via Polymarket RTDS)."""
        with self._lock:
            if self._binance_cache:
                return self._binance_cache[-1][1]
        return None

    def get_klines_1m(self) -> list[dict]:
        """Recent 1-minute OHLC candles built from Chainlink spot ticks."""
        with self._lock:
            return list(self._klines_1m)

    def get_price_at(self, target_timestamp: float, tolerance: int = 10) -> Optional[float]:
        """Chainlink spot price closest to target_timestamp (within tolerance seconds).

        Used for candle open: find the spot price at the 5-min window start.
        """
        target = int(target_timestamp)
        with self._lock:
            if not self._spot_cache:
                return None
            best_price = None
            best_diff = tolerance + 1
            for ts, price in self._spot_cache:
                diff = abs(ts - target)
                if diff < best_diff:
                    best_diff = diff
                    best_price = price
        if best_price is not None and best_diff <= tolerance:
            return best_price
        return None

    def get_twap_at(self, target_timestamp: float, tolerance: int = 10) -> Optional[float]:
        """30s TWAP settlement price closest to target_timestamp."""
        target = int(target_timestamp)
        with self._lock:
            if not self._twap_cache:
                return None
            best_price = None
            best_diff = tolerance + 1
            for ts, price in self._twap_cache:
                diff = abs(ts - target)
                if diff < best_diff:
                    best_diff = diff
                    best_price = price
        if best_price is not None and best_diff <= tolerance:
            return best_price
        return None
