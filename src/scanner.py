"""
Rocky Trading System - Market Scanner
Fetches BTC Up/Down 5-minute markets from Polymarket via series slug.
Hardened: parse JSON-string fields, filter expired markets, price from CLOB.
"""

import json
import time
import logging
import requests
from typing import Optional, Any
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger("rocky.scanner")

# Supported Polymarket BTC series (primary → secondary)
BTC_SERIES = [
    "btc-up-or-down-5m",
    "btc-up-or-down-15m",
    "btc-up-or-down-hourly",
]


def _parse_json_field(value: Any, default=None):
    """Gamma often returns arrays as JSON-encoded strings."""
    if value is None:
        return default if default is not None else []
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return default if default is not None else []
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default if default is not None else []
    return default if default is not None else []


def _parse_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_end_ts(end_date: str) -> float:
    """Return unix ts for end_date ISO string, or 0."""
    if not end_date:
        return 0.0
    try:
        # Handle Z and offsets
        s = end_date.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


@dataclass
class BtcMarket:
    """Represents a BTC Up/Down market on Polymarket."""
    condition_id: str
    question: str
    market_slug: str
    tokens: list            # [{"token_id": ..., "outcome": "Up"/"Down"}, ...]
    end_date: str
    active: bool
    yes_price: float        # Price of YES/Up token
    no_price: float         # Price of NO/Down token
    volume: float
    liquidity: float
    description: str = ""
    series_slug: str = ""
    price_to_beat: float = 0.0  # Candle open price for resolution
    event_id: str = ""
    seconds_to_end: float = 0.0

    @property
    def implied_up_prob(self) -> float:
        return self.yes_price

    @property
    def implied_down_prob(self) -> float:
        return self.no_price


