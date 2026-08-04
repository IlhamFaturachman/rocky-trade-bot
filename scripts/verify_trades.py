#!/usr/bin/env python3
"""
Verify Rocky trades against Polymarket gamma API.

Reads logs/trades.jsonl, finds resolved trades not yet gamma-verified,
queries https://gamma-api.polymarket.com/markets/{condition_id} for the
official outcome, and records results to logs/verify.csv.

Polls every 30s. If Polymarket hasn't resolved within ~3 min after the
window close, falls back to Binance candle (Rocky's existing result) and
flags the row as `pending`.

Usage:
  cd /home/farm/rocky-trade-bot
  ./.venv/bin/python3 scripts/verify_trades.py            # one-shot
  ./.venv/bin/python3 scripts/verify_trades.py --loop 30  # poll every 30s
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRADES = ROOT / "logs" / "trades.jsonl"
VERIFY_CSV = ROOT / "logs" / "verify.csv"

GAMMA_URL = "https://gamma-api.polymarket.com/markets?condition_id={cid}"
BINANCE_BASE = os.environ.get("BINANCE_API", "https://data-api.binance.vision/api/v3")
BINANCE_KLINES = f"{BINANCE_BASE}/klines"


def load_trades() -> list[dict]:
    if not TRADES.exists():
        return []
    rows = []
    for line in TRADES.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def load_verified() -> set[int]:
    """Trade IDs already in verify.csv."""
    if not VERIFY_CSV.exists():
        return set()
    ids = set()
    with open(VERIFY_CSV, newline="") as f:
        for row in csv.DictReader(f):
            try:
                ids.add(int(row["trade_id"]))
            except (KeyError, ValueError):
                continue
    return ids


def gamma_lookup(condition_id: str) -> str | None:
    """Query Polymarket gamma API. Returns 'up', 'down', or None (not resolved)."""
    if not condition_id:
        return None
    url = GAMMA_URL.format(cid=condition_id)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rocky-verify/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  gamma error for {condition_id}: {e}", file=sys.stderr)
        return None
    # gamma returns a list for condition_id query; take first market
    if isinstance(data, list):
        if not data:
            return None
        data = data[0]
    if not isinstance(data, dict):
        return None
    closed = data.get("closedTime") or data.get("endDate")
    if not closed:
        return None
    # Parse the winning outcome
    outcomes = data.get("outcomes") or []
    prices = data.get("outcomePrices") or []
    # outcomes is like ["Up", "Down"]; outcomePrices like ["1", "0"] (winner=1)
    if outcomes and prices:
        for i, p in enumerate(prices):
            if str(p) == "1":
                label = str(outcomes[i]).lower()
                if "up" in label or "yes" in label:
                    return "up"
                if "down" in label or "no" in label:
                    return "down"
    # Fallback: check umaResolution / winner field
    uma = data.get("umaResolution") or {}
    winner = uma.get("resolvedOutcome") or uma.get("winner") or data.get("winner")
    if winner:
        w = str(winner).lower()
        if "up" in w or "yes" in w:
            return "up"
        if "down" in w or "no" in w:
            return "down"
    return None


def binance_candle_close(ts: float) -> float | None:
    """Fetch the 5-min candle close at trade timestamp (Rocky's proxy)."""
    try:
        # Round down to 5m boundary
        boundary = int(ts // 300 * 300)
        start_ms = boundary * 1000
        url = f"{BINANCE_KLINES}?symbol=BTCUSDT&interval=5m&startTime={start_ms}&limit=1"
        req = urllib.request.Request(url, headers={"User-Agent": "rocky-verify/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        if data and isinstance(data, list) and len(data) > 0:
            return float(data[0][4])  # close price
    except Exception as e:
        print(f"  binance error: {e}", file=sys.stderr)
    return None


def verify_once() -> list[dict]:
    """Process all unverified resolved trades. Returns new rows."""
    trades = load_trades()
    verified = load_verified()
    new_rows = []

    # Collect resolved trades (last occurrence per trade_id wins — resolved event)
    resolved: dict[int, dict] = {}
    for t in trades:
        if t.get("_event") != "resolved":
            continue
        tid = t.get("trade_id")
        if tid is None:
            continue
        try:
            tid = int(tid)
        except (ValueError, TypeError):
            continue
        resolved[tid] = t

    for tid, t in resolved.items():
        if tid in verified:
            continue
        cid = str(t.get("condition_id") or "")
        direction = t.get("direction", "")
        rocky_result = t.get("result", "")
        candle_open = float(t.get("candle_open_price") or 0)
        candle_close = float(t.get("candle_close_price") or 0)
        ts = float(t.get("timestamp") or 0)
        poly_result = ""
        match = ""
        fallback = False
        if poly_outcome:
            poly_result = poly_outcome
            rocky_won = rocky_result == "win"
            poly_won = poly_outcome == direction
            match = "yes" if (rocky_won == poly_won) else "NO"
        else:
            age = time.time() - ts
            if age > 180:
                # Binance fallback: recompute outcome from candle close vs open
                bc = binance_candle_close(ts) or candle_close
                if bc and candle_open:
                    poly_result = "up" if bc >= candle_open else "down"
                    fallback = True
                    rocky_won = rocky_result == "win"
                    poly_won = poly_result == direction
                    match = "yes" if (rocky_won == poly_won) else "NO"

        if poly_result:
            row = {
                "trade_id": tid,
                "condition_id": cid,
                "direction": direction,
                "rocky_result": rocky_result,
                "poly_outcome": poly_result or "",
                "match": match or "",
                "fallback": "binance" if fallback else "gamma",
                "candle_open": candle_open,
                "candle_close": candle_close,
                "timestamp": ts,
            }
            new_rows.append(row)

    if new_rows:
        write_header = not VERIFY_CSV.exists()
        with open(VERIFY_CSV, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(new_rows[0].keys()))
            if write_header:
                w.writeheader()
            for r in new_rows:
                w.writerow(r)
        print(f"verified {len(new_rows)} new trade(s)")
    return new_rows


def print_summary():
    """Print running win-rate tally from verify.csv."""
    if not VERIFY_CSV.exists():
        print("no verify.csv yet")
        return
    rows = []
    with open(VERIFY_CSV, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    if not rows:
        print("verify.csv empty")
        return
    total = len(rows)
    gamma_only = [r for r in rows if r.get("fallback") == "gamma"]
    wins = sum(1 for r in rows if r["rocky_result"] == "win")
    match_ok = sum(1 for r in rows if r["match"] == "yes")
    print(f"--- summary ---")
    print(f"verified: {total}  gamma: {len(gamma_only)}  binance-fallback: {total - len(gamma_only)}")
    print(f"rocky wins: {wins}/{total} = {wins/total*100:.1f}%")
    print(f"rocky==poly match: {match_ok}/{total}")
    for r in rows:
        print(f"  #{r['trade_id']} {r['direction']:4} rocky={r['rocky_result']:4} "
              f"poly={r['poly_outcome']:4} match={r['match']:3} ({r['fallback']})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="poll interval seconds (0 = one-shot)")
    ap.add_argument("--stop-after", type=int, default=10, help="stop after N verified trades (0 = no limit)")
    args = ap.parse_args()

    count = 0
    while True:
        new = verify_once()
        count += len(new)
        print_summary()
        if args.stop_after and count >= args.stop_after:
            print(f"reached {count} verified trades, stopping")
            break
        if args.loop <= 0:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
