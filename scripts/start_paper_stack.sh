#!/usr/bin/env bash
set -euo pipefail
cd /home/farm/rocky-trade-bot
mkdir -p logs

# stop old
pkill -9 -f 'rocky-trade-bot/.venv/bin/python3 src/main.py' 2>/dev/null || true
pkill -9 -f 'src/dashboard.py' 2>/dev/null || true
sleep 1

# write .env
python3 <<'PY'
from pathlib import Path
import yaml
p = Path('/home/farm/rocky-trade-bot/.env')
kv = {}
if p.exists():
    for line in p.read_text().splitlines():
        if not line.strip() or line.strip().startswith('#') or '=' not in line:
            continue
        k,v = line.split('=',1)
        kv[k.strip()] = v.strip()
hc = yaml.safe_load(Path('/home/farm/.hermes/config.yaml').read_text())
key = hc['model']['api_key']
updates = {
  'TRADING_MODE': 'paper',
  'ROCKY_ENGINE': 'v2',
  'LLM_API_URL': 'http://127.0.0.1:8080/v1/chat/completions',
  'LLM_API_KEY': key,
  'LLM_MODEL': 'grok-4.5',
  'LLM_MAX_TOKENS': '1024',
  'LLM_TEMPERATURE': '0.15',
  'LLM_TIMEOUT_SECONDS': '90',
  'ROCKY_STARTING_BALANCE': '100',
  'ROCKY_LOOP_INTERVAL': '60',
  'ROCKY_MIN_CONFIDENCE': '0.68',
  'ROCKY_MAX_RISK_PCT': '0.10',
  'ROCKY_DAILY_LOSS_LIMIT': '0.25',
  'ROCKY_TIMEZONE_OFFSET': '0',
  'TELEGRAM_BOT_TOKEN': '8655449536:AAEzTmjGSYpTMGssExGmQVkMxObcigW7-vM',
  'TELEGRAM_CHAT_ID': '1121992384',
  'ROCKY_NOTIFY_SKIPS': 'false',
  'ROCKY_MIN_T_ELAPSED': '50',
  'ROCKY_MIN_T_LEFT': '30',
  'ROCKY_MAX_T_LEFT': '180',
  'ROCKY_SWEET_MIN_T_LEFT': '60',
  'ROCKY_SWEET_MAX_T_LEFT': '150',
  'ROCKY_MIN_EDGE': '0.05',
  'ROCKY_MAX_ENTRY': '0.70',
  'ROCKY_MIN_ENTRY': '0.28',
  'ROCKY_MIN_PTB_BPS': '3',
  'ROCKY_FEE_BPS': '150',
  'ROCKY_SLIP_BPS': '50',
  'ROCKY_FLIP_1M_BPS': '2',
  'ROCKY_SNIPER_ENABLED': 'true',
  'LOG_LEVEL': 'INFO',
}
kv.update(updates)
lines = ['# Rocky paper + Edge Pack + Telegram @gracerockyy_bot']
for k,v in updates.items():
    lines.append(f'{k}={v}')
for k,v in kv.items():
    if k not in updates:
        lines.append(f'{k}={v}')
p.write_text('\n'.join(lines)+'\n')
print('env ok', 'chat', kv['TELEGRAM_CHAT_ID'], 'model', kv['LLM_MODEL'])
PY

./.venv/bin/python3 -m py_compile src/edge.py src/main.py src/executor.py src/models.py src/dashboard.py src/decision_v2.py src/scanner.py
echo compile_ok

# export env for children
set -a
# shellcheck disable=SC1091
source <(grep -v '^#' .env | sed 's/\r$//')
set +a

# start dashboard
nohup ./.venv/bin/python3 src/dashboard.py --host 0.0.0.0 --port 8787 > logs/dashboard.log 2>&1 &
echo "dashboard_pid $!"

# start paper
nohup ./.venv/bin/python3 src/main.py --mode paper --engine v2 --interval 60 --balance 100 > logs/paper.stdout 2>&1 &
echo "paper_pid $!"
sleep 3
pgrep -af 'src/main.py|src/dashboard.py' || true
curl -sS --max-time 3 http://127.0.0.1:8787/api/status | head -c 400; echo
tail -20 logs/paper.stdout 2>/dev/null || tail -20 logs/rocky.log 2>/dev/null || true
