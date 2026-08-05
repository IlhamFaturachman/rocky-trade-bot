"""
Rocky Edge Pack v1 — hard gates that decide whether a trade is allowed.

Philosophy:
- Default SKIP
- Timing window (not too early, not last-second flip zone)
- Regime filter (chop/flat)
- EV vs ask (no mid fantasy, no chase expensive favorites)
- Flip guard (adverse 1m move vs intended direction)
"""

from __future__ import annotations

import os
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("rocky.edge")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class EdgeConfig:
    # Timing (seconds)
    min_t_elapsed: float = 50.0      # skip if window just opened
    min_t_left: float = 30.0         # skip last-second flip zone
    max_t_left: float = 180.0        # skip if too early in long horizon
    sweet_min_t_left: float = 60.0
    sweet_max_t_left: float = 150.0

    # Regime
    min_ptb_bps: float = 3.0         # |price-ptb|/ptb * 1e4
    max_chop_range_bps: float = 4.0  # recent 1m range avg too small
    require_vol_not_low: bool = False  # if True, skip pure low-vol

    # Pricing / EV
    min_edge: float = 0.05           # p_model - ask
    max_entry: float = 0.70          # never chase expensive favorites
    min_entry: float = 0.28
    fee_bps: float = 700.0           # Polymarket crypto taker fee = 7%
    slip_bps: float = 50.0           # extra slip buffer

    # Flip guard
    flip_1m_bps: float = 2.0         # adverse 1m move kills entry

    # Late sniper (optional secondary mode)
    sniper_enabled: bool = True
    sniper_min_t_left: float = 30.0
    sniper_max_t_left: float = 60.0
    sniper_min_ptb_bps: float = 5.0
    sniper_min_edge: float = 0.04

    @classmethod
    def from_env(cls) -> "EdgeConfig":
        return cls(
            min_t_elapsed=_env_float("ROCKY_MIN_T_ELAPSED", 50),
            min_t_left=_env_float("ROCKY_MIN_T_LEFT", 30),
            max_t_left=_env_float("ROCKY_MAX_T_LEFT", 180),
            sweet_min_t_left=_env_float("ROCKY_SWEET_MIN_T_LEFT", 60),
            sweet_max_t_left=_env_float("ROCKY_SWEET_MAX_T_LEFT", 150),
            min_ptb_bps=_env_float("ROCKY_MIN_PTB_BPS", 3),
            max_chop_range_bps=_env_float("ROCKY_MAX_CHOP_RANGE_BPS", 4),
            require_vol_not_low=os.environ.get("ROCKY_REQUIRE_VOL_NOT_LOW", "").lower()
            in ("1", "true", "yes"),
            min_edge=_env_float("ROCKY_MIN_EDGE", 0.05),
            max_entry=_env_float("ROCKY_MAX_ENTRY", 0.70),
            min_entry=_env_float("ROCKY_MIN_ENTRY", 0.28),
            fee_bps=_env_float("ROCKY_FEE_BPS", 700),
            slip_bps=_env_float("ROCKY_SLIP_BPS", 50),
            flip_1m_bps=_env_float("ROCKY_FLIP_1M_BPS", 2),
            sniper_enabled=os.environ.get("ROCKY_SNIPER_ENABLED", "true").lower()
            in ("1", "true", "yes"),
            sniper_min_t_left=_env_float("ROCKY_SNIPER_MIN_T_LEFT", 30),
            sniper_max_t_left=_env_float("ROCKY_SNIPER_MAX_T_LEFT", 60),
            sniper_min_ptb_bps=_env_float("ROCKY_SNIPER_MIN_PTB_BPS", 5),
            sniper_min_edge=_env_float("ROCKY_SNIPER_MIN_EDGE", 0.04),
        )


@dataclass
class EdgeDecision:
    allow: bool
    mode: str = "none"  # confirmation | sniper | none
    reason: str = ""
    skip_code: str = ""  # early|late|chop|no_edge|chase|flip|timing|regime
    t_elapsed: float = 0.0
    t_left: float = 0.0
    ptb_bps: float = 0.0
    ask_price: float = 0.0
    edge: float = 0.0
    p_model: float = 0.5
    fee_buffer: float = 0.0
    details: list[str] = field(default_factory=list)


def _window_seconds(market) -> float:
    """Infer window length from series or end-start if available."""
    slug = getattr(market, "series_slug", "") or ""
    if "5m" in slug or "5m" in (getattr(market, "market_slug", "") or ""):
        return 300.0
    if "15m" in slug:
        return 900.0
    if "hour" in slug:
        return 3600.0
    # fallback from seconds_to_end ceiling
    t_left = float(getattr(market, "seconds_to_end", 0) or 0)
    if t_left <= 300:
        return 300.0
    if t_left <= 900:
        return 900.0
    return max(300.0, t_left)


