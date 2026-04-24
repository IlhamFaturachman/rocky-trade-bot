#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# Rocky PolyClaw Trader — VPS Master Deployment Script
# Target: Debian 12 (2GB RAM, 2 CPU, 20GB disk)
#
# Usage:
#   sudo ./deploy-vps.sh
#
# Prerequisites:
#   - Fresh Debian 12 VPS with root/sudo access
#   - Files in /tmp/rocky-deploy/ (from deploy-package.sh)
#   - .env filled with API keys
#   - openclaw.json copied from Mac (optional, will use overlay if missing)
#
# This script is idempotent — safe to run multiple times.
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

ROCKY_DIR="/opt/rocky"
ROCKY_USER="rocky"
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENCLAW_HOME="/home/${ROCKY_USER}/.openclaw"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; exit 1; }
step() { echo -e "\n${CYAN}═══ $* ═══${NC}"; }

# ── Pre-flight ────────────────────────────────────────────────────────────────

if [[ $EUID -ne 0 ]]; then
    err "Run as root: sudo ./deploy-vps.sh"
fi

echo -e "${CYAN}"
echo "  ╔═══════════════════════════════════════════╗"
echo "  ║  🪨 Rocky PolyClaw Trader — VPS Deploy    ║"
echo "  ╚═══════════════════════════════════════════╝"
echo -e "${NC}"

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1: System packages
# ═══════════════════════════════════════════════════════════════════════════════

step "1/13 — System packages"

export DEBIAN_FRONTEND=noninteractive

apt-get update -qq

# Install base packages
apt-get install -y -qq \
    curl wget git jq ca-certificates gnupg lsb-release \
    python3 python3-pip python3-venv python3-dev \
    build-essential \
    > /dev/null 2>&1
log "Base packages installed"

# Install Node.js 24 via nodesource
if ! command -v node &>/dev/null || [[ "$(node -v 2>/dev/null)" != v24* ]]; then
    curl -fsSL https://deb.nodesource.com/setup_24.x | bash - > /dev/null 2>&1
    apt-get install -y -qq nodejs > /dev/null 2>&1
    log "Node.js $(node -v) installed"
else
    log "Node.js $(node -v) already installed"
fi

# Install Docker
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sh > /dev/null 2>&1
    systemctl enable --now docker
    log "Docker installed and running"
else
    log "Docker already installed"
fi

# Install docker-compose (v2 plugin or standalone)
if ! docker compose version &>/dev/null 2>&1; then
    apt-get install -y -qq docker-compose-plugin > /dev/null 2>&1 || \
    apt-get install -y -qq docker-compose > /dev/null 2>&1 || true
fi
log "Docker Compose available"

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2: Install OpenClaw
# ═══════════════════════════════════════════════════════════════════════════════

step "2/13 — OpenClaw"

if ! command -v openclaw &>/dev/null; then
    npm install -g openclaw@latest > /dev/null 2>&1
    log "OpenClaw $(openclaw --version 2>/dev/null || echo 'installed')"
else
    log "OpenClaw already installed: $(openclaw --version 2>/dev/null || echo 'present')"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3: Create rocky user
# ═══════════════════════════════════════════════════════════════════════════════

step "3/13 — Rocky user"

if ! id "$ROCKY_USER" &>/dev/null; then
    useradd --system --shell /bin/bash --home-dir "/home/${ROCKY_USER}" --create-home "$ROCKY_USER"
    log "Created user: $ROCKY_USER"
else
    log "User $ROCKY_USER already exists"
fi

# Add to docker group
usermod -aG docker "$ROCKY_USER" 2>/dev/null || true

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 4: Copy Rocky Python code
# ═══════════════════════════════════════════════════════════════════════════════

step "4/13 — Rocky source code"

mkdir -p "$ROCKY_DIR"/{src,tests,logs,skills/rocky,searxng,scripts}

