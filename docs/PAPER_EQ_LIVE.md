# Paper ≈ Live execution (env-only mode switch)

Rocky uses **one fill pipeline** for paper, live_dry, live, and shadow.

## What is unified

1. Fetch **side-specific CLOB order book** (token for UP/DOWN).
2. **Walk asks** (best → worse) for the USD stake → **VWAP**.
3. Reject if:
   - book too thin (`ROCKY_MIN_FILL_PCT`, default full fill / FOK-like)
   - VWAP − best_ask > `ROCKY_SLIP_BPS`
   - effective entry > `ROCKY_MAX_ENTRY` (after optional fee bump)
4. Journal fields: `expected_ask`, `best_ask`, `vwap`, `slip_vs_best`, `filled_shares`, `fill_reason`, `order_id`, `order_status`.
5. Shadow uses the **same walk**; if book missing, falls back to quoted ask so collection continues.

## Mode switch (`.env` only)

```bash
# Default safe
TRADING_MODE=paper
ROCKY_LIVE_ENABLED=false
ROCKY_LIVE_DRY_RUN=true
```

| Goal | Env |
|---|---|
| Paper (realism sim, balance paper) | `TRADING_MODE=paper` |
| Live dry (same sim, mode label `live_dry`, **no CLOB post**) | `TRADING_MODE=live` + `ROCKY_LIVE_ENABLED=false` **or** `ROCKY_LIVE_DRY_RUN=true` |
| **Real money** | `TRADING_MODE=live` + `ROCKY_LIVE_ENABLED=true` + `ROCKY_LIVE_DRY_RUN=false` + `POLY_PRIVATE_KEY` (+ optional API creds) |

Watchdog reads `TRADING_MODE` / `ROCKY_ENGINE` / interval / balance from `.env` — no code change to flip mode.

## Fill knobs

| Env | Default | Meaning |
|---|---|---|
| `ROCKY_FEE_BPS` | 150 | Fee buffer (also Edge Pack EV) |
| `ROCKY_SLIP_BPS` | 50 | Max VWAP − best_ask before reject |
| `ROCKY_APPLY_FEE_TO_ENTRY` | true | Journal entry = VWAP × (1 + fee_bps/1e4) |
| `ROCKY_MIN_FILL_PCT` | 1.0 | 1.0 = full fill required |
| `ROCKY_MAX_ENTRY` / `MIN_ENTRY` | 0.70 / 0.28 | Hard price band |
| `ROCKY_ORDER_TYPE` | FOK | Live post type |

## Still not “guaranteed profitable”

- Paper/live_dry still **simulate** fills from a snapshot book (no in-flight adverse selection).
- Real live adds latency, partials, rejects, gas, wallet balance.
- Keep collecting; only enable live after micro dry + real fill journal looks sane.

## Restart after env change

```bash
pkill -f 'rocky-trade-bot/.venv/bin/python3 src/main.py' || true
# watchdog --loop will respawn with new .env
```
