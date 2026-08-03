#!/usr/bin/env bash
# Isolate Rocky for 7-day maximal data collection
set -euo pipefail
ROOT=/home/farm/rocky-trade-bot
cd "$ROOT"
mkdir -p logs run

# stop previous
pkill -9 -f "$ROOT/.venv/bin/python3 src/main.py" 2>/dev/null || true
pkill -9 -f "$ROOT/.venv/bin/python3 src/dashboard.py" 2>/dev/null || true
pkill -9 -f "$ROOT/scripts/watchdog.sh" 2>/dev/null || true
sleep 1

# ensure env research flags
python3 <<'PY'
from pathlib import Path
p=Path('/home/farm/rocky-trade-bot/.env')
kv={}
if p.exists():
  for line in p.read_text().splitlines():
    if not line.strip() or line.strip().startswith('#') or '=' not in line: continue
    k,v=line.split('=',1); kv[k.strip()]=v.strip()
import yaml
key=yaml.safe_load(Path('/home/farm/.hermes/config.yaml').read_text())['model']['api_key']
kv.update({
  'TRADING_MODE':'paper',
  'ROCKY_ENGINE':'v2',
  'LLM_API_URL':'http://127.0.0.1:8080/v1/chat/completions',
  'LLM_API_KEY':key,
  'LLM_MODEL':'grok-4.5',
  'LLM_MAX_TOKENS':'700',
  'LLM_TEMPERATURE':'0.15',
  'LLM_TIMEOUT_SECONDS':'25',
  'LLM_MAX_ATTEMPTS':'2',
  'ROCKY_STARTING_BALANCE': kv.get('ROCKY_STARTING_BALANCE','100'),
  'ROCKY_LOOP_INTERVAL':'45',  # denser sampling for research
  'ROCKY_DATA_COLLECT':'true',
  'ROCKY_MIN_CONFIDENCE':'0.68',
  'ROCKY_MAX_RISK_PCT':'0.10',
  'ROCKY_DAILY_LOSS_LIMIT':'0.25',
  'ROCKY_MIN_T_ELAPSED':'50',
  'ROCKY_MIN_T_LEFT':'30',
  'ROCKY_MAX_T_LEFT':'180',
  'ROCKY_MIN_EDGE':'0.05',
  'ROCKY_MAX_ENTRY':'0.70',
  'ROCKY_MIN_ENTRY':'0.28',
  'ROCKY_MIN_PTB_BPS':'3',
  'ROCKY_FEE_BPS':'150',
  'ROCKY_SLIP_BPS':'50',
  'ROCKY_SNIPER_ENABLED':'true',
  'ROCKY_NOTIFY_SKIPS':'false',
  'LOG_LEVEL':'INFO',
  'TELEGRAM_BOT_TOKEN': kv.get('TELEGRAM_BOT_TOKEN','8655449536:AAEzTmjGSYpTMGssExGmQVkMxObcigW7-vM'),
  'TELEGRAM_CHAT_ID': kv.get('TELEGRAM_CHAT_ID','1121992384'),
})
lines=['# Rocky 7-day isolated data collection']+[f'{k}={v}' for k,v in kv.items()]
p.write_text('\n'.join(lines)+'\n')
print('env ready engine', kv['ROCKY_ENGINE'], 'interval', kv['ROCKY_LOOP_INTERVAL'], 'collect', kv['ROCKY_DATA_COLLECT'])
PY

chmod +x scripts/watchdog.sh scripts/daily_report.sh scripts/analyze_collection.py 2>/dev/null || true
./.venv/bin/python3 -m py_compile src/main.py src/executor.py src/edge.py src/features.py src/dashboard.py src/decision_v2.py

# mark collection start
date -u +%Y-%m-%dT%H:%M:%SZ > run/collection_start_utc.txt
echo "7d" > run/collection_mode.txt

# start via watchdog (it will spawn paper+dash)
nohup bash scripts/watchdog.sh > logs/watchdog.stdout 2>&1 &
echo "watchdog_pid $!"
sleep 4
pgrep -af 'src/main.py|src/dashboard.py|watchdog.sh' | head -10 || true
curl -sS --max-time 3 http://127.0.0.1:8787/api/status | head -c 200; echo
echo "collection started at $(cat run/collection_start_utc.txt)"
