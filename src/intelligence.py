"""
Rocky Trading System - Intelligence Engine
Gathers real-time BTC price data, news, and sentiment for trade decisions.
"""

import os
import time
import logging
import requests
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("rocky.intel")


@dataclass
class BtcSnapshot:
    """Point-in-time BTC market intelligence."""
    timestamp: float
    price_usd: float
    price_change_1m: float = 0.0   # % change last 1 min
    price_change_5m: float = 0.0   # % change last 5 min
    price_change_15m: float = 0.0  # % change last 15 min
    price_change_1h: float = 0.0   # % change last 1 hour
    volume_24h: float = 0.0
    high_24h: float = 0.0
    low_24h: float = 0.0
    momentum: str = "neutral"       # "bullish", "bearish", "neutral"
    volatility: str = "normal"      # "low", "normal", "high"
    news_sentiment: str = "neutral" # "bullish", "bearish", "neutral"
    news_headlines: list = field(default_factory=list)
    raw_klines: list = field(default_factory=list)  # Recent candles
    spot_age_seconds: float = 0.0  # Age of Chainlink spot tick (staleness detection)
    chainlink_binance_div_bps: float = 0.0  # Divergence between Chainlink and Binance-relayed spot

    @property
    def trend_direction(self) -> str:
        """Simple trend based on multi-timeframe momentum."""
        bullish_signals = 0
        bearish_signals = 0

        for change in [self.price_change_1m, self.price_change_5m, self.price_change_15m]:
            if change > 0.05:
                bullish_signals += 1
            elif change < -0.05:
                bearish_signals += 1

        if bullish_signals >= 2:
            return "bullish"
        elif bearish_signals >= 2:
            return "bearish"
        return "neutral"


