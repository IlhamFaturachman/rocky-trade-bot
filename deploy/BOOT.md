# BOOT.md — Rocky Startup Procedure

_Run this checklist every time you wake up._

## 1. Load Trading State

```bash
# Check state file
cat /opt/rocky/logs/state.json
```

Report: current balance, trade count, consecutive losses, daily starting balance.

## 2. Check API Connectivity

Verify all services are reachable:

- **Binance API:** `curl -s https://api.binance.com/api/v3/ping`
- **Polymarket Gamma:** `curl -s https://gamma-api.polymarket.com/markets?limit=1`
- **SearXNG:** `curl -s http://127.0.0.1:8888/healthz`
- **LLM API:** Confirm LLM_API_KEY is set in environment

Report any failures.

## 3. Check Pending Trades

```bash
# Check for unresolved trades in journal
tail -20 /opt/rocky/logs/trades.jsonl | grep -v '"_event": "resolved"'
```

If there are pending trades older than 5 minutes, resolve them.

## 4. Report Status via Telegram

Send a startup message with:
- Engine version (v1/v2)
- Trading mode (paper/live)
- Current balance
- Any pending trades
- API connectivity status

## 5. Start Trading Loop

The trading loop runs automatically via systemd/cron. Verify it's active:

```bash
systemctl status rocky
```

If not running, start it:

```bash
sudo systemctl start rocky
```

## 6. Review Recent Performance

If there are trades from the last session:
- Check win rate
- Note any patterns in losses
- Update memory with lessons learned

---

_You're online. Time to trade. 🪨_