def _ptb_bps(price: float, ptb: float) -> float:
    if ptb <= 0 or price <= 0:
        return 0.0
    return abs(price - ptb) / ptb * 10_000.0


def _signed_ptb_bps(price: float, ptb: float) -> float:
    if ptb <= 0 or price <= 0:
        return 0.0
    return (price - ptb) / ptb * 10_000.0


def _recent_range_bps(snapshot) -> float:
    klines = getattr(snapshot, "raw_klines", None) or []
    if len(klines) < 5:
        return 999.0
    # Exclude the last (incomplete) candle — its range is still forming
    # and can falsely trigger chop when only 1-2 ticks have arrived
    recent = klines[-6:-1] if len(klines) >= 6 else klines[:-1]
    ranges = []
    for k in recent:
        o = float(k.get("open") or 0)
        h = float(k.get("high") or 0)
        l = float(k.get("low") or 0)
        if o > 0:
            ranges.append((h - l) / o * 10_000.0)
    if not ranges:
        return 999.0
    return sum(ranges) / len(ranges)


def _ask_for_direction(market, direction: str, orderbook: Optional[dict] = None) -> float:
    """Best available ask for the token we would buy."""
    # Prefer CLOB asks if present
    if orderbook:
        asks = orderbook.get("asks") or []
        prices = []
        for lv in asks:
            if isinstance(lv, dict):
                try:
                    prices.append(float(lv.get("price")))
                except (TypeError, ValueError):
                    pass
            elif isinstance(lv, (list, tuple)) and lv:
                try:
                    prices.append(float(lv[0]))
                except (TypeError, ValueError):
                    pass
        prices = [p for p in prices if 0 < p < 1]
        if prices:
            return min(prices)

    if direction == "up":
        return float(getattr(market, "yes_price", 0.5) or 0.5)
    return float(getattr(market, "no_price", 0.5) or 0.5)


def _p_model_from_signal(direction: str, confidence: float, snapshot, market) -> float:
    """
    Map signal confidence + tape to p(up), blended with tape_fair_p_up guardrail.
    LLM confidence is uncalibrated — tape_fair_p_up (grounded in PTB displacement)
    acts as a sanity check. If they diverge >15%, blend toward tape.
    """
    conf = max(0.5, min(0.95, float(confidence or 0.5)))
    if direction == "up":
        llm_p = conf
    elif direction == "down":
        llm_p = 1.0 - conf
    else:
        ptb = float(getattr(market, "price_to_beat", 0) or 0)
        price = float(getattr(snapshot, "price_usd", 0) or 0)
        if ptb > 0 and price > 0:
            return 0.55 if price >= ptb else 0.45
        return 0.5
    # Tape-based p_model guardrail (from features.tape_fair_p_up)
    tape_p = _tape_fair_p_up_safe(snapshot, market)
    if tape_p is not None:
        divergence = abs(llm_p - tape_p)
        if divergence > 0.15:
            # Blend 60% LLM + 40% tape when divergent (tape grounded in settlement criterion)
            llm_p = 0.6 * llm_p + 0.4 * tape_p
    return max(0.01, min(0.99, llm_p))


def _tape_fair_p_up_safe(snapshot, market) -> Optional[float]:
    """Compute tape_fair_p_up safely (may not have features import)."""
    try:
        from src.features import tape_fair_p_up
        return tape_fair_p_up(snapshot, market)
    except Exception:
        return None