class MarketScanner:
    """Scans Polymarket for active BTC Up/Down markets."""

    def __init__(self, config):
        self.config = config
        self.gamma_url = config.gamma_api_url
        self.clob_url = config.clob_api_url
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Rocky-Trader/0.2",
            "Accept": "application/json",
        })

    def fetch_btc_markets(self, series_slugs: list[str] = None) -> list[BtcMarket]:
        """
        Fetch active BTC markets from Polymarket.

        Primary path for 5m: direct event slug `btc-updown-5m-{window_unix}`
        (series list endpoint often returns stale/future windows only).
        """
        if series_slugs is None:
            series_slugs = BTC_SERIES

        markets: list[BtcMarket] = []

        # 1) Live 5-minute windows by deterministic slug
        if "btc-up-or-down-5m" in series_slugs:
            markets.extend(self._fetch_live_5m_by_slug())

        # 2) Series list as backup / longer horizons
        if not markets:
            for slug in series_slugs:
                series_markets = self._fetch_by_series(slug)
                markets.extend(series_markets)
                live = [m for m in series_markets if m.seconds_to_end > 30]
                if live and slug == "btc-up-or-down-5m":
                    break

        if not markets:
            logger.info("Series lookup returned nothing, trying keyword fallback")
            markets = self._fetch_by_keyword()

        # Drop expired / ending immediately
        now = time.time()
        live_markets = []
        for m in markets:
            end_ts = _parse_end_ts(m.end_date)
            m.seconds_to_end = max(0.0, end_ts - now) if end_ts else 0.0
            if end_ts and end_ts <= now + 15:
                continue
            # Enrich prices if missing / default
            if m.tokens and (
                m.yes_price <= 0
                or m.no_price <= 0
                or abs(m.yes_price + m.no_price - 1.0) > 0.25
                or (m.yes_price == 0.5 and m.no_price == 0.5)
            ):
                self._enrich_prices_from_clob(m)
            live_markets.append(m)

        logger.info(f"Found {len(live_markets)} live BTC markets (from {len(markets)} raw)")
        return live_markets

    def _fetch_live_5m_by_slug(self) -> list[BtcMarket]:
        """
        Polymarket 5m BTC windows use slug: btc-updown-5m-{unix_start}.
        Window starts every 300s. Fetch current + next few windows.
        """
        markets: list[BtcMarket] = []
        now = time.time()
        window = int(now // 300) * 300
        # previous (may still be resolving), current, next few
        starts = [window + i * 300 for i in range(-1, 6)]
        for start in starts:
            slug = f"btc-updown-5m-{start}"
            try:
                resp = self.session.get(
                    f"{self.gamma_url}/events/slug/{slug}",
                    timeout=12,
                )
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                event = resp.json()
                if not isinstance(event, dict):
                    continue
                parsed = self._markets_from_event(event, series_slug="btc-up-or-down-5m")
                markets.extend(parsed)
            except requests.RequestException as e:
                logger.debug(f"slug {slug}: {e}")
            except Exception as e:
                logger.debug(f"slug parse {slug}: {e}")

        logger.info(f"Slug-based 5m fetch: {len(markets)} markets")
        return markets

    def _markets_from_event(self, event: dict, series_slug: str) -> list[BtcMarket]:
        """Parse one gamma event object into BtcMarket list."""
        markets: list[BtcMarket] = []
        now = time.time()
        event_markets = event.get("markets", []) or []
        event_id = str(event.get("id", ""))

        price_to_beat = 0.0
        metadata = event.get("eventMetadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        if isinstance(metadata, dict):
            price_to_beat = _parse_float(metadata.get("priceToBeat", 0))

        for m in event_markets:
            if m.get("closed") is True:
                continue
            if m.get("active") is False:
                continue

            end_date = m.get("endDate") or event.get("endDate") or ""
            end_ts = _parse_end_ts(end_date)
            if end_ts and end_ts < now - 60:
                continue

            clob_ids = _parse_json_field(m.get("clobTokenIds"), [])
            outcomes = _parse_json_field(m.get("outcomes"), [])
            outcome_prices = _parse_json_field(m.get("outcomePrices"), [])

            tokens = []
            yes_price = 0.5
            no_price = 0.5

            for i, tid in enumerate(clob_ids):
                outcome = outcomes[i] if i < len(outcomes) else f"outcome_{i}"
                tokens.append({
                    "token_id": str(tid),
                    "outcome": str(outcome),
                })

            if outcome_prices and len(outcome_prices) >= 2:
                mapped = {}
                for i, oc in enumerate(outcomes):
                    if i < len(outcome_prices):
                        mapped[str(oc).lower()] = _parse_float(outcome_prices[i], 0.5)
                if "up" in mapped or "yes" in mapped:
                    yes_price = mapped.get("up", mapped.get("yes", 0.5))
                    no_price = mapped.get("down", mapped.get("no", 1.0 - yes_price))
                else:
                    yes_price = _parse_float(outcome_prices[0], 0.5)
                    no_price = _parse_float(outcome_prices[1], 0.5)

            best_bid = _parse_float(m.get("bestBid"), 0)
            best_ask = _parse_float(m.get("bestAsk"), 0)
            if (yes_price == 0.5 and no_price == 0.5) and best_bid > 0 and 0 < best_ask < 1:
                yes_price = (best_bid + best_ask) / 2.0
                no_price = max(0.01, min(0.99, 1.0 - yes_price))

            markets.append(BtcMarket(
                condition_id=str(m.get("conditionId", m.get("id", ""))),
                question=m.get("question", event.get("title", "")),
                market_slug=m.get("slug", event.get("slug", "")),
                tokens=tokens,
                end_date=end_date,
                active=True,
                yes_price=yes_price,
                no_price=no_price,
                volume=_parse_float(m.get("volume", 0)),
                liquidity=_parse_float(m.get("liquidity", 0)),
                description=m.get("description", "") or "",
                series_slug=series_slug,
                price_to_beat=price_to_beat,
                event_id=event_id,
                seconds_to_end=max(0.0, end_ts - now) if end_ts else 0.0,
            ))
        return markets

    def _fetch_by_series(self, series_slug: str) -> list[BtcMarket]:
        """Fetch markets by Polymarket series slug."""
        markets = []
        try:
            # Fetch newest first — ascending endDate returns ancient windows first
            resp = self.session.get(
                f"{self.gamma_url}/events",
                params={
                    "active": "true",
                    "closed": "false",
                    "series_slug": series_slug,
                    "limit": 20,
                    "order": "id",
                    "ascending": "false",
                },
                timeout=15,
            )
            resp.raise_for_status()
            events = resp.json()
            if not isinstance(events, list):
                logger.error(f"Unexpected events payload type: {type(events)}")
                return []

            now = time.time()
            for event in events:
                event_markets = event.get("markets", []) or []
                event_id = str(event.get("id", ""))

                price_to_beat = 0.0
                metadata = event.get("eventMetadata") or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except (json.JSONDecodeError, TypeError):
                        metadata = {}
                if isinstance(metadata, dict):
                    price_to_beat = _parse_float(metadata.get("priceToBeat", 0))

                for m in event_markets:
                    if m.get("closed") is True:
                        continue
                    if m.get("active") is False:
                        continue

                    end_date = m.get("endDate") or event.get("endDate") or ""
                    end_ts = _parse_end_ts(end_date)
                    # Skip clearly expired windows
                    if end_ts and end_ts < now - 60:
                        continue

                    clob_ids = _parse_json_field(m.get("clobTokenIds"), [])
                    outcomes = _parse_json_field(m.get("outcomes"), [])
                    outcome_prices = _parse_json_field(m.get("outcomePrices"), [])

                    tokens = []
                    yes_price = 0.5
                    no_price = 0.5

                    for i, tid in enumerate(clob_ids):
                        outcome = outcomes[i] if i < len(outcomes) else f"outcome_{i}"
                        tokens.append({
                            "token_id": str(tid),
                            "outcome": str(outcome),
                        })

                    # Map prices by outcome name when possible
                    if outcome_prices and len(outcome_prices) >= 2:
                        try:
                            # Prefer name-based mapping
                            mapped = {}
                            for i, oc in enumerate(outcomes):
                                if i < len(outcome_prices):
                                    mapped[str(oc).lower()] = _parse_float(outcome_prices[i], 0.5)
                            if "up" in mapped or "yes" in mapped:
                                yes_price = mapped.get("up", mapped.get("yes", 0.5))
                                no_price = mapped.get("down", mapped.get("no", 1.0 - yes_price))
                            else:
                                yes_price = _parse_float(outcome_prices[0], 0.5)
                                no_price = _parse_float(outcome_prices[1], 0.5)
                        except (ValueError, TypeError) as e:
                            logger.debug(f"Price parse error: {e}")

                    # Fallback mid from bestBid/bestAsk if present
                    best_bid = _parse_float(m.get("bestBid"), 0)
                    best_ask = _parse_float(m.get("bestAsk"), 0)
                    if (yes_price == 0.5 and no_price == 0.5) and best_bid > 0 and best_ask > 0:
                        yes_price = (best_bid + best_ask) / 2.0
                        no_price = max(0.01, min(0.99, 1.0 - yes_price))

                    market = BtcMarket(
                        condition_id=str(m.get("conditionId", m.get("id", ""))),
                        question=m.get("question", event.get("title", "")),
                        market_slug=m.get("slug", event.get("slug", "")),
                        tokens=tokens,
                        end_date=end_date,
                        active=True,
                        yes_price=yes_price,
                        no_price=no_price,
                        volume=_parse_float(m.get("volume", 0)),
                        liquidity=_parse_float(m.get("liquidity", 0)),
                        description=m.get("description", "") or "",
                        series_slug=series_slug,
                        price_to_beat=price_to_beat,
                        event_id=event_id,
                        seconds_to_end=max(0.0, end_ts - now) if end_ts else 0.0,
                    )
                    markets.append(market)

            logger.info(f"Series '{series_slug}': found {len(markets)} non-expired markets")

        except requests.RequestException as e:
            logger.error(f"Failed to fetch series '{series_slug}': {e}")
        except Exception as e:
            logger.error(f"Unexpected error fetching series '{series_slug}': {e}")

        return markets

    def _enrich_prices_from_clob(self, market: BtcMarket) -> None:
        """Fill yes/no mid prices from CLOB order book."""
        if not market.tokens:
            return
        try:
            up_token = None
            down_token = None
            for t in market.tokens:
                oc = str(t.get("outcome", "")).lower()
                if oc in ("up", "yes"):
                    up_token = t.get("token_id")
                elif oc in ("down", "no"):
                    down_token = t.get("token_id")
            if not up_token and market.tokens:
                up_token = market.tokens[0].get("token_id")
            if not down_token and len(market.tokens) > 1:
                down_token = market.tokens[1].get("token_id")

            if up_token:
                mid = self._mid_from_book(str(up_token))
                if mid is not None:
                    market.yes_price = mid
                    market.no_price = max(0.01, min(0.99, 1.0 - mid))
            if down_token and (market.no_price <= 0 or market.no_price == 0.5):
                mid_d = self._mid_from_book(str(down_token))
                if mid_d is not None:
                    market.no_price = mid_d
                    if market.yes_price == 0.5:
                        market.yes_price = max(0.01, min(0.99, 1.0 - mid_d))
        except Exception as e:
            logger.debug(f"CLOB price enrich failed: {e}")

    def _mid_from_book(self, token_id: str) -> Optional[float]:
        book = self.fetch_market_orderbook(token_id)
        bids = book.get("bids") or []
        asks = book.get("asks") or []

        def best_price(levels, side: str) -> Optional[float]:
            if not levels:
                return None
            prices = []
            for lv in levels:
                if isinstance(lv, dict):
                    prices.append(_parse_float(lv.get("price")))
                elif isinstance(lv, (list, tuple)) and lv:
                    prices.append(_parse_float(lv[0]))
            prices = [p for p in prices if p > 0]
            if not prices:
                return None
            return max(prices) if side == "bid" else min(prices)

        bid = best_price(bids, "bid")
        ask = best_price(asks, "ask")
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        if bid is not None:
            return bid
        if ask is not None:
            return ask
        return None

    def _fetch_by_keyword(self) -> list[BtcMarket]:
        """Fallback: keyword-based search for BTC markets."""
        markets = []
        try:
            params = {
                "active": "true",
                "closed": "false",
                "limit": 50,
                "order": "endDate",
                "ascending": "true",
                "tag": "crypto",
            }
            resp = self.session.get(
                f"{self.gamma_url}/markets",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                return []

            now = time.time()
            for m in data:
                question = m.get("question", "").lower()
                desc = m.get("description", "").lower()
                combined = f"{question} {desc}"

                has_btc = any(kw in combined for kw in ["btc", "bitcoin"])
                has_direction = any(kw in combined for kw in [
                    "up", "down", "above", "below", "higher", "lower"
                ])
                if not (has_btc and has_direction):
                    continue

                end_date = m.get("endDate", "") or ""
                end_ts = _parse_end_ts(end_date)
                if end_ts and end_ts < now - 60:
                    continue

                clob_ids = _parse_json_field(m.get("clobTokenIds"), [])
                outcomes = _parse_json_field(m.get("outcomes"), [])
                outcome_prices = _parse_json_field(m.get("outcomePrices"), [])

                tokens = []
                yes_price = 0.5
                no_price = 0.5
                if outcome_prices and len(outcome_prices) >= 2:
                    yes_price = _parse_float(outcome_prices[0], 0.5)
                    no_price = _parse_float(outcome_prices[1], 0.5)

                for i, tid in enumerate(clob_ids):
                    tokens.append({
                        "token_id": str(tid),
                        "outcome": outcomes[i] if i < len(outcomes) else f"outcome_{i}",
                    })

                markets.append(BtcMarket(
                    condition_id=str(m.get("conditionId", m.get("id", ""))),
                    question=m.get("question", ""),
                    market_slug=m.get("slug", ""),
                    tokens=tokens,
                    end_date=end_date,
                    active=bool(m.get("active", False)),
                    yes_price=yes_price,
                    no_price=no_price,
                    volume=_parse_float(m.get("volume", 0)),
                    liquidity=_parse_float(m.get("liquidity", 0)),
                    description=m.get("description", "") or "",
                    seconds_to_end=max(0.0, end_ts - now) if end_ts else 0.0,
                ))

        except requests.RequestException as e:
            logger.error(f"Keyword search failed: {e}")

        return markets

    def fetch_market_orderbook(self, token_id: str) -> dict:
        """Fetch orderbook for a specific token from CLOB API."""
        try:
            resp = self.session.get(
                f"{self.clob_url}/book",
                params={"token_id": token_id},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch orderbook for {token_id}: {e}")
            return {"bids": [], "asks": []}

    def get_best_market(self, markets: list[BtcMarket]) -> Optional[BtcMarket]:
        """
        Select best market for a 5m-style trade:
        - Prefer 5m series
        - Prefer windows with 45s–4.5m remaining (not already decided, not too late)
        - Prefer higher liquidity when available
        """
        if not markets:
            return None

        def score(m: BtcMarket) -> tuple:
            series_pref = 0 if m.series_slug == "btc-up-or-down-5m" else (
                1 if m.series_slug == "btc-up-or-down-15m" else 2
            )
            # Ideal remaining time ~ 60-240s for 5m
            t = m.seconds_to_end
            if t <= 0:
                time_score = 10_000
            elif 45 <= t <= 270:
                time_score = abs(150 - t)  # closer to 2.5m remaining better
            elif t < 45:
                time_score = 5_000 + (45 - t)
            else:
                time_score = t  # too early / longer windows lower priority
            liq = -m.liquidity
            return (series_pref, time_score, liq, m.end_date)

        viable = [m for m in markets if m.seconds_to_end > 30]
        if not viable:
            viable = markets

        viable.sort(key=score)
        best = viable[0]
        logger.info(
            f"Best market: {best.question[:70]} | "
            f"t_left={best.seconds_to_end:.0f}s | "
            f"UP={best.yes_price:.3f} DOWN={best.no_price:.3f} | "
            f"liq={best.liquidity:.0f}"
        )
        return best
