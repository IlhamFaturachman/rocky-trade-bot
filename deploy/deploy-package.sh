#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# Rocky — Build deploy package (tar.gz)
# Run from the project root on your Mac.
#
# Usage:
#   ./deploy/deploy-package.sh
#
# Output:
#   /tmp/rocky-deploy.tar.gz
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
STAGING_DIR="/tmp/rocky-deploy"
OUTPUT="/tmp/rocky-deploy.tar.gz"

echo "🪨 Building Rocky deploy package..."
echo "   Source: $PROJECT_DIR"

# Clean staging
rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR"/{src,tests,searxng,deploy,skills/rocky,scripts}

# Core Python source
cp "$PROJECT_DIR"/src/*.py "$STAGING_DIR/src/"
echo "   ✓ src/ ($(ls "$PROJECT_DIR"/src/*.py | wc -l | tr -d ' ') files)"

# Tests
cp "$PROJECT_DIR"/tests/*.py "$STAGING_DIR/tests/"
echo "   ✓ tests/"

# SearXNG config
cp "$PROJECT_DIR"/searxng/settings.yml "$STAGING_DIR/searxng/"
echo "   ✓ searxng/"

# Docker compose
cp "$PROJECT_DIR/docker-compose.yml" "$STAGING_DIR/"
echo "   ✓ docker-compose.yml"

# Requirements
cp "$PROJECT_DIR/requirements.txt" "$STAGING_DIR/"
echo "   ✓ requirements.txt"

# .env.example
cp "$PROJECT_DIR/.env.example" "$STAGING_DIR/"
echo "   ✓ .env.example"

# Deploy scripts and configs
cp "$SCRIPT_DIR/deploy-vps.sh" "$STAGING_DIR/deploy/"
cp "$SCRIPT_DIR/SOUL.md" "$STAGING_DIR/deploy/"
cp "$SCRIPT_DIR/BOOT.md" "$STAGING_DIR/deploy/"
cp "$SCRIPT_DIR/openclaw-trading-config.json" "$STAGING_DIR/deploy/"
cp "$SCRIPT_DIR/README-DEPLOY.md" "$STAGING_DIR/deploy/"
echo "   ✓ deploy/ (deploy-vps.sh, SOUL.md, BOOT.md, config, README)"

# Skill
cp "$PROJECT_DIR/skills/rocky/SKILL.md" "$STAGING_DIR/skills/rocky/"
echo "   ✓ skills/rocky/SKILL.md"

# Scripts
cp "$PROJECT_DIR/scripts/setup-telegram-test.py" "$STAGING_DIR/scripts/"
echo "   ✓ scripts/setup-telegram-test.py"

# README
cp "$PROJECT_DIR/README.md" "$STAGING_DIR/" 2>/dev/null || true

# Make scripts executable
chmod +x "$STAGING_DIR/deploy/deploy-vps.sh"

# Create the archive
cd /tmp
tar czf "$OUTPUT" rocky-deploy/

# Stats
SIZE=$(du -sh "$OUTPUT" | cut -f1)
FILE_COUNT=$(find "$STAGING_DIR" -type f | wc -l | tr -d ' ')

echo ""
echo "✅ Deploy package created:"
echo "   📦 $OUTPUT ($SIZE, $FILE_COUNT files)"
echo ""
echo "   Deploy to VPS:"
echo "   scp $OUTPUT root@YOUR_VPS:/tmp/"
echo "   ssh root@YOUR_VPS 'cd /tmp && tar xzf rocky-deploy.tar.gz && cd rocky-deploy && sudo ./deploy/deploy-vps.sh'"

# Cleanup staging
rm -rf "$STAGING_DIR"
