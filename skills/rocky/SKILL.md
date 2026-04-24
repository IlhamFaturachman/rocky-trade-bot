# Rocky Trading Skill

> Autonomous BTC prediction market trading on Polymarket.

## Metadata

- **Name:** rocky-trader
- **Version:** 0.2.0
- **Author:** Liam
- **Tags:** trading, polymarket, btc, crypto, autonomous

## Description

Rocky is an autonomous BTC prediction market trader for Polymarket. It scans for BTC Up/Down 5-minute markets, gathers real-time intelligence (price, momentum, news), and makes trade decisions using either a rule-based engine (v1) or Opus 4.6 LLM reasoning (v2).

## Commands

### /trade — Run one trading cycle

Execute a single trading cycle manually (scan → analyze → decide → execute).

```bash
cd /opt/rocky && ./venv/bin/python3 -c "
from src.config import Config, TradingMode
from src.scanner import MarketScanner
from src.intelligence import IntelligenceEngine
from src.decision_v2 import DecisionEngineV2
from src.executor import ExecutionEngine
from src.notifier import TelegramNotifier
import os, json

config = Config()
scanner = MarketScanner(config)
intel = IntelligenceEngine(config)
engine = DecisionEngineV2(config) if os.environ.get('ROCKY_ENGINE','v1') == 'v2' else __import__('src.decision', fromlist=['DecisionEngine']).DecisionEngine(config)
executor = ExecutionEngine(config)
notifier = TelegramNotifier()

snapshot = intel.get_snapshot()
snapshot = intel.enrich_with_news(snapshot)
markets = scanner.fetch_btc_markets()
if not markets:
    print('No active markets found')
else:
    market = scanner.get_best_market(markets)
    orderbook = scanner.fetch_market_orderbook(market.tokens[0]['token_id']) if market.tokens else {}
    signal = engine.analyze(market, snapshot, orderbook=orderbook) if hasattr(engine, 'analyze') and 'orderbook' in engine.analyze.__code__.co_varnames else engine.analyze(market, snapshot)
    if signal and signal.should_trade:
        record = executor.execute(signal)
        if record:
            notifier.send_trade_opened(record)
            print(json.dumps({'trade_id': record.trade_id, 'direction': record.direction, 'confidence': record.confidence, 'stake': record.stake_usd}, indent=2))
    elif signal:
        print(f'Signal: {signal.direction} @ {signal.confidence:.0%} — below threshold, skipping')
    else:
        print('No signal generated')
"
```

### /stats — Show trading statistics

```bash
cd /opt/rocky && ./venv/bin/python3 -c "
from src.config import Config
from src.executor import ExecutionEngine
import json
config = Config()
executor = ExecutionEngine(config)
stats = executor.get_stats()
print(json.dumps(stats, indent=2))
"
```

### /balance — Show current balance

```bash
cd /opt/rocky && cat logs/state.json | python3 -m json.tool
```

### /journal [N] — Show recent trades

Show the last N trades (default 5):

```bash
cd /opt/rocky && tail -${1:-5} logs/trades.jsonl | python3 -m json.tool
```

### /pause — Pause trading

```bash
sudo systemctl stop rocky
echo "Trading paused"
```

### /resume — Resume trading

```bash
sudo systemctl start rocky
echo "Trading resumed"
```

### /engine v1|v2 — Switch decision engine

```bash
# Edit the environment and restart
sudo sed -i "s/ROCKY_ENGINE=.*/ROCKY_ENGINE=$1/" /opt/rocky/.env
sudo systemctl restart rocky
echo "Switched to engine $1"
```

### /market add <series_slug> — Add market series

Add a new Polymarket series to scan (e.g., `btc-up-or-down-15m`):

```bash
# This modifies the BTC_SERIES list in scanner.py
echo "Adding series: $1"
cd /opt/rocky && ./venv/bin/python3 -c "
import json
# Series are configured in src/scanner.py BTC_SERIES list
# For runtime override, set ROCKY_EXTRA_SERIES env var
import os
current = os.environ.get('ROCKY_EXTRA_SERIES', '')
series = [s for s in current.split(',') if s] + ['$1']
print(f'ROCKY_EXTRA_SERIES={\",\".join(series)}')
print('Add this to /opt/rocky/.env and restart')
"
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TRADING_MODE` | No | `paper` | `paper` or `live` |
| `ROCKY_ENGINE` | No | `v1` | `v1` (rules) or `v2` (LLM) |
| `LLM_API_KEY` | For v2 | — | enowX Labs API key |
| `LLM_MODEL` | No | `enowxlabs/claude-opus-4.6` | LLM model |
| `LLM_API_URL` | No | `https://api.enowxlabs.com/v1/chat/completions` | LLM endpoint |
| `POLY_PRIVATE_KEY` | For live | — | Polygon wallet private key |
| `TELEGRAM_BOT_TOKEN` | No | — | Telegram bot token |
| `TELEGRAM_CHAT_ID` | No | — | Telegram chat ID |
| `SEARXNG_URL` | No | `http://127.0.0.1:8888` | SearXNG instance |
| `ROCKY_TIMEZONE_OFFSET` | No | `0` | UTC offset in hours |

## Files

| Path | Description |
|------|-------------|
| `/opt/rocky/src/main.py` | Main trading loop |
| `/opt/rocky/src/scanner.py` | Polymarket market scanner |
| `/opt/rocky/src/intelligence.py` | BTC price & news intelligence |
| `/opt/rocky/src/decision.py` | V1 rule-based decision engine |
| `/opt/rocky/src/decision_v2.py` | V2 LLM decision engine |
| `/opt/rocky/src/executor.py` | Trade execution & journal |
| `/opt/rocky/src/notifier.py` | Telegram notifications |
| `/opt/rocky/logs/state.json` | Trading state (balance, counts) |
| `/opt/rocky/logs/trades.jsonl` | Trade journal (JSONL) |
| `/opt/rocky/logs/rocky.log` | Application log |
