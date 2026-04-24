"""
Rocky Trading System - V2 LLM Decision Engine
Uses Opus 4.6 via OpenAI-compatible API as the core decision maker.
No hardcoded trade signals. The model receives raw market data and reasons.

Drop-in replacement for decision.py — same TradeSignal output interface.
"""

import os
import json
import time
import logging
import requests
from typing import Optional

from .intelligence import BtcSnapshot
from .scanner import BtcMarket
from .models import TradeSignal

logger = logging.getLogger("rocky.decision_v2")


# ── System prompt — Rocky's trading brain ────────────────────────────────────

SYSTEM_PROMPT = """You are Rocky, an autonomous BTC prediction market trader. You analyze raw market data and decide whether Bitcoin will go UP or DOWN in the next 5 minutes.

You are trading on Polymarket's BTC 5-minute prediction markets. You buy YES tokens if you think BTC goes up, NO tokens if you think it goes down, or SKIP if there's no clear edge.

## How These Markets Resolve
- The market resolves based on the Binance BTC/USDT 5-minute candle.
- UP wins if candle CLOSE >= candle OPEN (the "price to beat").
- DOWN wins if candle CLOSE < candle OPEN.
- Even a $0.01 difference matters. This is binary.

## Your Decision Framework

You think in probabilities. You look for:
1. **Momentum** — Is price accelerating in one direction? Are recent candles building?
2. **Trend alignment** — Do 1m, 5m, 15m timeframes agree?
3. **Volatility regime** — High vol = bigger moves = more opportunity. Low vol = chop = danger.
4. **News catalysts** — Breaking news that hasn't been priced in yet.
5. **Market mispricing** — Are Polymarket odds stale vs what the data shows?
6. **Mean reversion signals** — Has price overextended and likely to snap back?
7. **Volume confirmation** — Is the move backed by volume or is it a fakeout?
8. **Price to beat** — Where is the candle open? Is current price above or below it?

## Rules
- You MUST output valid JSON. Nothing else.
- Confidence is 0-100. Only trade if genuinely confident (65+).
- SKIP is always valid. No trade is better than a bad trade.
- Be honest about uncertainty. Don't force a trade.
- Short reasoning (2-4 bullet points). Be specific about what data drove the decision.

## Output Format (strict JSON)
```json
{
  "direction": "up" | "down" | "skip",
  "confidence": 0-100,
  "reasoning": [
    "specific reason 1",
    "specific reason 2",
    "specific reason 3"
  ]
}
```"""


# ── Build the analysis prompt with all raw data ─────────────────────────────

def build_analysis_prompt(
    market: BtcMarket,
    snapshot: BtcSnapshot,
    orderbook: dict,
    news_headlines: list[str],
) -> str:
    """Build the data-rich prompt for the LLM."""

    # Format klines as a readable table
    klines_str = ""
    if snapshot.raw_klines:
        recent = snapshot.raw_klines[-15:]  # Last 15 one-minute candles
        klines_str = "| Time Ago | Open | High | Low | Close | Volume |\n"
        klines_str += "|----------|------|------|-----|-------|--------|\n"
        for i, k in enumerate(reversed(recent)):
            mins_ago = i + 1
            klines_str += (
                f"| {mins_ago}m ago | "
                f"${k['open']:,.2f} | ${k['high']:,.2f} | "
                f"${k['low']:,.2f} | ${k['close']:,.2f} | "
                f"{k['volume']:,.1f} |\n"
            )

    # Format orderbook
    book_str = "Not available"
    if orderbook.get("bids") or orderbook.get("asks"):
        bids = orderbook.get("bids", [])[:5]
        asks = orderbook.get("asks", [])[:5]
        book_str = "Bids (buy YES/Up):\n"
        for b in bids:
            book_str += f"  ${b.get('price', '?')} × {b.get('size', '?')}\n"
        book_str += "Asks (sell YES/Up):\n"
        for a in asks:
            book_str += f"  ${a.get('price', '?')} × {a.get('size', '?')}\n"

    # Format news
    news_str = "No recent headlines"
    if news_headlines:
        news_str = "\n".join(f"- {h}" for h in news_headlines[:8])

    # Price to beat info
    ptb_str = "Not available"
    if hasattr(market, 'price_to_beat') and market.price_to_beat > 0:
        diff = snapshot.price_usd - market.price_to_beat
        pct = (diff / market.price_to_beat) * 100 if market.price_to_beat > 0 else 0
        ptb_str = (
            f"${market.price_to_beat:,.2f} "
            f"(current is ${diff:+,.2f} / {pct:+.4f}% from open)"
        )

    prompt = f"""## MARKET DATA — {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}

### BTC Price
- **Current:** ${snapshot.price_usd:,.2f}
- **1m change:** {snapshot.price_change_1m:+.4f}%
- **5m change:** {snapshot.price_change_5m:+.4f}%
- **15m change:** {snapshot.price_change_15m:+.4f}%
- **1h change:** {snapshot.price_change_1h:+.4f}%
- **24h high:** ${snapshot.high_24h:,.2f}
- **24h low:** ${snapshot.low_24h:,.2f}
- **24h volume:** {snapshot.volume_24h:,.0f} BTC

### Recent 1-Minute Candles (BTCUSDT)
{klines_str if klines_str else "Not available"}

### Momentum & Volatility
- **Momentum:** {snapshot.momentum}
- **Volatility:** {snapshot.volatility}
- **Multi-TF trend:** {snapshot.trend_direction}

### Polymarket Market
- **Question:** {market.question}
- **Series:** {getattr(market, 'series_slug', 'unknown')}
- **Price to beat (candle open):** {ptb_str}
- **YES/Up price:** {market.yes_price:.4f} ({market.yes_price:.0%} implied)
- **NO/Down price:** {market.no_price:.4f} ({market.no_price:.0%} implied)
- **Volume:** ${market.volume:,.2f}
- **Liquidity:** ${market.liquidity:,.2f}
- **Ends:** {market.end_date}

### Polymarket Orderbook
{book_str}

### Latest BTC News
{news_str}

---

Analyze ALL the data above. What is your trade decision for the next 5 minutes?
Output ONLY valid JSON."""

    return prompt


