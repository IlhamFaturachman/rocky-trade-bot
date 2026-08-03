#!/usr/bin/env python3
"""
Post-collection analyzer for Rocky 7-day paper/shadow journal.

Usage:
  cd /home/farm/rocky-trade-bot
  ./.venv/bin/python3 scripts/analyze_collection.py
  ./.venv/bin/python3 scripts/analyze_collection.py --days 7
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs"


def load_jsonl(path: Path):
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--root", default=str(ROOT))
    args = ap.parse_args()
    logs = Path(args.root) / "logs"
    cutoff = time.time() - args.days * 86400

    trades = [t for t in load_jsonl(logs / "trades.jsonl") if float(t.get("timestamp") or 0) >= cutoff]
    cycles = [c for c in load_jsonl(logs / "cycles.jsonl") if float(c.get("ts") or 0) >= cutoff]
    skips = [s for s in load_jsonl(logs / "skips.jsonl") if float(s.get("ts") or 0) >= cutoff]

    opened = [t for t in trades if t.get("_event") == "opened"]
    resolved = [t for t in trades if t.get("_event") == "resolved"]

    def shadowish(t):
        if "is_shadow" in t:
            return bool(t.get("is_shadow"))
        return (t.get("flag") or "real") != "real"

    real_res = [t for t in resolved if not shadowish(t)]
    sh_res = [t for t in resolved if shadowish(t)]

    def wr(rows):
        if not rows:
            return 0.0
        w = sum(1 for t in rows if t.get("result") == "win")
        return w / len(rows)

    def pnl(rows):
        return sum(float(t.get("pnl") or 0) for t in rows)

    print("=" * 60)
    print(f"Rocky collection analysis — last {args.days}d")
    print("=" * 60)
    print(f"cycles logged: {len(cycles)}")
    print(f"skips logged:  {len(skips)}")
    print(f"opened: {len(opened)}  resolved: {len(resolved)}")
    print()
    print(f"REAL    n={len(real_res)}  WR={wr(real_res):.1%}  PnL={pnl(real_res):+.4f}")
    print(f"SHADOW  n={len(sh_res)}  WR={wr(sh_res):.1%}  would-be PnL={pnl(sh_res):+.4f}")
    print()

    by_flag = defaultdict(list)
    for t in sh_res:
        by_flag[t.get("flag") or "shadow"].append(t)
    print("Shadow by flag:")
    for flag, rows in sorted(by_flag.items(), key=lambda kv: -len(kv[1])):
        print(f"  {flag:16} n={len(rows):4d}  WR={wr(rows):6.1%}  PnL={pnl(rows):+.4f}")

    # Phase / hour if present on resolved via joined opened
    print()
    by_hour = defaultdict(list)
    for t in sh_res:
        ts = float(t.get("timestamp") or 0)
        if ts:
            by_hour[time.gmtime(ts).tm_hour].append(t)
    if by_hour:
        print("Shadow WR by UTC hour (min 5 samples):")
        for h in range(24):
            rows = by_hour.get(h, [])
            if len(rows) >= 5:
                print(f"  hour {h:02d}  n={len(rows):3d}  WR={wr(rows):6.1%}  PnL={pnl(rows):+.4f}")

    # Simple recommendation
    print()
    print("Heuristic gates for live (from shadow):")
    good = []
    for flag, rows in by_flag.items():
        if len(rows) >= 20 and wr(rows) >= 0.55 and pnl(rows) > 0:
            good.append((flag, wr(rows), pnl(rows), len(rows)))
    if good:
        for flag, w, p, n in sorted(good, key=lambda x: -x[1]):
            print(f"  KEEP-ish flag={flag} WR={w:.1%} PnL={p:+.2f} n={n}")
    else:
        print("  No flag yet has n>=20, WR>=55%, PnL>0 — keep collecting.")

    # Edge buckets if edge field present
    buckets = [(0, 0.02), (0.02, 0.05), (0.05, 0.08), (0.08, 1.0)]
    print()
    print("Shadow WR by edge bucket:")
    for lo, hi in buckets:
        rows = [t for t in sh_res if lo <= float(t.get("edge") or 0) < hi]
        if rows:
            print(f"  edge[{lo:.2f},{hi:.2f}) n={len(rows):3d} WR={wr(rows):6.1%} PnL={pnl(rows):+.4f}")

    out = {
        "days": args.days,
        "cycles": len(cycles),
        "real": {"n": len(real_res), "wr": wr(real_res), "pnl": pnl(real_res)},
        "shadow": {"n": len(sh_res), "wr": wr(sh_res), "pnl": pnl(sh_res)},
        "by_flag": {
            f: {"n": len(r), "wr": wr(r), "pnl": pnl(r)} for f, r in by_flag.items()
        },
    }
    path = logs / "analysis_summary.json"
    path.write_text(json.dumps(out, indent=2))
    print()
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
