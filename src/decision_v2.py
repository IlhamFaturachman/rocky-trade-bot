"""
Rocky Trading System - V2 LLM Decision Engine
OpenAI-compatible API (Liam farm g2a / any chat completions endpoint).

Hardened for Polymarket BTC 5m:
- Edge vs market price required (not just direction)
- Skip when odds already extreme / no edge
- EV-aware confidence
- Strict JSON parse + code-side risk gates
"""

import os
import json
import time
import logging
import re
import requests
from typing import Optional

from .intelligence import BtcSnapshot
from .scanner import BtcMarket
from .models import TradeSignal

logger = logging.getLogger("rocky.decision_v2")


SYSTEM_PROMPT = """You are Rocky, a professional short-horizon BTC prediction-market trader.

You trade Polymarket BTC Up/Down windows (usually 5 minutes). Resolution is binary on the Binance BTC/USDT candle for that window:
- UP wins if CLOSE >= OPEN (price to beat)
- DOWN wins if CLOSE < OPEN
A $0.01 difference decides the market.

## Your ONLY job
Estimate P(UP) and P(DOWN), compare to Polymarket token prices, and output a direction whenever there is ANY directional lean — even a small one. Do NOT default to skip.

## Edge definition (compute honestly)
edge_up = your_p_up - yes_price
edge_down = your_p_down - no_price
Prefer trades where the chosen edge >= 0.06. If the edge is smaller but a directional lean still exists, output the direction with confidence 55-62 and the computed (smaller) edge. The executor gates on edge separately — your job is to report direction + edge truthfully, not to pre-filter.

## When to SKIP (rare)
Only SKIP when there is genuinely NO directional information: flat tape, price sitting exactly on the open, equal odds, zero displacement, conflicting signals that cancel out. A slight lean is tradeable — output it.

## What actually matters for 5 minutes (priority order)
1. Distance of CURRENT price vs PRICE TO BEAT (signed $ and bps) — the dominant signal
2. Last 1-3 one-minute candles: direction, range, volume expansion/contraction
3. Micro-momentum vs mean-reversion (overextended wick into thin volume = fade risk)
4. Multi-TF alignment (1m/5m/15m) — agreement helps, disagreement -> lower confidence
5. Polymarket mispricing / stale book vs spot
6. News ONLY if clearly breaking and not already in the move

Do NOT:
- Default to skip when uncertain — pick the slight-lean direction with low confidence instead
- Confuse 24h narrative with 5-minute edge
- Output confidence > 80 unless multiple independent factors align AND edge >= 0.10
- Invent data not present in the prompt

## Confidence calibration
- 55-62: slight lean, small edge, modest displacement — still output direction (do NOT skip)
- 65-74: small edge, clean direction, modest displacement
- 75-84: strong alignment + clear edge
- 85-92: rare; only with large edge and clean tape
Never output 93-100. Never output below 55 unless you are genuinely skipping with zero directional info.

## Output
Return ONLY valid JSON (no markdown):
{
  "direction": "up" | "down" | "skip",
  "confidence": 0-100,
  "p_up": 0.0-1.0,
  "edge": -1.0-1.0,
  "reasoning": ["specific factor 1", "specific factor 2", "specific factor 3"]
}

edge should be the edge for the chosen side (positive if trading). For skip, edge can be 0.
"""