# ── LLM Decision Engine ─────────────────────────────────────────────────────

class DecisionEngineV2:
    """Uses Opus 4.6 via OpenAI-compatible API as the decision maker."""

    def __init__(self, config):
        self.config = config
        self.session = requests.Session()

        # LLM API configuration (OpenAI-compatible)
        self.api_url = os.environ.get(
            "LLM_API_URL",
            "https://api.enowxlabs.com/v1/chat/completions"
        )
        self.api_key = os.environ.get("LLM_API_KEY", "")
        self.model = os.environ.get("LLM_MODEL", "enowxlabs/claude-opus-4.6")
        self.max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "1024"))
        self.temperature = float(os.environ.get("LLM_TEMPERATURE", "0.2"))

        if not self.api_key:
            logger.warning(
                "LLM_API_KEY not set! V2 decision engine will not work. "
                "Set LLM_API_KEY in .env or environment."
            )

    def analyze(
        self,
        market: BtcMarket,
        snapshot: BtcSnapshot,
        orderbook: dict = None,
    ) -> Optional[TradeSignal]:
        """Send all market data to Opus 4.6 and get a trade decision."""
        if not self.api_key:
            logger.error("No LLM API key configured. Cannot make decisions.")
            return None

        if snapshot.price_usd <= 0:
            logger.warning("No price data, skipping LLM analysis")
            return None

        # Build the prompt with all raw data
        user_prompt = build_analysis_prompt(
            market=market,
            snapshot=snapshot,
            orderbook=orderbook or {},
            news_headlines=snapshot.news_headlines,
        )

        # Call the LLM
        llm_response = self._call_llm(user_prompt)
        if not llm_response:
            logger.error("LLM call failed, no trade this cycle")
            return None

        # Parse the response
        decision = self._parse_response(llm_response)
        if not decision:
            logger.error(f"Failed to parse LLM response: {llm_response[:200]}")
            return None

        direction = decision.get("direction", "skip").lower()
        confidence = min(max(decision.get("confidence", 0), 0), 100) / 100.0
        reasoning = decision.get("reasoning", [])

        if direction not in ("up", "down", "skip"):
            logger.warning(f"Invalid direction '{direction}', treating as skip")
            direction = "skip"

        # Select token and price
        token_id, expected_price = self._select_token(market, direction)

        # Position sizing (code-enforced, not LLM-decided)
        stake_pct = self.config.get_position_size_pct(confidence)

        signal = TradeSignal(
            direction=direction,
            confidence=confidence,
            reasoning=reasoning,
            market=market,
            snapshot=snapshot,
            token_id=token_id,
            expected_price=expected_price,
            stake_pct=stake_pct,
            raw_llm_response=llm_response,
            candle_open_price=getattr(market, 'price_to_beat', 0.0),
        )

        logger.info(signal.summary())
        return signal

    def _call_llm(self, user_prompt: str) -> Optional[str]:
        """Call the LLM via OpenAI-compatible chat completions API."""
        start = time.time()

        try:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }

            payload = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            }

            resp = self.session.post(
                self.api_url,
                headers=headers,
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            # OpenAI-compatible response format
            text = data["choices"][0]["message"]["content"]

            elapsed = time.time() - start
            logger.info(f"LLM responded in {elapsed:.1f}s ({len(text)} chars)")

            return text

        except requests.exceptions.Timeout:
            logger.error("LLM API timeout (30s)")
            return None
        except requests.exceptions.HTTPError as e:
            body = ""
            if hasattr(e, "response") and e.response is not None:
                body = e.response.text[:300]
            logger.error(f"LLM API error: {e} — {body}")
            return None
        except (KeyError, IndexError) as e:
            logger.error(f"Unexpected LLM response format: {e}")
            return None
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return None

    def _parse_response(self, response: str) -> Optional[dict]:
        """Parse the LLM's JSON response, handling markdown fences."""
        text = response.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Try to find JSON object in the response
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end])
                except json.JSONDecodeError as e:
                    logger.debug(f"JSON parse error: {e}")

        logger.error("Could not parse JSON from LLM response")
        return None

    def _select_token(self, market: BtcMarket, direction: str) -> tuple[str, float]:
        """Select which token to buy based on direction."""
        if direction == "skip" or not market.tokens:
            return ("", 0.0)

        if direction == "up":
            token_id = market.tokens[0]["token_id"] if market.tokens else ""
            price = market.yes_price
        else:
            token_id = market.tokens[1]["token_id"] if len(market.tokens) > 1 else ""
            price = market.no_price

        return (token_id, price)