class EdgeGate:
    def __init__(self, cfg: Optional[EdgeConfig] = None):
        self.cfg = cfg or EdgeConfig.from_env()
        self.skip_counts: dict[str, int] = {}

    def _bump(self, code: str):
        self.skip_counts[code] = self.skip_counts.get(code, 0) + 1

    def evaluate(
        self,
        market,
        snapshot,
        direction: str,
        confidence: float,
        orderbook: Optional[dict] = None,
    ) -> EdgeDecision:
        cfg = self.cfg
        t_left = float(getattr(market, "seconds_to_end", 0) or 0)
        win = _window_seconds(market)
        t_elapsed = max(0.0, win - t_left) if t_left > 0 else 0.0
        price = float(getattr(snapshot, "price_usd", 0) or 0)
        ptb = float(getattr(market, "price_to_beat", 0) or 0)
        ptb_bps = _ptb_bps(price, ptb)
        signed_bps = _signed_ptb_bps(price, ptb)
        range_bps = _recent_range_bps(snapshot)
        # Polymarket dynamic taker fee: fee = rate * price * (1-price)
        # Peaks ~0.375% at price 0.50, drops to ~0.027% at price 0.10.
        # Using best ask as proxy for entry price. rate = fee_bps/10000.
        ask_proxy = float(getattr(market, "yes_price", 0) or 0.5)
        rate = cfg.fee_bps / 10_000.0
        dynamic_fee = rate * ask_proxy * (1 - ask_proxy)
        # Slippage: keep slip_bps as base, but add depth-aware bump if orderbook is thin
        # (computed later from actual book depth; here use flat as conservative baseline)
        slip_buffer = cfg.slip_bps / 10_000.0
        fee_buffer = dynamic_fee + slip_buffer

        details = [
            f"t_elapsed={t_elapsed:.0f}s t_left={t_left:.0f}s win={win:.0f}s",
            f"ptb_bps={ptb_bps:.2f} signed={signed_bps:+.2f} range5m_bps≈{range_bps:.2f}",
            f"vol={getattr(snapshot, 'volatility', '?')} mom={getattr(snapshot, 'momentum', '?')}",
        ]

        # ── Timing ──────────────────────────────────────────────
        if t_left <= 0:
            self._bump("expired")
            return EdgeDecision(False, reason="market expired/unknown t_left", skip_code="timing",
                                t_elapsed=t_elapsed, t_left=t_left, ptb_bps=ptb_bps, details=details)

        if t_elapsed < cfg.min_t_elapsed:
            self._bump("early")
            return EdgeDecision(
                False,
                reason=f"too early in window (t+{t_elapsed:.0f}s < {cfg.min_t_elapsed:.0f}s)",
                skip_code="early",
                t_elapsed=t_elapsed, t_left=t_left, ptb_bps=ptb_bps, details=details,
            )

        if t_left < cfg.min_t_left:
            self._bump("late")
            return EdgeDecision(
                False,
                reason=f"last-second flip zone (t_left={t_left:.0f}s < {cfg.min_t_left:.0f}s)",
                skip_code="late",
                t_elapsed=t_elapsed, t_left=t_left, ptb_bps=ptb_bps, details=details,
            )

        if t_left > cfg.max_t_left:
            self._bump("too_far")
            return EdgeDecision(
                False,
                reason=f"window too far out (t_left={t_left:.0f}s > {cfg.max_t_left:.0f}s)",
                skip_code="timing",
                t_elapsed=t_elapsed, t_left=t_left, ptb_bps=ptb_bps, details=details,
            )

        # Mode selection
        mode = "confirmation"
        if cfg.sweet_min_t_left <= t_left <= cfg.sweet_max_t_left:
            mode = "confirmation"
        elif cfg.sniper_enabled and cfg.sniper_min_t_left <= t_left <= cfg.sniper_max_t_left:
            mode = "sniper"
        else:
            # still allow confirmation outside sweet if within max/min
            mode = "confirmation"

        # ── Regime / chop ───────────────────────────────────────
        vol = str(getattr(snapshot, "volatility", "normal") or "normal")
        if cfg.require_vol_not_low and vol == "low" and ptb_bps < cfg.min_ptb_bps * 2:
            self._bump("chop")
            return EdgeDecision(
                False, mode=mode,
                reason="low volatility chop — no trade",
                skip_code="chop",
                t_elapsed=t_elapsed, t_left=t_left, ptb_bps=ptb_bps, details=details,
            )

        if range_bps < cfg.max_chop_range_bps and ptb_bps < cfg.min_ptb_bps:
            self._bump("chop")
            return EdgeDecision(
                False, mode=mode,
                reason=f"choppy/flat (range≈{range_bps:.1f}bps, ptb={ptb_bps:.1f}bps)",
                skip_code="chop",
                t_elapsed=t_elapsed, t_left=t_left, ptb_bps=ptb_bps, details=details,
            )

        min_ptb = cfg.sniper_min_ptb_bps if mode == "sniper" else cfg.min_ptb_bps
        if ptb > 0 and ptb_bps < min_ptb:
            self._bump("chop")
            return EdgeDecision(
                False, mode=mode,
                reason=f"too close to price-to-beat ({ptb_bps:.1f}bps < {min_ptb:.1f})",
                skip_code="chop",
                t_elapsed=t_elapsed, t_left=t_left, ptb_bps=ptb_bps, details=details,
            )

        # Direction required
        if direction not in ("up", "down"):
            self._bump("no_dir")
            return EdgeDecision(
                False, mode=mode, reason="no tradeable direction", skip_code="no_edge",
                t_elapsed=t_elapsed, t_left=t_left, ptb_bps=ptb_bps, details=details,
            )

        # Sniper: must already be on correct side of PTB with buffer
        if mode == "sniper" and ptb > 0:
            if direction == "up" and signed_bps < cfg.sniper_min_ptb_bps:
                self._bump("sniper_side")
                return EdgeDecision(
                    False, mode=mode,
                    reason=f"sniper UP needs price above PTB by {cfg.sniper_min_ptb_bps}bps (have {signed_bps:+.1f})",
                    skip_code="regime",
                    t_elapsed=t_elapsed, t_left=t_left, ptb_bps=ptb_bps, details=details,
                )
            if direction == "down" and signed_bps > -cfg.sniper_min_ptb_bps:
                self._bump("sniper_side")
                return EdgeDecision(
                    False, mode=mode,
                    reason=f"sniper DOWN needs price below PTB by {cfg.sniper_min_ptb_bps}bps (have {signed_bps:+.1f})",
                    skip_code="regime",
                    t_elapsed=t_elapsed, t_left=t_left, ptb_bps=ptb_bps, details=details,
                )

        # Confirmation: direction should agree with PTB side when displacement exists
        if mode == "confirmation" and ptb > 0 and ptb_bps >= cfg.min_ptb_bps:
            if direction == "up" and signed_bps < 0:
                self._bump("against_ptb")
                return EdgeDecision(
                    False, mode=mode,
                    reason="UP signal but price still below PTB — wait/confirm",
                    skip_code="regime",
                    t_elapsed=t_elapsed, t_left=t_left, ptb_bps=ptb_bps, details=details,
                )
            if direction == "down" and signed_bps > 0:
                self._bump("against_ptb")
                return EdgeDecision(
                    False, mode=mode,
                    reason="DOWN signal but price still above PTB — wait/confirm",
                    skip_code="regime",
                    t_elapsed=t_elapsed, t_left=t_left, ptb_bps=ptb_bps, details=details,
                )

        # ── Flip guard (1m adverse) ─────────────────────────────
        ch1 = float(getattr(snapshot, "price_change_1m", 0) or 0)  # percent
        ch1_bps = ch1 * 100.0
        if direction == "up" and ch1_bps <= -cfg.flip_1m_bps:
            self._bump("flip")
            return EdgeDecision(
                False, mode=mode,
                reason=f"flip guard: 1m adverse {ch1_bps:.1f}bps vs UP",
                skip_code="flip",
                t_elapsed=t_elapsed, t_left=t_left, ptb_bps=ptb_bps, details=details,
            )
        if direction == "down" and ch1_bps >= cfg.flip_1m_bps:
            self._bump("flip")
            return EdgeDecision(
                False, mode=mode,
                reason=f"flip guard: 1m adverse {ch1_bps:+.1f}bps vs DOWN",
                skip_code="flip",
                t_elapsed=t_elapsed, t_left=t_left, ptb_bps=ptb_bps, details=details,
            )

        # ── Price / EV ──────────────────────────────────────────
        ask = _ask_for_direction(market, direction, orderbook)
        if ask > cfg.max_entry or ask < cfg.min_entry:
            self._bump("chase")
            return EdgeDecision(
                False, mode=mode,
                reason=f"entry ask {ask:.3f} outside [{cfg.min_entry:.2f},{cfg.max_entry:.2f}]",
                skip_code="chase",
                t_elapsed=t_elapsed, t_left=t_left, ptb_bps=ptb_bps,
                ask_price=ask, details=details,
            )

        p_model = _p_model_from_signal(direction, confidence, snapshot, market)
        # Edge for the side we buy: if UP, edge = p_up - ask_up; if DOWN, edge = p_down - ask_down
        if direction == "up":
            edge = p_model - ask - fee_buffer
        else:
            edge = (1.0 - p_model) - ask - fee_buffer

        min_edge = cfg.sniper_min_edge if mode == "sniper" else cfg.min_edge
        if edge < min_edge:
            self._bump("no_edge")
            return EdgeDecision(
                False, mode=mode,
                reason=f"no EV: edge={edge:.3f} < min {min_edge:.3f} (p={p_model:.2f} ask={ask:.3f} fee={fee_buffer:.3f})",
                skip_code="no_edge",
                t_elapsed=t_elapsed, t_left=t_left, ptb_bps=ptb_bps,
                ask_price=ask, edge=edge, p_model=p_model, fee_buffer=fee_buffer, details=details,
            )

        details.append(f"ALLOW mode={mode} edge={edge:.3f} ask={ask:.3f} p={p_model:.2f}")
        logger.info("EDGE ALLOW %s | %s", mode, details[-1])
        return EdgeDecision(
            True,
            mode=mode,
            reason=f"edge ok ({mode})",
            skip_code="",
            t_elapsed=t_elapsed,
            t_left=t_left,
            ptb_bps=ptb_bps,
            ask_price=ask,
            edge=edge,
            p_model=p_model,
            fee_buffer=fee_buffer,
            details=details,
        )
