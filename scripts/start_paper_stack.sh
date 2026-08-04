#!/usr/bin/env bash
set -euo pipefail
cd /home/farm/rocky-trade-bot
mkdir -p logs

# stop old
pkill -9 -f 'src/main.py' 2>/dev/null || true
pkill -9 -f 'verify_trades' 2>/dev/null || true
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
updates = {
  'TRADING_MODE': 'paper',
  'ROCKY_ENGINE': 'v2',
  # LLM via local 9router gateway -> Zendy DeepSeek V4 Flash (thinking disabled)
  'LLM_API_URL': 'http://127.0.0.1:20128/v1/chat/completions',
  'LLM_API_KEY': 'sk-ec29591901e9df02-fmjzzv-9987fd46',
  'LLM_MODEL': 'zendy/deepseek',
  'LLM_FALLBACK_MODEL': 'oc/deepseek-v4-flash-free',
  'LLM_TEMPERATURE': '0.15',
  'LLM_TIMEOUT_SECONDS': '90',
  'ROCKY_STARTING_BALANCE': '100',
  'ROCKY_LOOP_INTERVAL': '60',
  'ROCKY_MIN_CONFIDENCE': '0.60',
  'ROCKY_MAX_RISK_PCT': '0.10',
  'ROCKY_DAILY_LOSS_LIMIT': '0.25',
  'ROCKY_TIMEZONE_OFFSET': '0',
  'TELEGRAM_BOT_TOKEN': '8655449536:AAEzTmjGSYpTMGssExGmQVkMxObcigW7-vM',
  'TELEGRAM_CHAT_ID': '1121992384',
  'ROCKY_NOTIFY_SKIPS': 'false',
  'ROCKY_MIN_T_ELAPSED': '50',
  'ROCKY_MIN_T_LEFT': '20',
  'ROCKY_MAX_T_LEFT': '280',
  'ROCKY_SWEET_MIN_T_LEFT': '60',
  'ROCKY_SWEET_MAX_T_LEFT': '150',
  'ROCKY_MIN_EDGE': '0.05',
  'ROCKY_MAX_ENTRY': '0.78',
  'ROCKY_MIN_ENTRY': '0.22',
  'ROCKY_MIN_PTB_BPS': '3',
  'ROCKY_FEE_BPS': '150',
  'ROCKY_SLIP_BPS': '50',
  'ROCKY_FLIP_1M_BPS': '2',
  'ROCKY_SNIPER_ENABLED': 'true',
  'LOG_LEVEL': 'INFO',
  'BINANCE_API': 'https://data-api.binance.vision/api/v3',
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
pgrep -af 'src/main.py|src/dashboard.py|verify_trades' || true

# start verify_trades (Polymarket/Binance cross-check, survives restarts)
nohup ./.venv/bin/python3 scripts/verify_trades.py --loop 60 --stop-after 0 > logs/verify.log 2>&1 &
echo "verify_pid $!"
