"""
Rocky Trading System - Market Scanner
Fetches BTC Up/Down 5-minute markets from Polymarket via series slug.
"""

import time
import logging
import requests
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger("rocky.scanner")

# Supported Polymarket BTC series (primary → secondary)
BTC_SERIES = [
    "btc-up-or-down-5m",
    "btc-up-or-down-15m",
    "btc-up-or-down-hourly",
]


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
            "User-Agent": "Rocky-Trader/0.1",
            "Accept": "application/json",
        })

    def fetch_btc_markets(self, series_slugs: list[str] = None) -> list[BtcMarket]:
        """
        Fetch active BTC markets from Polymarket.
        Primary: query by series_slug for exact matches.
        Fallback: keyword-based search.
        """
        if series_slugs is None:
            series_slugs = BTC_SERIES

        markets = []

        # Primary: series-based lookup (accurate)
        for slug in series_slugs:
            series_markets = self._fetch_by_series(slug)
            markets.extend(series_markets)
            if markets:
                break  # Found markets in preferred series, stop

        # Fallback: keyword search if series lookup fails
        if not markets:
            logger.info("Series lookup returned nothing, trying keyword fallback")
            markets = self._fetch_by_keyword()

        logger.info(f"Found {len(markets)} active BTC markets")
        return markets

    def _fetch_by_series(self, series_slug: str) -> list[BtcMarket]:
        """Fetch markets by Polymarket series slug."""
        markets = []
        try:
            resp = self.session.get(
                f"{self.gamma_url}/events",
                params={
                    "active": "true",
                    "closed": "false",
                    "series_slug": series_slug,
                    "limit": 10,
                    "order": "endDate",
                    "ascending": "true",
                },
                timeout=15,
            )
            resp.raise_for_status()
            events = resp.json()

            for event in events:
                event_markets = event.get("markets", [])
                event_id = event.get("id", "")

                # Extract priceToBeat from event metadata
                price_to_beat = 0.0
                metadata = event.get("eventMetadata", {})
                if isinstance(metadata, str):
                    import json
                    try:
                        metadata = json.loads(metadata)
                    except (json.JSONDecodeError, TypeError):
                        metadata = {}
                price_to_beat = float(metadata.get("priceToBeat", 0))

                for m in event_markets:
                    if not m.get("active", False):
                        continue

                    tokens = []
                    clob_ids = m.get("clobTokenIds", [])
                    outcomes = m.get("outcomes", [])
                    outcome_prices = m.get("outcomePrices", [])

                    yes_price = 0.5
                    no_price = 0.5

                    for i, tid in enumerate(clob_ids):
                        outcome = outcomes[i] if i < len(outcomes) else f"outcome_{i}"
                        tokens.append({
                            "token_id": tid,
                            "outcome": outcome,
                        })
                        # Map prices: first outcome = Up/Yes, second = Down/No
                        if i < len(outcome_prices):
                            try:
                                price = float(outcome_prices[i])
                                if i == 0:
                                    yes_price = price
                                elif i == 1:
                                    no_price = price
                            except (ValueError, TypeError) as e:
                                logger.debug(f"Price parse error: {e}")

                    market = BtcMarket(
                        condition_id=m.get("conditionId", m.get("id", "")),
                        question=m.get("question", event.get("title", "")),
                        market_slug=m.get("slug", ""),
                        tokens=tokens,
                        end_date=m.get("endDate", event.get("endDate", "")),
                        active=True,
                        yes_price=yes_price,
                        no_price=no_price,
                        volume=float(m.get("volume", 0)),
                        liquidity=float(m.get("liquidity", 0)),
                        description=m.get("description", ""),
                        series_slug=series_slug,
                        price_to_beat=price_to_beat,
                        event_id=event_id,
                    )
                    markets.append(market)

            logger.info(f"Series '{series_slug}': found {len(markets)} markets")

        except requests.RequestException as e:
            logger.error(f"Failed to fetch series '{series_slug}': {e}")
        except Exception as e:
            logger.error(f"Unexpected error fetching series '{series_slug}': {e}")

        return markets

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

                tokens = []
                clob_ids = m.get("clobTokenIds", [])
                outcomes = m.get("outcomes", [])
                outcome_prices = m.get("outcomePrices", [])

                yes_price = 0.5
                no_price = 0.5
                if outcome_prices and len(outcome_prices) >= 2:
                    try:
                        yes_price = float(outcome_prices[0])
                        no_price = float(outcome_prices[1])
                    except (ValueError, TypeError) as e:
                        logger.debug(f"Price parse error: {e}")

                for i, tid in enumerate(clob_ids):
                    tokens.append({
                        "token_id": tid,
                        "outcome": outcomes[i] if i < len(outcomes) else f"outcome_{i}",
                    })

                market = BtcMarket(
                    condition_id=m.get("conditionId", m.get("id", "")),
                    question=m.get("question", ""),
                    market_slug=m.get("slug", ""),
                    tokens=tokens,
                    end_date=m.get("endDate", ""),
                    active=m.get("active", False),
                    yes_price=yes_price,
                    no_price=no_price,
                    volume=float(m.get("volume", 0)),
                    liquidity=float(m.get("liquidity", 0)),
                    description=m.get("description", ""),
                )
                markets.append(market)

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
        """Select the best market — nearest expiry with sufficient liquidity."""
        if not markets:
            return None

        # Prefer markets with liquidity > 50, fall back to any
        viable = [m for m in markets if m.liquidity > 50]
        if not viable:
            viable = markets

        # Nearest expiry first
        viable.sort(key=lambda m: m.end_date)
        return viable[0]