class IntelligenceEngine:
    """Gathers real-time BTC market intelligence from multiple sources."""

    def __init__(self, config, rtds=None):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Rocky-Trader/0.1"})
        self._rtds = rtds  # TwapSource instance (Polymarket RTDS WebSocket)

    def get_snapshot(self) -> BtcSnapshot:
        """Build a complete BTC market snapshot."""
        now = time.time()

        # Primary: RTDS Chainlink spot (real-time, bypasses Binance block)
        # Fallback: Binance REST (works on mcs-liam, not VPS Jerman)
        price_data = self._fetch_price()
        klines = self._fetch_klines()

        # Calculate momentum from klines
        changes = self._calculate_changes(klines, price_data.get("price", 0))

        # Determine volatility from recent candles
        volatility = self._assess_volatility(klines)

        # Determine momentum
        momentum = self._assess_momentum(changes)

        snapshot = BtcSnapshot(
            timestamp=now,
            price_usd=price_data.get("price", 0),
            price_change_1m=changes.get("1m", 0),
            price_change_5m=changes.get("5m", 0),
            price_change_15m=changes.get("15m", 0),
            price_change_1h=changes.get("1h", 0),
            volume_24h=price_data.get("volume", 0),
            high_24h=price_data.get("high", 0),
            low_24h=price_data.get("low", 0),
            momentum=momentum,
            volatility=volatility,
            raw_klines=klines[-20:] if klines else [],
            spot_age_seconds=price_data.get("spot_age", 0),
            chainlink_binance_div_bps=price_data.get("div_bps", 0),
        )

        logger.info(
            f"BTC ${snapshot.price_usd:,.2f} | "
            f"5m: {snapshot.price_change_5m:+.3f}% | "
            f"momentum: {snapshot.momentum} | "
            f"volatility: {snapshot.volatility}"
        )

        return snapshot

    def enrich_with_news(self, snapshot: BtcSnapshot) -> BtcSnapshot:
        """Add news sentiment to snapshot via web search."""
        try:
            headlines = self._search_btc_news()
            sentiment = self._analyze_headlines(headlines)
            snapshot.news_headlines = headlines[:5]
            snapshot.news_sentiment = sentiment
            logger.info(f"News sentiment: {sentiment} ({len(headlines)} headlines)")
        except Exception as e:
            logger.warning(f"Failed to fetch news: {e}")
        return snapshot

    def _fetch_price(self) -> dict:
        """Get current BTC price. RTDS Chainlink spot first, Binance REST fallback."""
        # 1. RTDS Chainlink spot (real-time, bypasses Kominfo block)
        if self._rtds is not None:
            spot = self._rtds.get_spot()
            if spot and spot > 0:
                return {
                    "price": spot,
                    "volume": 0,
                    "high": spot,
                    "low": spot,
                    "change_pct": 0,
                    "spot_age": self._rtds.get_spot_age_seconds(),
                    "div_bps": self._rtds.get_chainlink_binance_div_bps(),
                }
        # 2. Binance REST fallback (works on mcs-liam)
        return self._fetch_binance_price()

    def _fetch_klines(self) -> list:
        """Get 1m klines. RTDS-built from Chainlink ticks first, Binance REST fallback."""
        if self._rtds is not None:
            klines = self._rtds.get_klines_1m()
            if klines and len(klines) >= 3:
                return klines
        return self._fetch_binance_klines()

    def _fetch_binance_price(self) -> dict:
        """Get current BTC price from Binance (fallback when RTDS unavailable)."""
        try:
            resp = self.session.get(
                f"{self.config.binance_api}/ticker/24hr",
                params={"symbol": "BTCUSDT"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "price": float(data.get("lastPrice", 0)),
                "volume": float(data.get("volume", 0)),
                "high": float(data.get("highPrice", 0)),
                "low": float(data.get("lowPrice", 0)),
                "change_pct": float(data.get("priceChangePercent", 0)),
                "spot_age": 999.0,  # Binance fallback — no RTDS staleness tracking
                "div_bps": 0.0,
            }
        except Exception as e:
            logger.error(f"Binance price fetch failed: {e}")
            return {"price": 0, "volume": 0, "high": 0, "low": 0, "spot_age": 999.0, "div_bps": 0.0}

    def _fetch_binance_klines(self, interval: str = "1m", limit: int = 60) -> list:
        """Fetch recent 1-minute candles from Binance."""
        try:
            resp = self.session.get(
                f"{self.config.binance_api}/klines",
                params={
                    "symbol": "BTCUSDT",
                    "interval": interval,
                    "limit": limit,
                },
                timeout=10,
            )
            resp.raise_for_status()
            raw = resp.json()

            klines = []
            for k in raw:
                klines.append({
                    "open_time": k[0],
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "close_time": k[6],
                })
            return klines
        except Exception as e:
            logger.error(f"Failed to fetch klines: {e}")
            return []

    def _calculate_changes(self, klines: list, current_price: float) -> dict:
        """Calculate price changes over various timeframes from klines."""
        changes = {"1m": 0, "5m": 0, "15m": 0, "1h": 0}

        if not klines or current_price <= 0:
            return changes

        def pct_change(old, new):
            if old <= 0:
                return 0
            return ((new - old) / old) * 100

        if len(klines) >= 2:
            changes["1m"] = pct_change(klines[-2]["close"], current_price)
        if len(klines) >= 6:
            changes["5m"] = pct_change(klines[-6]["close"], current_price)
        if len(klines) >= 16:
            changes["15m"] = pct_change(klines[-16]["close"], current_price)
        if len(klines) >= 60:
            changes["1h"] = pct_change(klines[0]["close"], current_price)

        return changes

    def _assess_volatility(self, klines: list) -> str:
        """Assess recent volatility from candle ranges."""
        if len(klines) < 10:
            return "normal"

        recent = klines[-10:]
        ranges = [(k["high"] - k["low"]) / k["open"] * 100 for k in recent if k["open"] > 0]

        if not ranges:
            return "normal"

        avg_range = sum(ranges) / len(ranges)

        if avg_range > 0.15:
            return "high"
        elif avg_range < 0.03:
            return "low"
        return "normal"

    def _assess_momentum(self, changes: dict) -> str:
        """Determine momentum direction from price changes."""
        bullish = 0
        bearish = 0

        weights = {"1m": 3, "5m": 2, "15m": 1, "1h": 0.5}

        for tf, weight in weights.items():
            change = changes.get(tf, 0)
            if change > 0.02:
                bullish += weight
            elif change < -0.02:
                bearish += weight

        if bullish > bearish + 1:
            return "bullish"
        elif bearish > bullish + 1:
            return "bearish"
        return "neutral"

    def _search_btc_news(self) -> list[str]:
        """Search for recent BTC news headlines via SearXNG or fallback."""
        headlines = []

        # Try SearXNG first (local instance)
        searxng_url = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888")
        try:
            resp = self.session.get(
                f"{searxng_url}/search",
                params={
                    "q": "Bitcoin BTC price news today",
                    "format": "json",
                    "categories": "news",
                    "time_range": "day",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            for result in data.get("results", [])[:10]:
                title = result.get("title", "").strip()
                if title:
                    headlines.append(title)
            if headlines:
                logger.info(f"SearXNG returned {len(headlines)} headlines")
                return headlines
        except Exception as e:
            logger.debug(f"SearXNG unavailable: {e}")

        # Fallback: CryptoCompare (may 401 without key)
        try:
            resp = self.session.get(
                "https://min-api.cryptocompare.com/data/v2/news/",
                params={"categories": "BTC", "sortOrder": "latest"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                for article in data.get("Data", [])[:10]:
                    title = article.get("title", "").strip()
                    if title:
                        headlines.append(title)
                if headlines:
                    return headlines
        except Exception as e:
            logger.debug(f"CryptoCompare news failed: {e}")

        # Fallback: Binance BTC announcements RSS-ish via public news proxy
        try:
            resp = self.session.get(
                "https://api.coingecko.com/api/v3/news",
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                items = data if isinstance(data, list) else data.get("data", []) or data.get("news", [])
                for article in items[:10]:
                    title = (article.get("title") or article.get("description") or "").strip()
                    if title and ("btc" in title.lower() or "bitcoin" in title.lower() or True):
                        headlines.append(title[:160])
                if headlines:
                    return headlines[:10]
        except Exception as e:
            logger.debug(f"CoinGecko news failed: {e}")

        if not headlines:
            logger.warning("All news sources failed — continuing without headlines")
        return headlines

    def _analyze_headlines(self, headlines: list[str]) -> str:
        """Simple keyword-based sentiment analysis of headlines."""
        if not headlines:
            return "neutral"

        bullish_keywords = [
            "surge", "rally", "soar", "jump", "bull", "high", "record",
            "breakout", "pump", "gain", "rise", "up", "moon", "buy",
            "adoption", "etf approved", "institutional",
        ]
        bearish_keywords = [
            "crash", "drop", "plunge", "dump", "bear", "low", "sell",
            "fear", "hack", "ban", "regulation", "sec", "lawsuit",
            "decline", "fall", "down", "tank", "collapse",
        ]

        bull_score = 0
        bear_score = 0

        for headline in headlines:
            h = headline.lower()
            for kw in bullish_keywords:
                if kw in h:
                    bull_score += 1
            for kw in bearish_keywords:
                if kw in h:
                    bear_score += 1

        if bull_score > bear_score + 2:
            return "bullish"
        elif bear_score > bull_score + 2:
            return "bearish"
        return "neutral"
