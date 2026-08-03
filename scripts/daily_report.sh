#!/usr/bin/env bash
# Daily Telegram report for Rocky 7-day collection
set -euo pipefail
cd /home/farm/rocky-trade-bot
set -a
# shellcheck disable=SC1091
source <(grep -v '^#' .env | sed 's/\r$//')
set +a

REPORT=$(./.venv/bin/python3 scripts/analyze_collection.py --days 1 2>&1 | tail -80)
# also short summary JSON
SUMMARY=$(./.venv/bin/python3 - <<'PY'
import json, time
from pathlib import Path
from collections import Counter
logs=Path('logs')
now=time.time(); day=now-86400
def load(p):
  rows=[]
  if not p.exists(): return rows
  for line in p.read_text().splitlines():
    try: o=json.loads(line)
    except: continue
    ts=float(o.get('timestamp') or o.get('ts') or 0)
    if ts>=day: rows.append(o)
  return rows
trades=load(logs/'trades.jsonl')
cycles=load(logs/'cycles.jsonl')
res=[t for t in trades if t.get('_event')=='resolved']
real=[t for t in res if not t.get('is_shadow') and (t.get('flag') in (None,'','real'))]
sh=[t for t in res if t.get('is_shadow') or (t.get('flag') not in (None,'','real'))]
def wr(r):
  if not r: return 0
  return sum(1 for t in r if t.get('result')=='win')/len(r)
def pnl(r): return sum(float(t.get('pnl') or 0) for t in r)
state={}
try: state=json.loads((logs/'state.json').read_text())
except: pass
flags=Counter((t.get('flag') or 'shadow') for t in sh)
top=', '.join(f'{k}:{v}' for k,v in flags.most_common(5))
print(f"balance=${state.get('balance',0):.2f} cycles24h={len(cycles)} real={len(real)} WR={wr(real):.0%} PnL={pnl(real):+.2f} shadow={len(sh)} WR={wr(sh):.0%} wouldPnL={pnl(sh):+.2f} flags[{top}]")
PY
)

TOKEN="${TELEGRAM_BOT_TOKEN:-}"
CHAT="${TELEGRAM_CHAT_ID:-}"
if [[ -n "$TOKEN" && -n "$CHAT" ]]; then
  TEXT="🪨 <b>Rocky daily collection</b>%0A<code>${SUMMARY}</code>%0A%0A<pre>$(echo "$REPORT" | sed 's/&/\&amp;/g;s/</\&lt;/g;s/>/\&gt;/g' | head -40)</pre>"
  curl -sS -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    -d "chat_id=${CHAT}" \
    -d "parse_mode=HTML" \
    -d "text=${TEXT}" >/dev/null || true
fi
echo "$SUMMARY"
echo "$REPORT" | tail -30
