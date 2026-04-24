# 🪨 Rocky — VPS Deployment Guide

## Prerequisites

- **VPS:** Debian 12, 2GB RAM, 2 CPU cores, 20GB disk
- **Access:** Root or sudo SSH access
- **From Mac:** Your `openclaw.json` config (has Telegram, LLM keys)
- **API Keys:** enowX Labs API key, Telegram bot token + chat ID

## Step-by-Step Deployment

### 1. Create the deploy package (on your Mac)

```bash
cd /Users/macairm12020/Documents/OpenClaw/openclaw-tradingbot-1
./deploy/deploy-package.sh
# Creates: /tmp/rocky-deploy.tar.gz
```

### 2. Transfer to VPS

```bash
scp /tmp/rocky-deploy.tar.gz root@YOUR_VPS_IP:/tmp/
```

### 3. SSH into VPS

```bash
ssh root@YOUR_VPS_IP
```

### 4. Extract deploy package

```bash
cd /tmp
tar xzf rocky-deploy.tar.gz
cd rocky-deploy
```

### 5. Copy your OpenClaw config from Mac

On your Mac:
```bash
scp ~/.openclaw/openclaw.json root@YOUR_VPS_IP:/tmp/rocky-deploy/openclaw.json
```

This preserves your existing Telegram bot, enowX Labs API key, and hook configuration.

### 6. Edit .env with your keys

```bash
cp .env.example .env
nano .env
```

Fill in:
- `LLM_API_KEY` — your enowX Labs API key
- `TELEGRAM_BOT_TOKEN` — your Telegram bot token
- `TELEGRAM_CHAT_ID` — your Telegram chat ID
- `ROCKY_ENGINE=v2` — to use LLM-powered trading
- `ROCKY_TIMEZONE_OFFSET=7` — for GMT+7

For live trading (later):
- `TRADING_MODE=live`
- `POLY_PRIVATE_KEY=0x...`

### 7. Run the deploy script

```bash
sudo ./deploy/deploy-vps.sh
```

This installs everything:
- Node.js 24, Python 3, Docker
- OpenClaw (global)
- Rocky Python code → `/opt/rocky/`
- SearXNG → Docker container on port 8888
- OpenClaw config with trading overlay
- Systemd services (auto-start on reboot)
- Sends Telegram test message

### 8. Verify

```bash
# Check services
systemctl status rocky
systemctl status openclaw-gateway

# Watch live trading logs
journalctl -u rocky -f

# Check balance
cat /opt/rocky/logs/state.json | jq .

# Check recent trades
tail -5 /opt/rocky/logs/trades.jsonl | jq .
```

You should also receive a Telegram message: "🪨 Rocky Trading Bot is online!"

### 9. Monitor paper trading

Let it run for a few hours in paper mode. Check:
- Are trades being placed?
- Is the win rate reasonable?
- Are Telegram notifications working?

### 10. Go live

When ready:
```bash
sudo nano /opt/rocky/.env
# Change:
#   TRADING_MODE=live
#   POLY_PRIVATE_KEY=0x...
sudo systemctl restart rocky
```

## Architecture

```
VPS (Debian 12)
├── OpenClaw Gateway (systemd)
│   ├── Telegram bot integration
│   ├── Cron: 5-min trading cycles
│   ├── Cron: hourly stats report
│   └── Agent workspace: /opt/rocky/
├── Rocky Python Bot (systemd)
│   ├── Market scanner (Polymarket API)
│   ├── Intelligence engine (Binance + SearXNG)
│   ├── Decision engine v2 (Opus 4.6)
│   ├── Executor (paper/live)
│   └── Telegram notifier
├── SearXNG (Docker)
│   └── Crypto news search on :8888
└── Files
    ├── /opt/rocky/.env              # API keys
    ├── /opt/rocky/logs/state.json   # Balance & state
    ├── /opt/rocky/logs/trades.jsonl # Trade journal
    └── /home/rocky/.openclaw/       # OpenClaw config
```

## Troubleshooting

### Rocky service won't start
```bash
journalctl -u rocky -n 50 --no-pager
# Common: missing .env vars, Python import errors
```

### OpenClaw gateway won't start
```bash
journalctl -u openclaw-gateway -n 50 --no-pager
# Common: missing openclaw.json, port conflicts
```

### SearXNG not responding
```bash
docker ps | grep searxng
docker logs rocky-searxng --tail 20
# Restart: cd /opt/rocky && docker compose restart
```

### No markets found
- Polymarket BTC 5m markets may not be active 24/7
- Check: `curl -s "https://gamma-api.polymarket.com/events?active=true&closed=false&series_slug=btc-up-or-down-5m&limit=1" | jq .`

### Telegram not working
```bash
cd /opt/rocky
source .env
./venv/bin/python3 scripts/setup-telegram-test.py
```

### LLM API errors (v2 engine)
```bash
# Test the API directly
curl -s https://api.enowxlabs.com/v1/chat/completions \
  -H "Authorization: Bearer $LLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"enowxlabs/claude-opus-4.6","messages":[{"role":"user","content":"ping"}],"max_tokens":10}'
```

### Reset trading state
```bash
# Caution: resets balance and trade history
sudo systemctl stop rocky
rm /opt/rocky/logs/state.json
rm /opt/rocky/logs/trades.jsonl
sudo systemctl start rocky
```

## Updating Rocky

```bash
# On Mac: rebuild package
cd /Users/macairm12020/Documents/OpenClaw/openclaw-tradingbot-1
./deploy/deploy-package.sh

# Transfer and extract
scp /tmp/rocky-deploy.tar.gz root@VPS:/tmp/
ssh root@VPS "cd /tmp && tar xzf rocky-deploy.tar.gz && cd rocky-deploy && sudo ./deploy/deploy-vps.sh"
```

The deploy script is idempotent — it preserves `.env` and state files.
