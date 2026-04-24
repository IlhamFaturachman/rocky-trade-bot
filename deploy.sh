#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# Rocky PolyClaw Trader — VPS Deployment Script
# Target: Debian 12 (2GB RAM, 2 CPU cores)
# Usage:  chmod +x deploy.sh && sudo ./deploy.sh
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

ROCKY_DIR="/opt/rocky"
ROCKY_USER="rocky"
ROCKY_GROUP="rocky"

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

# ── Pre-flight checks ────────────────────────────────────────────────────────

if [[ $EUID -ne 0 ]]; then
    err "Run as root: sudo ./deploy.sh"
fi

if ! grep -qi "debian" /etc/os-release 2>/dev/null; then
    warn "Not Debian — script may need adjustments"
fi

step "1/7 — System packages"

apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    docker.io docker-compose \
    curl wget git jq \
    ca-certificates gnupg lsb-release \
    > /dev/null 2>&1

log "System packages installed"

# Enable and start Docker
systemctl enable --now docker
log "Docker running"

# ── Create rocky user ─────────────────────────────────────────────────────────

step "2/7 — Rocky user & directories"

if ! id "$ROCKY_USER" &>/dev/null; then
    useradd --system --shell /usr/sbin/nologin --home-dir "$ROCKY_DIR" "$ROCKY_USER"
    log "Created user: $ROCKY_USER"
else
    log "User $ROCKY_USER already exists"
fi

# Add rocky to docker group so SearXNG compose works
usermod -aG docker "$ROCKY_USER" 2>/dev/null || true

mkdir -p "$ROCKY_DIR"/{logs,searxng,src,config,tests}
log "Directories created"

# ── Copy project files ────────────────────────────────────────────────────────

step "3/7 — Deploying Rocky source code"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Copy all source files
cp -r "$SCRIPT_DIR"/src/* "$ROCKY_DIR/src/"
cp -r "$SCRIPT_DIR"/tests/* "$ROCKY_DIR/tests/"
cp "$SCRIPT_DIR/requirements.txt" "$ROCKY_DIR/"
cp "$SCRIPT_DIR/docker-compose.yml" "$ROCKY_DIR/"
cp -r "$SCRIPT_DIR/searxng/"* "$ROCKY_DIR/searxng/"

# Copy .env if it exists, otherwise copy example
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    cp "$SCRIPT_DIR/.env" "$ROCKY_DIR/.env"
    log "Copied .env"
elif [[ ! -f "$ROCKY_DIR/.env" ]]; then
    cp "$SCRIPT_DIR/.env.example" "$ROCKY_DIR/.env"
    warn "No .env found — copied .env.example. Edit /opt/rocky/.env before going live!"
fi

log "Source code deployed to $ROCKY_DIR"

# ── Python virtual environment ────────────────────────────────────────────────

step "4/7 — Python virtual environment"

if [[ ! -d "$ROCKY_DIR/venv" ]]; then
    python3 -m venv "$ROCKY_DIR/venv"
    log "Created venv"
else
    log "Venv already exists"
fi

"$ROCKY_DIR/venv/bin/pip" install --upgrade pip -q
"$ROCKY_DIR/venv/bin/pip" install -r "$ROCKY_DIR/requirements.txt" -q
log "Python dependencies installed"

# Verify
"$ROCKY_DIR/venv/bin/python3" -c "import requests; print('  requests:', requests.__version__)"

# ── SearXNG ───────────────────────────────────────────────────────────────────

step "5/7 — SearXNG (local search engine)"

# Generate a random secret key for SearXNG
SEARX_SECRET=$(openssl rand -hex 32)
sed -i "s/rocky-searxng-change-me-in-production/$SEARX_SECRET/" "$ROCKY_DIR/searxng/settings.yml" 2>/dev/null || true

cd "$ROCKY_DIR"
docker-compose up -d
log "SearXNG container started"

# Wait for it to be ready
echo -n "  Waiting for SearXNG..."
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:8888/healthz > /dev/null 2>&1 || \
       curl -sf http://127.0.0.1:8888/ > /dev/null 2>&1; then
        echo " ready!"
        break
    fi
    echo -n "."
    sleep 2
done
log "SearXNG available at http://127.0.0.1:8888"

# ── Run tests ─────────────────────────────────────────────────────────────────

step "6/7 — Running tests"

cd "$ROCKY_DIR"
"$ROCKY_DIR/venv/bin/python3" tests/test_core.py
log "All tests passed"

# ── Systemd service ───────────────────────────────────────────────────────────

step "7/7 — Systemd service"

cp "$SCRIPT_DIR/rocky.service" /etc/systemd/system/rocky.service
systemctl daemon-reload
systemctl enable rocky.service
log "Service installed and enabled"

# Fix ownership
chown -R "$ROCKY_USER:$ROCKY_GROUP" "$ROCKY_DIR"
# .env needs to be readable by the service
chmod 600 "$ROCKY_DIR/.env"

# Start the service
systemctl start rocky.service
sleep 2

if systemctl is-active --quiet rocky.service; then
    log "Rocky is running! 🪨"
else
    warn "Service may not have started. Check: journalctl -u rocky -f"
fi

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  🪨 Rocky PolyClaw Trader — Deployed!${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Install dir:    $ROCKY_DIR"
echo "  Config:         $ROCKY_DIR/.env"
echo "  Logs:           $ROCKY_DIR/logs/"
echo "  Trade journal:  $ROCKY_DIR/logs/trades.jsonl"
echo "  SearXNG:        http://127.0.0.1:8888"
echo ""
echo "  Commands:"
echo "    systemctl status rocky        # Check status"
echo "    journalctl -u rocky -f        # Live logs"
echo "    systemctl restart rocky       # Restart"
echo "    systemctl stop rocky          # Stop"
echo ""
echo "  Paper trading is active by default."
echo "  To go live, edit $ROCKY_DIR/.env:"
echo "    TRADING_MODE=live"
echo "    POLY_PRIVATE_KEY=0x..."
echo "  Then: systemctl restart rocky"
echo ""
echo -e "${YELLOW}  ⚠️  Remember to edit .env with your wallet key before going live!${NC}"
echo ""
