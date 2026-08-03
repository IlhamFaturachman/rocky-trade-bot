# Rocky 7-Day Isolated Data Collection

## Goal
Collect **maximal labeled data** (real paper + shadow counterfactuals) for 7 days **before live**.
After day 7, run analysis and only then decide live gates.

## Is current setup "already perfect"?
**No.** Perfect live profitability is not guaranteed by any public bot writeup.
What we *can* maximize now is **coverage of decision-relevant features** so day-8 choices are evidence-based.

## What profitable bots claim (public, take with salt)
Common themes from Polymarket 5m BTC writeups/repos:
1. **Latency / fair-value lag** (Binance tape vs Polymarket book)
2. **Late-window confirmation** (not t+10s blind entries)
3. **Edge vs ask + fees** (not mid fantasy)
4. **Regime filters** (skip chop)
5. **Maker vs taker** microstructure (harder; needs WS + limit orders)

We implement research-grade logging for (1)–(4). True sub-second latency arb needs websockets + colocated infra (phase 2).

## What we log now (maximal collection)
| File | Content |
|---|---|
| `logs/cycles.jsonl` | Every cycle features: phase, PTB bps, momentum, book bid/ask/spread, fair_p_up_tape, edges, signal, edge gates, llm_failed |
| `logs/trades.jsonl` | Real + **shadow** opens/resolves with `flag`, `is_shadow`, edge, t_left, entry ask |
| `logs/skips.jsonl` | Skip codes + reasons |
| `logs/state.json` | Real balance only |
| `logs/last_cycle.json` | Dashboard snapshot |
| `logs/analysis_summary.json` | From analyzer |

### Shadow flags (counterfactual labels)
- `model_skip` — model refused; tape direction still simulated
- `chase` / `chop` / `early` / `late` / `no_edge` / `flip` / `risk` — edge pack blocks
- `dual_*` — opposite direction counterfactual
- `late_window` — late-phase specialist sample
- `real` — balance-affecting paper trade

## 7-day run (isolated)
```bash
cd /home/farm/rocky-trade-bot
bash scripts/start_7d_collection.sh
# watchdog keeps paper+dashboard alive
# daily report → Telegram @gracerockyy_bot
```

Dashboard: `http://103.150.61.32:8787/`

## After 7 days
```bash
./.venv/bin/python3 scripts/analyze_collection.py --days 7
```
Live only if:
- shadow or real has **enough samples** (hundreds+)
- **WR and PnL positive** in chosen flag/phase/edge buckets
- fee/slip assumptions still hold

## Honest limits
- No websocket latency race yet
- No historical backtest of full Polymarket books
- LLM is assistive; hard gates + labels matter more
- 7 days may still be thin in low-vol regimes — extend if needed
