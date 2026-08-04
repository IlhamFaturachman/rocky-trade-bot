"""
Unified CLOB fill model for paper / live_dry / live / shadow.

Goal: paper behavior ≈ live market-buy (FOK-style) against the real book:
- walk asks from best (lowest) price
- VWAP entry for the USD stake
- reject if book too thin, slip too high, or VWAP breaches max entry
- optional fee bump on effective entry (taker-style buffer)

Live path reuses the same pre-trade checks; only the final "post order"
step differs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _f(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


@dataclass
class FillConfig:
    """Execution realism knobs (shared paper + live)."""

    fee_bps: float = 150.0
    slip_bps: float = 50.0  # max adverse vs best ask (reject if VWAP worse)
    apply_fee_to_entry: bool = True  # bump VWAP by fee_bps for journal entry
    min_fill_pct: float = 1.0  # require full fill (FOK-like); <1 allows partial
    max_entry: float = 0.70
    min_entry: float = 0.01
    min_ask_size: float = 0.0  # shares at best ask (0 = no min)
    book_stale_reject: bool = True
    # Live safety
    live_enabled: bool = False  # must be true to post real orders
    live_dry_run: bool = True  # if live mode but not enabled → dry journal
    order_type: str = "FOK"  # FOK market-style

    @classmethod
    def from_env(cls) -> "FillConfig":
        return cls(
            fee_bps=_env_float("ROCKY_FEE_BPS", 150),
            slip_bps=_env_float("ROCKY_SLIP_BPS", 50),
            apply_fee_to_entry=_env_bool("ROCKY_APPLY_FEE_TO_ENTRY", True),
            min_fill_pct=min(1.0, max(0.01, _env_float("ROCKY_MIN_FILL_PCT", 1.0))),
            max_entry=_env_float("ROCKY_MAX_ENTRY", 0.70),
            min_entry=_env_float("ROCKY_MIN_ENTRY", 0.01),
            min_ask_size=_env_float("ROCKY_MIN_ASK_SIZE", 0.0),
            book_stale_reject=_env_bool("ROCKY_BOOK_STALE_REJECT", True),
            live_enabled=_env_bool("ROCKY_LIVE_ENABLED", False),
            live_dry_run=_env_bool("ROCKY_LIVE_DRY_RUN", True),
            order_type=(os.environ.get("ROCKY_ORDER_TYPE") or "FOK").upper(),
        )


@dataclass
class FillResult:
    ok: bool
    entry_price: float = 0.0  # effective journal entry (VWAP ± fee)
    vwap: float = 0.0  # raw book VWAP before fee bump
    best_ask: float = 0.0
    best_ask_size: float = 0.0
    filled_usd: float = 0.0
    filled_shares: float = 0.0
    unfilled_usd: float = 0.0
    slip_vs_best: float = 0.0  # vwap - best_ask
    fee_bps_applied: float = 0.0
    levels_used: int = 0
    reason: str = ""
    levels: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "entry_price": self.entry_price,
            "vwap": self.vwap,
            "best_ask": self.best_ask,
            "best_ask_size": self.best_ask_size,
            "filled_usd": self.filled_usd,
            "filled_shares": self.filled_shares,
            "unfilled_usd": self.unfilled_usd,
            "slip_vs_best": self.slip_vs_best,
            "fee_bps_applied": self.fee_bps_applied,
            "levels_used": self.levels_used,
            "reason": self.reason,
        }


def _parse_levels(levels: list) -> list[tuple[float, float]]:
    """Return (price, size) for asks/bids; size in shares."""
    out: list[tuple[float, float]] = []
    for lv in levels or []:
        if isinstance(lv, dict):
            p = _f(lv.get("price"))
            s = _f(lv.get("size") or lv.get("amount") or lv.get("quantity"))
        elif isinstance(lv, (list, tuple)) and lv:
            p = _f(lv[0])
            s = _f(lv[1]) if len(lv) > 1 else 0.0
        else:
            continue
        if 0 < p < 1 and s > 0:
            out.append((p, s))
    return out


def walk_buy_asks(
    orderbook: Optional[dict],
    stake_usd: float,
    cfg: Optional[FillConfig] = None,
    *,
    fallback_ask: float = 0.0,
) -> FillResult:
    """
    Simulate a market BUY of `stake_usd` notional against asks.

    Polymarket CLOB often returns asks unsorted; we sort ascending (best first).
    """
    cfg = cfg or FillConfig.from_env()
    stake = float(stake_usd or 0)
    if stake <= 0:
        return FillResult(ok=False, reason="stake<=0")

    asks = _parse_levels((orderbook or {}).get("asks") or [])
    asks.sort(key=lambda t: t[0])  # best ask = lowest price

    if not asks:
        # Fallback: single-level at signal ask (still apply fee/slip caps)
        if fallback_ask and 0 < fallback_ask < 1:
            asks = [(float(fallback_ask), 1e12)]  # infinite size at quoted ask
        else:
            return FillResult(ok=False, reason="empty orderbook asks")

    best_ask, best_sz = asks[0]
    if cfg.min_ask_size > 0 and best_sz < cfg.min_ask_size:
        return FillResult(
            ok=False,
            best_ask=best_ask,
            best_ask_size=best_sz,
            reason=f"best ask size {best_sz:.2f} < min {cfg.min_ask_size:.2f}",
        )

    remaining = stake
    cost = 0.0
    shares = 0.0
    used: list[dict] = []
    for price, size in asks:
        if remaining <= 1e-9:
            break
        level_usd = price * size
        take_usd = min(remaining, level_usd)
        take_shares = take_usd / price if price > 0 else 0.0
        cost += take_usd
        shares += take_shares
        remaining -= take_usd
        used.append({"price": price, "size_shares": take_shares, "usd": take_usd})

    filled_usd = stake - remaining
    fill_pct = filled_usd / stake if stake > 0 else 0.0
    if fill_pct + 1e-9 < cfg.min_fill_pct:
        return FillResult(
            ok=False,
            best_ask=best_ask,
            best_ask_size=best_sz,
            filled_usd=filled_usd,
            filled_shares=shares,
            unfilled_usd=remaining,
            levels_used=len(used),
            levels=used,
            reason=f"partial fill {fill_pct:.1%} < min {cfg.min_fill_pct:.0%} (book thin)",
        )

    vwap = cost / shares if shares > 0 else 0.0
    if vwap <= 0:
        return FillResult(ok=False, reason="vwap<=0", best_ask=best_ask)

    slip = vwap - best_ask
    max_slip = (cfg.slip_bps / 10_000.0) if cfg.slip_bps > 0 else 1.0
    if slip > max_slip + 1e-9:
        return FillResult(
            ok=False,
            vwap=vwap,
            best_ask=best_ask,
            best_ask_size=best_sz,
            filled_usd=filled_usd,
            filled_shares=shares,
            slip_vs_best=slip,
            levels_used=len(used),
            levels=used,
            reason=f"slip {slip:.4f} > max {max_slip:.4f} (bps={cfg.slip_bps})",
        )

    # Polymarket dynamic taker fee: fee = rate * price * (1-price)
    # Not flat — peaks at mid-price, drops at extremes.
    fee_frac = 0.0
    if cfg.apply_fee_to_entry and cfg.fee_bps > 0:
        rate = cfg.fee_bps / 10_000.0
        fee_frac = rate * vwap * (1 - vwap)
        entry = vwap * (1.0 + fee_frac)
    else:
        entry = vwap
    entry = min(0.99, max(0.01, entry))
    fee_bps_applied = fee_frac * 10_000

    if entry > cfg.max_entry + 1e-9:
        return FillResult(
            ok=False,
            vwap=vwap,
            entry_price=entry,
            best_ask=best_ask,
            best_ask_size=best_sz,
            filled_usd=filled_usd,
            filled_shares=shares,
            slip_vs_best=slip,
            fee_bps_applied=fee_bps_applied,
            levels_used=len(used),
            levels=used,
            reason=f"effective entry {entry:.4f} > max_entry {cfg.max_entry:.2f}",
        )
    if entry < cfg.min_entry - 1e-9:
        return FillResult(
            ok=False,
            vwap=vwap,
            entry_price=entry,
            best_ask=best_ask,
            reason=f"effective entry {entry:.4f} < min_entry {cfg.min_entry:.2f}",
        )

    return FillResult(
        ok=True,
        entry_price=entry,
        vwap=vwap,
        best_ask=best_ask,
        best_ask_size=best_sz,
        filled_usd=filled_usd,
        filled_shares=shares,
        unfilled_usd=max(0.0, remaining),
        slip_vs_best=slip,
        fee_bps_applied=fee_bps_applied,
        levels_used=len(used),
        levels=used,
        reason="ok",
    )


def quote_ask(orderbook: Optional[dict], fallback: float = 0.0) -> float:
    asks = _parse_levels((orderbook or {}).get("asks") or [])
    if not asks:
        return float(fallback or 0.0)
    return min(p for p, _ in asks)