def build_analysis_prompt(
    market: BtcMarket,
    snapshot: BtcSnapshot,
    orderbook: dict,
    news_headlines: list[str],
) -> str:
    """Build the data-rich prompt for the LLM."""

    klines_str = ""
    if snapshot.raw_klines:
        recent = snapshot.raw_klines[-15:]
        klines_str = "| Mins ago | Open | High | Low | Close | Volume |\n"
        klines_str += "|----------|------|------|-----|-------|--------|\n"
        for i, k in enumerate(reversed(recent)):
            mins_ago = i + 1
            klines_str += (
                f"| {mins_ago}m | "
                f"${k['open']:,.2f} | ${k['high']:,.2f} | "
                f"${k['low']:,.2f} | ${k['close']:,.2f} | "
                f"{k['volume']:,.1f} |\n"
            )

    book_str = "Not available"
    if orderbook.get("bids") or orderbook.get("asks"):
        bids = orderbook.get("bids", [])[:5]
        asks = orderbook.get("asks", [])[:5]
        book_str = "Bids (buy YES/Up):\n"
        for b in bids:
            if isinstance(b, dict):
                book_str += f"  ${b.get('price', '?')} × {b.get('size', '?')}\n"
            else:
                book_str += f"  {b}\n"
        book_str += "Asks (sell YES/Up):\n"
        for a in asks:
            if isinstance(a, dict):
                book_str += f"  ${a.get('price', '?')} × {a.get('size', '?')}\n"
            else:
                book_str += f"  {a}\n"

    news_str = "No recent headlines"
    if news_headlines:
        news_str = "\n".join(f"- {h}" for h in news_headlines[:8])

    ptb_str = "Not available"
    ptb = getattr(market, "price_to_beat", 0.0) or 0.0
    if ptb <= 0 and snapshot.raw_klines:
        # Approximate current 5m open from last closed 5m boundary using 1m bars
        # (best-effort when Polymarket metadata missing)
        try:
            # use first of last 5 one-minute opens as rough proxy
            ptb = float(snapshot.raw_klines[-5]["open"])
        except Exception:
            ptb = 0.0
    if ptb > 0 and snapshot.price_usd > 0:
        diff = snapshot.price_usd - ptb
        pct = (diff / ptb) * 100
        bps = pct * 100
        ptb_str = (
            f"${ptb:,.2f} | spot ${snapshot.price_usd:,.2f} | "
            f"delta ${diff:+,.2f} ({pct:+.4f}% / {bps:+.1f} bps) | "
            f"side_of_open={'ABOVE' if diff >= 0 else 'BELOW'}"
        )

    t_left = getattr(market, "seconds_to_end", 0.0) or 0.0
    # Fair-ish mid if both sides present
    mkt_sum = (market.yes_price or 0) + (market.no_price or 0)

    prompt = f"""## MARKET DATA — {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}

### BTC Spot (Binance BTCUSDT)
- **Current:** ${snapshot.price_usd:,.2f}
- **1m change:** {snapshot.price_change_1m:+.4f}%
- **5m change:** {snapshot.price_change_5m:+.4f}%
- **15m change:** {snapshot.price_change_15m:+.4f}%
- **1h change:** {snapshot.price_change_1h:+.4f}%
- **24h high/low:** ${snapshot.high_24h:,.2f} / ${snapshot.low_24h:,.2f}
- **24h volume:** {snapshot.volume_24h:,.0f} BTC
- **Momentum label:** {snapshot.momentum}
- **Volatility label:** {snapshot.volatility}
- **Multi-TF trend:** {snapshot.trend_direction}

### Recent 1-Minute Candles
{klines_str if klines_str else "Not available"}

### Polymarket Window
- **Question:** {market.question}
- **Series:** {getattr(market, 'series_slug', 'unknown')}
- **Seconds remaining:** {t_left:.0f}s
- **Price to beat / open context:** {ptb_str}
- **YES/Up price:** {market.yes_price:.4f} ({market.yes_price:.1%} implied)
- **NO/Down price:** {market.no_price:.4f} ({market.no_price:.1%} implied)
- **yes+no sum:** {mkt_sum:.4f} (≈1.0 healthy; far from 1.0 = stale/illiquid)
- **Volume:** ${market.volume:,.2f}
- **Liquidity:** ${market.liquidity:,.2f}
- **Ends:** {market.end_date}

### Polymarket Orderbook (YES/Up token)
{book_str}

### Latest BTC News (often lagging for 5m — use carefully)
{news_str}

---
Compute p_up, compare to YES price, apply edge rules, then decide.
Output ONLY valid JSON.
"""
    return prompt


