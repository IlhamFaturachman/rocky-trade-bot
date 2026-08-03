"""
Rocky research features — maximal data collection for 5m BTC markets.

Inspired by common profitable-bot themes (not guarantees):
- Tape vs book mispricing (fair value vs ask)
- Window phase (early / sweet / late)
- Displacement from price-to-beat (bps)
- Short-horizon momentum / range / volume
- Book microstructure (bid/ask/spread/mid)
- Session tags (UTC hour)
"""

from __future__ import annotations

import time
from typing import Any, Optional


def _f(x, default=0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def best_bid_ask(orderbook: Optional[dict]) -> dict:
    bids = (orderbook or {}).get("bids") or []
    asks = (orderbook or {}).get("asks") or []

    def prices(levels, side: str):
        out = []
        for lv in levels:
            if isinstance(lv, dict):
                p = _f(lv.get("price"))
                s = _f(lv.get("size"))
            elif isinstance(lv, (list, tuple)) and lv:
                p = _f(lv[0])
                s = _f(lv[1]) if len(lv) > 1 else 0.0
            else:
                continue
            if 0 < p < 1:
                out.append((p, s))
        if not out:
            return None, 0.0
        if side == "bid":
            p, s = max(out, key=lambda t: t[0])
        else:
            p, s = min(out, key=lambda t: t[0])
        return p, s

    bid, bid_sz = prices(bids, "bid")
    ask, ask_sz = prices(asks, "ask")
    mid = None
    spread = None
    if bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
        spread = max(0.0, ask - bid)
    return {
        "best_bid": bid,
        "best_ask": ask,
        "best_bid_size": bid_sz,
        "best_ask_size": ask_sz,
        "mid": mid,
        "spread": spread,
    }


def window_phase(t_elapsed: float, t_left: float, win: float = 300.0) -> str:
    if t_left <= 0:
        return "expired"
    if t_elapsed < 50:
        return "early"
    if t_left <= 30:
        return "too_late"
    if 30 < t_left <= 60:
        return "late_sniper"
    if 60 <= t_left <= 150:
        return "sweet"
    if t_left > 180:
        return "too_far"
    return "mid"


def tape_fair_p_up(snapshot, market) -> float:
    """
    Crude fair P(up) from tape only (no LLM).
    Uses PTB side + short momentum. Clamped to [0.05, 0.95].
    """
    price = _f(getattr(snapshot, "price_usd", 0))
    ptb = _f(getattr(market, "price_to_beat", 0))
    ch1 = _f(getattr(snapshot, "price_change_1m", 0))
    ch5 = _f(getattr(snapshot, "price_change_5m", 0))
    p = 0.5
    if ptb > 0 and price > 0:
        bps = (price - ptb) / ptb * 10_000.0
        # ~1% move in p per 2 bps displacement, capped
        p += max(-0.25, min(0.25, bps / 20.0 * 0.10))
    p += max(-0.12, min(0.12, ch1 * 0.8))
    p += max(-0.10, min(0.10, ch5 * 0.4))
    mom = str(getattr(snapshot, "momentum", "neutral") or "neutral")
    if mom == "bullish":
        p += 0.03
    elif mom == "bearish":
        p -= 0.03
    return max(0.05, min(0.95, p))


def build_cycle_features(
    *,
    market,
    snapshot,
    orderbook: Optional[dict] = None,
    signal=None,
    edge=None,
    engine: str = "v2",
    cycle: int = 0,
    extra: Optional[dict] = None,
) -> dict[str, Any]:
    now = time.time()
    t_left = _f(getattr(market, "seconds_to_end", 0))
    # 5m default
    win = 300.0
    slug = str(getattr(market, "series_slug", "") or getattr(market, "market_slug", "") or "")
    if "15m" in slug:
        win = 900.0
    elif "hour" in slug:
        win = 3600.0
    t_elapsed = max(0.0, win - t_left) if t_left > 0 else 0.0
    price = _f(getattr(snapshot, "price_usd", 0))
    ptb = _f(getattr(market, "price_to_beat", 0))
    ptb_bps = abs(price - ptb) / ptb * 10_000.0 if ptb > 0 and price > 0 else 0.0
    signed_ptb_bps = (price - ptb) / ptb * 10_000.0 if ptb > 0 and price > 0 else 0.0

    book = best_bid_ask(orderbook)
    yes = _f(getattr(market, "yes_price", 0.5), 0.5)
    no = _f(getattr(market, "no_price", 0.5), 0.5)
    fair_up = tape_fair_p_up(snapshot, market)
    # edge vs mid/ask if available
    ask_up = book["best_ask"] if book["best_ask"] is not None else yes
    # for down token approximate ask as no mid if no separate book
    ask_down = no

    feat = {
        "ts": now,
        "utc_hour": time.gmtime(now).tm_hour,
        "utc_dow": time.gmtime(now).tm_wday,
        "cycle": cycle,
        "engine": engine,
        "market": getattr(market, "question", ""),
        "market_slug": getattr(market, "market_slug", ""),
        "series_slug": getattr(market, "series_slug", ""),
        "condition_id": getattr(market, "condition_id", ""),
        "end_date": getattr(market, "end_date", ""),
        "t_left": t_left,
        "t_elapsed": t_elapsed,
        "window_sec": win,
        "phase": window_phase(t_elapsed, t_left, win),
        "btc": price,
        "ptb": ptb,
        "ptb_bps": ptb_bps,
        "signed_ptb_bps": signed_ptb_bps,
        "ch1m": _f(getattr(snapshot, "price_change_1m", 0)),
        "ch5m": _f(getattr(snapshot, "price_change_5m", 0)),
        "ch15m": _f(getattr(snapshot, "price_change_15m", 0)),
        "ch1h": _f(getattr(snapshot, "price_change_1h", 0)),
        "momentum": getattr(snapshot, "momentum", ""),
        "volatility": getattr(snapshot, "volatility", ""),
        "trend": getattr(snapshot, "trend_direction", ""),
        "vol24h": _f(getattr(snapshot, "volume_24h", 0)),
        "high24h": _f(getattr(snapshot, "high_24h", 0)),
        "low24h": _f(getattr(snapshot, "low_24h", 0)),
        "yes": yes,
        "no": no,
        "liquidity": _f(getattr(market, "liquidity", 0)),
        "volume": _f(getattr(market, "volume", 0)),
        "book_bid": book["best_bid"],
        "book_ask": book["best_ask"],
        "book_mid": book["mid"],
        "book_spread": book["spread"],
        "book_bid_sz": book["best_bid_size"],
        "book_ask_sz": book["best_ask_size"],
        "fair_p_up_tape": fair_up,
        "edge_up_vs_ask": fair_up - _f(ask_up, yes) if ask_up else None,
        "edge_down_vs_ask": (1.0 - fair_up) - _f(ask_down, no) if ask_down else None,
        "news_sentiment": getattr(snapshot, "news_sentiment", ""),
        "news_n": len(getattr(snapshot, "news_headlines", []) or []),
    }

    if signal is not None:
        feat.update({
            "signal_dir": getattr(signal, "direction", ""),
            "signal_conf": _f(getattr(signal, "confidence", 0)),
            "signal_edge": _f(getattr(signal, "edge", 0)),
            "signal_ask": _f(getattr(signal, "ask_price", 0) or getattr(signal, "expected_price", 0)),
            "signal_p_model": _f(getattr(signal, "p_model", 0)),
            "signal_mode": getattr(signal, "edge_mode", ""),
        })
    if edge is not None:
        feat.update({
            "edge_allow": bool(getattr(edge, "allow", False)),
            "edge_reason": getattr(edge, "reason", ""),
            "edge_skip_code": getattr(edge, "skip_code", ""),
            "edge_mode": getattr(edge, "mode", ""),
            "edge_value": _f(getattr(edge, "edge", 0)),
            "edge_ask": _f(getattr(edge, "ask_price", 0)),
            "edge_p_model": _f(getattr(edge, "p_model", 0)),
            "edge_ptb_bps": _f(getattr(edge, "ptb_bps", 0)),
        })
    if extra:
        feat.update(extra)
    return feat