# Copy source
cp -r "$DEPLOY_DIR"/src/* "$ROCKY_DIR/src/"
cp -r "$DEPLOY_DIR"/tests/* "$ROCKY_DIR/tests/"
cp "$DEPLOY_DIR/requirements.txt" "$ROCKY_DIR/"
cp "$DEPLOY_DIR/docker-compose.yml" "$ROCKY_DIR/"
cp -r "$DEPLOY_DIR"/searxng/* "$ROCKY_DIR/searxng/"
cp "$DEPLOY_DIR/scripts/setup-telegram-test.py" "$ROCKY_DIR/scripts/" 2>/dev/null || true

# Copy .env (don't overwrite if exists)
if [[ -f "$DEPLOY_DIR/.env" ]]; then
    cp "$DEPLOY_DIR/.env" "$ROCKY_DIR/.env"
    log "Copied .env"
elif [[ ! -f "$ROCKY_DIR/.env" ]]; then
    cp "$DEPLOY_DIR/.env.example" "$ROCKY_DIR/.env"
    warn "No .env found — copied .env.example. EDIT /opt/rocky/.env before going live!"
else
    log ".env already exists, not overwriting"
fi

log "Source code deployed to $ROCKY_DIR"

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 5: Python venv + dependencies
# ═══════════════════════════════════════════════════════════════════════════════

step "5/13 — Python environment"

if [[ ! -d "$ROCKY_DIR/venv" ]]; then
    python3 -m venv "$ROCKY_DIR/venv"
    log "Created venv"
else
    log "Venv already exists"
fi

"$ROCKY_DIR/venv/bin/pip" install --upgrade pip -q 2>/dev/null
"$ROCKY_DIR/venv/bin/pip" install -r "$ROCKY_DIR/requirements.txt" -q 2>/dev/null
log "Python dependencies installed"

# Verify
"$ROCKY_DIR/venv/bin/python3" -c "import requests; print(f'  requests {requests.__version__}')"

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 6: SearXNG
# ═══════════════════════════════════════════════════════════════════════════════

step "6/13 — SearXNG (news search)"

# Generate random secret if placeholder exists
SEARX_SECRET=$(openssl rand -hex 32)
sed -i "s/rocky-searxng-change-me-in-production/$SEARX_SECRET/" \
    "$ROCKY_DIR/searxng/settings.yml" 2>/dev/null || true

cd "$ROCKY_DIR"

# Use docker compose v2 or fall back to v1
if docker compose version &>/dev/null 2>&1; then
    docker compose up -d 2>/dev/null
else
    docker-compose up -d 2>/dev/null
fi

log "SearXNG container started"

# Wait for ready
echo -n "  Waiting for SearXNG..."
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:8888/ > /dev/null 2>&1; then
        echo " ready!"
        break
    fi
    echo -n "."
    sleep 2
done
log "SearXNG at http://127.0.0.1:8888"

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 7: OpenClaw config
# ═══════════════════════════════════════════════════════════════════════════════

step "7/13 — OpenClaw configuration"

mkdir -p "$OPENCLAW_HOME"

# If user provided their openclaw.json from Mac, use it as base
if [[ -f "$DEPLOY_DIR/openclaw.json" ]]; then
    cp "$DEPLOY_DIR/openclaw.json" "$OPENCLAW_HOME/openclaw.json"
    log "Copied openclaw.json from deploy package"

    # Merge trading-specific config overlay
    if [[ -f "$DEPLOY_DIR/deploy/openclaw-trading-config.json" ]] && command -v jq &>/dev/null; then
        # Merge: base config + trading overlay (overlay wins on conflicts)
        TEMP_MERGED=$(mktemp)
        jq -s '.[0] * .[1]' \
            "$OPENCLAW_HOME/openclaw.json" \
            "$DEPLOY_DIR/deploy/openclaw-trading-config.json" \
            > "$TEMP_MERGED" 2>/dev/null && \
        mv "$TEMP_MERGED" "$OPENCLAW_HOME/openclaw.json"
        log "Merged trading config overlay"
    fi
elif [[ ! -f "$OPENCLAW_HOME/openclaw.json" ]]; then
    # No base config — use the overlay as starting point
    if [[ -f "$DEPLOY_DIR/deploy/openclaw-trading-config.json" ]]; then
        cp "$DEPLOY_DIR/deploy/openclaw-trading-config.json" "$OPENCLAW_HOME/openclaw.json"
        warn "No openclaw.json from Mac — using trading config only. Copy your Mac config for full setup."
    fi
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 8: Agent workspace files (SOUL.md, BOOT.md)
# ═══════════════════════════════════════════════════════════════════════════════

step "8/13 — Agent workspace"

# Create OpenClaw workspace pointing to /opt/rocky
mkdir -p "$OPENCLAW_HOME/workspace"

# SOUL.md and BOOT.md go in the rocky dir (which IS the workspace)
cp "$DEPLOY_DIR/deploy/SOUL.md" "$ROCKY_DIR/SOUL.md"
cp "$DEPLOY_DIR/deploy/BOOT.md" "$ROCKY_DIR/BOOT.md"

# Create AGENTS.md for the workspace
cat > "$ROCKY_DIR/AGENTS.md" << 'AGENTS_EOF'
# AGENTS.md — Rocky Trading Workspace

You are Rocky, an autonomous BTC prediction market trader.
Your workspace is /opt/rocky. Your trading code is in src/.
Your trade journal is in logs/trades.jsonl.
Your state is in logs/state.json.

On startup, follow BOOT.md.
Your identity and personality are in SOUL.md.
Your trading skill commands are in skills/rocky/SKILL.md.
AGENTS_EOF

# Create memory directory
mkdir -p "$ROCKY_DIR/memory"

log "SOUL.md, BOOT.md, AGENTS.md deployed"

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 9: Rocky trading skill
# ═══════════════════════════════════════════════════════════════════════════════

step "9/13 — Rocky trading skill"

# Install skill in both locations
mkdir -p "$OPENCLAW_HOME/skills/rocky"
cp "$DEPLOY_DIR/skills/rocky/SKILL.md" "$ROCKY_DIR/skills/rocky/SKILL.md"
cp "$DEPLOY_DIR/skills/rocky/SKILL.md" "$OPENCLAW_HOME/skills/rocky/SKILL.md"

log "Rocky skill installed"

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 10: Cron (configured via openclaw.json, verified here)
# ═══════════════════════════════════════════════════════════════════════════════

step "10/13 — Cron jobs"

if [[ -f "$OPENCLAW_HOME/openclaw.json" ]] && command -v jq &>/dev/null; then
    CRON_COUNT=$(jq '.cron | length' "$OPENCLAW_HOME/openclaw.json" 2>/dev/null || echo "0")
    log "OpenClaw cron jobs configured: $CRON_COUNT"
else
    warn "Cron jobs are defined in openclaw.json — verify after setup"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 11: Systemd services
# ═══════════════════════════════════════════════════════════════════════════════

step "11/13 — Systemd services"

# Rocky Python trading bot service
cat > /etc/systemd/system/rocky.service << EOF
[Unit]
Description=Rocky PolyClaw Trader - Autonomous BTC Trading Agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=${ROCKY_USER}
Group=${ROCKY_USER}
WorkingDirectory=${ROCKY_DIR}
EnvironmentFile=${ROCKY_DIR}/.env
ExecStart=${ROCKY_DIR}/venv/bin/python3 src/main.py
Restart=always
RestartSec=30
StartLimitIntervalSec=600
StartLimitBurst=10

StandardOutput=append:${ROCKY_DIR}/logs/rocky-service.log
StandardError=append:${ROCKY_DIR}/logs/rocky-service.log

NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

# OpenClaw gateway service
cat > /etc/systemd/system/openclaw-gateway.service << EOF
[Unit]
Description=OpenClaw Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${ROCKY_USER}
Group=${ROCKY_USER}
Environment=HOME=/home/${ROCKY_USER}
ExecStart=$(which openclaw) gateway start --foreground
Restart=always
RestartSec=10

StandardOutput=append:${ROCKY_DIR}/logs/openclaw-gateway.log
StandardError=append:${ROCKY_DIR}/logs/openclaw-gateway.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable rocky.service
systemctl enable openclaw-gateway.service
log "Systemd services installed and enabled"

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 12: Fix ownership and start everything
# ═══════════════════════════════════════════════════════════════════════════════

step "12/13 — Permissions & startup"

chown -R "${ROCKY_USER}:${ROCKY_USER}" "$ROCKY_DIR"
chown -R "${ROCKY_USER}:${ROCKY_USER}" "$OPENCLAW_HOME"
chown -R "${ROCKY_USER}:${ROCKY_USER}" "/home/${ROCKY_USER}"
chmod 600 "$ROCKY_DIR/.env"

# Run tests first
log "Running tests..."
cd "$ROCKY_DIR"
su -s /bin/bash "$ROCKY_USER" -c "cd $ROCKY_DIR && ./venv/bin/python3 tests/test_core.py" 2>&1 | tail -1
su -s /bin/bash "$ROCKY_USER" -c "cd $ROCKY_DIR && ./venv/bin/python3 tests/test_v2.py" 2>&1 | tail -1

# Start OpenClaw gateway first
systemctl start openclaw-gateway.service || warn "OpenClaw gateway may need manual config"
sleep 3

# Start Rocky
systemctl start rocky.service
sleep 2

if systemctl is-active --quiet rocky.service; then
    log "Rocky trading bot is RUNNING 🪨"
else
    warn "Rocky service may not have started. Check: journalctl -u rocky -f"
fi

if systemctl is-active --quiet openclaw-gateway.service; then
    log "OpenClaw gateway is RUNNING"
else
    warn "OpenClaw gateway may need configuration. Check: journalctl -u openclaw-gateway -f"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 13: Telegram test
# ═══════════════════════════════════════════════════════════════════════════════

step "13/13 — Telegram verification"

# Source .env for Telegram vars
set -a
source "$ROCKY_DIR/.env" 2>/dev/null || true
set +a

if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]] && [[ -n "${TELEGRAM_CHAT_ID:-}" ]]; then
    "$ROCKY_DIR/venv/bin/python3" -c "
import requests
token = '${TELEGRAM_BOT_TOKEN}'
chat_id = '${TELEGRAM_CHAT_ID}'
msg = '🪨 *Rocky Trading Bot is online!*\n\nVPS deployment complete. Ready to trade.'
resp = requests.post(
    f'https://api.telegram.org/bot{token}/sendMessage',
    json={'chat_id': chat_id, 'text': msg, 'parse_mode': 'Markdown'},
    timeout=10
)
if resp.status_code == 200:
    print('  ✅ Telegram test message sent!')
else:
    print(f'  ⚠️  Telegram send failed: {resp.status_code}')
" 2>/dev/null || warn "Telegram test failed — check bot token and chat ID"
else
    warn "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in .env — skipping Telegram test"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════════

echo ""
echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  🪨 Rocky PolyClaw Trader — Deployment Complete!${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Rocky code:       $ROCKY_DIR"
echo "  OpenClaw config:  $OPENCLAW_HOME/openclaw.json"
echo "  Environment:      $ROCKY_DIR/.env"
echo "  Logs:             $ROCKY_DIR/logs/"
echo "  Trade journal:    $ROCKY_DIR/logs/trades.jsonl"
echo "  SearXNG:          http://127.0.0.1:8888"
echo ""
echo "  Services:"
echo "    systemctl status rocky              # Trading bot"
echo "    systemctl status openclaw-gateway   # OpenClaw gateway"
echo "    journalctl -u rocky -f              # Live trading logs"
echo "    journalctl -u openclaw-gateway -f   # Gateway logs"
echo ""
echo "  Quick commands:"
echo "    systemctl restart rocky             # Restart trading"
echo "    systemctl stop rocky                # Stop trading"
echo "    cat $ROCKY_DIR/logs/state.json      # Check balance"
echo "    tail -5 $ROCKY_DIR/logs/trades.jsonl | jq .  # Recent trades"
echo ""
echo -e "${YELLOW}  ⚠️  Before going live:${NC}"
echo "    1. Edit $ROCKY_DIR/.env with your API keys"
echo "    2. Set TRADING_MODE=live"
echo "    3. Set ROCKY_ENGINE=v2 for LLM-powered trading"
echo "    4. Fund your Polymarket wallet with USDC"
echo "    5. systemctl restart rocky"
echo ""