class DecisionEngineV2:
    """LLM decision maker via OpenAI-compatible chat completions."""

    def __init__(self, config):
        self.config = config
        self.session = requests.Session()

        self.api_url = os.environ.get(
            "LLM_API_URL",
            "http://127.0.0.1:8080/v1/chat/completions",
        )
        self.api_key = os.environ.get("LLM_API_KEY", "")
        self.model = os.environ.get("LLM_MODEL", "grok-4.5")
        # Keep completion short — long max_tokens + reasoning models = timeouts
        self.max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "350"))
        self.temperature = float(os.environ.get("LLM_TEMPERATURE", "0.15"))
        self.min_edge = float(os.environ.get("ROCKY_MIN_EDGE", "0.06"))
        self.max_entry_price = float(os.environ.get("ROCKY_MAX_ENTRY_PRICE", "0.72"))
        self.min_entry_price = float(os.environ.get("ROCKY_MIN_ENTRY_PRICE", "0.28"))
        self.timeout = int(os.environ.get("LLM_TIMEOUT_SECONDS", "45"))
        self.max_attempts = int(os.environ.get("LLM_MAX_ATTEMPTS", "3"))

        if not self.api_key:
            logger.warning(
                "LLM_API_KEY not set! V2 decision engine will not work. "
                "Set LLM_API_KEY in .env (use Liam farm g2a key)."
            )
        else:
            logger.info(
                f"LLM ready model={self.model} timeout={self.timeout}s "
                f"max_tokens={self.max_tokens} attempts={self.max_attempts}"
            )

    def analyze(
        self,
        market: BtcMarket,
        snapshot: BtcSnapshot,
        orderbook: dict = None,
    ) -> Optional[TradeSignal]:
        if not self.api_key:
            logger.error("No LLM API key configured. Cannot make decisions.")
            return None

        if snapshot.price_usd <= 0:
            logger.warning("No price data, skipping LLM analysis")
            return None

        # Hard pre-filters (save tokens + avoid garbage trades)
        t_left = getattr(market, "seconds_to_end", 0.0) or 0.0
        if 0 < t_left < 40:
            logger.info(f"Too late in window ({t_left:.0f}s left) — skip")
            return self._skip_signal(market, snapshot, ["Window almost expired"])

        user_prompt = build_analysis_prompt(
            market=market,
            snapshot=snapshot,
            orderbook=orderbook or {},
            news_headlines=snapshot.news_headlines,
        )

        llm_response = self._call_llm(user_prompt)
        if not llm_response:
            logger.error("LLM call failed, no trade this cycle")
            return None

        decision = self._parse_response(llm_response)
        if not decision:
            logger.error(f"Failed to parse LLM response: {llm_response[:200]}")
            return None

        direction = str(decision.get("direction", "skip")).lower().strip()
        confidence = min(max(float(decision.get("confidence", 0)), 0), 100) / 100.0
        reasoning = decision.get("reasoning") or []
        if isinstance(reasoning, str):
            reasoning = [reasoning]
        p_up = decision.get("p_up")
        edge_reported = decision.get("edge")

        if direction not in ("up", "down", "skip"):
            logger.warning(f"Invalid direction '{direction}', treating as skip")
            direction = "skip"

        # Code-side edge enforcement
        try:
            p_up_f = float(p_up) if p_up is not None else None
        except (TypeError, ValueError):
            p_up_f = None

        if direction in ("up", "down"):
            if p_up_f is not None:
                p_up_f = min(max(p_up_f, 0.01), 0.99)
                edge_up = p_up_f - market.yes_price
                edge_down = (1.0 - p_up_f) - market.no_price
                true_edge = edge_up if direction == "up" else edge_down
            else:
                try:
                    true_edge = float(edge_reported) if edge_reported is not None else 0.0
                except (TypeError, ValueError):
                    true_edge = 0.0

            entry = market.yes_price if direction == "up" else market.no_price

            # Reject bad prices / no edge
            if entry <= 0 or entry >= 1:
                direction = "skip"
                reasoning = list(reasoning) + [f"Invalid entry price {entry}"]
            elif entry > self.max_entry_price or entry < self.min_entry_price:
                direction = "skip"
                reasoning = list(reasoning) + [
                    f"Entry {entry:.2f} outside [{self.min_entry_price:.2f},{self.max_entry_price:.2f}]"
                ]
            elif true_edge < self.min_edge:
                direction = "skip"
                reasoning = list(reasoning) + [
                    f"Edge {true_edge:+.3f} < min {self.min_edge:.3f} — skip"
                ]
            else:
                reasoning = list(reasoning) + [f"Code edge check OK: {true_edge:+.3f}"]
                # Cap overconfident LLM
                confidence = min(confidence, 0.90)
                if true_edge < 0.10:
                    confidence = min(confidence, 0.78)

        token_id, expected_price = self._select_token(market, direction)
        stake_pct = self.config.get_position_size_pct(confidence) if direction != "skip" else 0.0

        # Prefer market price_to_beat; else approximate
        candle_open = getattr(market, "price_to_beat", 0.0) or 0.0
        if candle_open <= 0 and snapshot.raw_klines and len(snapshot.raw_klines) >= 5:
            try:
                candle_open = float(snapshot.raw_klines[-5]["open"])
            except Exception:
                candle_open = 0.0

        signal = TradeSignal(
            direction=direction,
            confidence=confidence,
            reasoning=list(reasoning)[:8],
            market=market,
            snapshot=snapshot,
            token_id=token_id,
            expected_price=expected_price,
            stake_pct=stake_pct,
            raw_llm_response=llm_response,
            candle_open_price=candle_open,
        )

        logger.info(signal.summary())
        return signal

    def _skip_signal(self, market, snapshot, reasons: list[str]) -> TradeSignal:
        return TradeSignal(
            direction="skip",
            confidence=0.0,
            reasoning=reasons,
            market=market,
            snapshot=snapshot,
            token_id="",
            expected_price=0.0,
            stake_pct=0.0,
            candle_open_price=getattr(market, "price_to_beat", 0.0) or 0.0,
        )

    def _call_llm(self, user_prompt: str) -> Optional[str]:
        start = time.time()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": self.model,
            "stream": False,  # some gateways (9router opencode-free) leak SSE otherwise
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            # DeepSeek reasoning mode burns the whole token budget on
            # chain-of-thought and never emits the final JSON. Disable it.
            "thinking": {"type": "disabled"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        }
        timeout = int(getattr(self, "timeout", 0) or os.environ.get("LLM_TIMEOUT_SECONDS", "45"))
        max_attempts = int(getattr(self, "max_attempts", 0) or os.environ.get("LLM_MAX_ATTEMPTS", "3"))
        # Progressive timeout: first try shorter, then longer (g2a sometimes slow)
        timeouts = [timeout, min(timeout + 15, 75), min(timeout + 30, 90)]
        last_err = None
        for attempt in range(1, max_attempts + 1):
            to = timeouts[min(attempt - 1, len(timeouts) - 1)]
            try:
                resp = self.session.post(
                    self.api_url,
                    headers=headers,
                    json=payload,
                    timeout=to,
                )
                if resp.status_code in (429, 502, 503, 504) and attempt < max_attempts:
                    logger.warning(
                        f"LLM {resp.status_code} attempt {attempt}/{max_attempts} — retry"
                    )
                    time.sleep(1.2 * attempt)
                    continue
                resp.raise_for_status()
                try:
                    data = resp.json()
                except requests.exceptions.JSONDecodeError:
                    # Gateways may return SSE frames even for non-stream
                    # requests (9router opencode-free does this). If the body
                    # is a stream, parse the LAST complete frame; otherwise
                    # strip a trailing "data: [DONE]" marker and re-try.
                    raw = resp.text
                    data = self._parse_ssE_or_json(raw)
                    if data is None:
                        raise
                msg = data["choices"][0]["message"]
                text = msg.get("content") or ""
                # Some models put JSON only in reasoning; fall back if content empty
                if not str(text).strip():
                    text = msg.get("reasoning_content") or msg.get("reasoning") or ""
                text = str(text)
                elapsed = time.time() - start
                logger.info(f"LLM responded in {elapsed:.1f}s ({len(text)} chars)")
                return text
            except requests.exceptions.Timeout as e:
                last_err = e
                logger.warning(f"LLM timeout attempt {attempt}/{max_attempts} (to={to}s)")
                if attempt < max_attempts:
                    time.sleep(1.0 * attempt)
                    # Shrink tokens on retry to finish faster
                    payload["max_tokens"] = max(180, int(payload.get("max_tokens", 350) * 0.7))
                    continue
            except requests.exceptions.HTTPError as e:
                last_err = e
                body = ""
                if hasattr(e, "response") and e.response is not None:
                    body = e.response.text[:300]
                logger.error(f"LLM API error: {e} — {body}")
                if (
                    attempt < max_attempts
                    and e.response is not None
                    and e.response.status_code in (429, 502, 503, 504)
                ):
                    time.sleep(1.2 * attempt)
                    continue
                return None
            except (KeyError, IndexError) as e:
                logger.error(f"Unexpected LLM response format: {e}")
                return None
            except Exception as e:
                last_err = e
                logger.error(f"LLM call failed: {e}")
                if attempt < max_attempts:
                    time.sleep(0.8 * attempt)
                    continue
                return None
        logger.error(f"LLM failed after retries: {last_err}")
        return None

    def _parse_ssE_or_json(self, raw: str) -> Optional[dict]:
        """Parse a body that may be raw JSON or leaked SSE frames.

        Some gateways (9router opencode-free) return SSE frames even for
        non-stream requests. Strategy:
        1. Try whole body as JSON.
        2. If "data: [DONE]" is present, take everything before it.
        3. If multiple "data: {...}" frames exist, parse the LAST frame.
        4. Last resort: strip to the final balanced "}" and re-try.
        """
        raw = raw.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        done = raw.find("data: [DONE]")
        if done > 0:
            raw = raw[:done].strip()

        # Last SSE frame: "data: {...}"
        idx = raw.rfind("data: {")
        if idx >= 0:
            s = raw.find("{", idx)
            try:
                return json.loads(raw[s:])
            except json.JSONDecodeError:
                pass

        end = raw.rfind("}")
        if end > 0:
            try:
                return json.loads(raw[: end + 1])
            except json.JSONDecodeError:
                pass
        return None

    def _parse_response(self, response: str) -> Optional[dict]:
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Scan for every brace-balanced JSON object; return the last valid one.
        # Some cheap models wrap the JSON in prose or repeat examples before the
        # final answer, so naive first-{/last-} extraction is not enough.
        candidates = []
        depth = 0
        in_str = False
        esc = False
        start = -1
        for i, ch in enumerate(text):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    candidates.append(text[start : i + 1])
                    start = -1
        for chunk in reversed(candidates):
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                # trailing commas / soft fix
                soft = re.sub(r",\s*}", "}", chunk)
                soft = re.sub(r",\s*]", "]", soft)
                try:
                    return json.loads(soft)
                except json.JSONDecodeError:
                    continue
        logger.error("Could not parse JSON from LLM response")
        return None

    def _select_token(self, market: BtcMarket, direction: str) -> tuple[str, float]:
        if direction == "skip" or not market.tokens:
            return ("", 0.0)

        up_id = down_id = ""
        for t in market.tokens:
            oc = str(t.get("outcome", "")).lower()
            if oc in ("up", "yes"):
                up_id = str(t.get("token_id", ""))
            elif oc in ("down", "no"):
                down_id = str(t.get("token_id", ""))

        if direction == "up":
            token_id = up_id or (str(market.tokens[0].get("token_id", "")) if market.tokens else "")
            price = market.yes_price
        else:
            token_id = down_id or (
                str(market.tokens[1].get("token_id", "")) if len(market.tokens) > 1 else ""
            )
            price = market.no_price
        return (token_id, price)
