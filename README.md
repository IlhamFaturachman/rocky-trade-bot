# 🪨 Rocky — PolyClaw Trader

Autonomous BTC prediction market trading agent for Polymarket.

## Quick Start

```bash
# On your Debian 12 VPS:
git clone <repo> /tmp/rocky
cd /tmp/rocky
cp .env.example .env
# Edit .env with your settings
sudo ./deploy.sh
```

## Architecture

```
rocky/
├── src/
│   ├── main.py           # Autonomous 5-min trading loop
│   ├── config.py          # Configuration & risk parameters
│   ├── scanner.py         # Polymarket BTC market scanner
│   ├── intelligence.py    # BTC price, momentum, news, sentiment
│   ├── decision.py        # Confidence scoring & trade signals
│   └── executor.py        # Paper & live trade execution + journal
├── tests/
│   └── test_core.py       # Core logic tests
├── searxng/
│   └── settings.yml       # SearXNG config for news search
├── logs/                   # Runtime: trades.jsonl, state.json, rocky.log
├── deploy.sh              # One-command VPS deployment
├── docker-compose.yml     # SearXNG container
├── rocky.service          # Systemd unit file
├── requirements.txt       # Python dependencies
├── .env.example           # Environment template
└── README.md
```

## Trading Loop (every 5 minutes)

1. **Resolve** — Check pending trades against BTC price movement
2. **Intelligence** — Fetch BTC price, klines, momentum, volatility from Binance
3. **News** — Search SearXNG for BTC headlines (every 3rd cycle)
4. **Scan** — Find active BTC 5-min Up/Down markets on Polymarket
5. **Decide** — Score confidence (0-100%) from all signals
6. **Execute** — Place trade if confidence ≥ 65%, respecting risk limits

## Risk Management

| Rule | Value |
|------|-------|
| Max risk per trade | 20% of balance |
| Min confidence to trade | 65% |
| Position size (65-74%) | 10% of balance |
| Position size (75-84%) | 15% of balance |
| Position size (85%+) | 20% of balance |
| Max consecutive losses | 3 (then pause) |
| Daily loss limit | 40% drawdown |

## Commands

```bash
systemctl status rocky        # Check status
journalctl -u rocky -f        # Live logs
systemctl restart rocky       # Restart
systemctl stop rocky          # Stop

# Manual run (paper mode):
cd /opt/rocky
./venv/bin/python3 src/main.py --mode paper --interval 60

# View trade journal:
cat /opt/rocky/logs/trades.jsonl | jq .
```

## Going Live

1. Edit `/opt/rocky/.env`:
   ```
   TRADING_MODE=live
   POLY_PRIVATE_KEY=0x...
   ```
2. Derive CLOB API credentials using py-clob-client
3. Deposit USDC to your Polymarket account
4. `sudo systemctl restart rocky`
