# Rocky LLM Trade Experiment — Win Rate Validation

**Date:** 2026-08-04
**Goal:** Validate that Rocky with new LLM brain (Zendy DeepSeek V4 Flash, thinking disabled) achieves ~80% win rate on 10 real trades, cross-checked against Polymarket gamma API.

## Background / Evidence

Data from `logs/archive-2026-08-03/` (7-day collection, Jul 19-22):
- **88 real trades resolved**: 77 wins / 11 loss = **87.5% WR**, PnL +$4403.71
- Real trade edge distribution: all in **0.08–0.30** (edge rule works)
- Edge buckets (shadow): edge 0.00–0.02 = 50.9% WR (random), edge 0.05–0.08 = 91.3%, edge 0.08+ = 87.5%
- `model_skip` flag shadow WR 64.9% (LLM skip = tape tends to win → LLM skip is a valid signal, but wasted opportunity)
- Academic research: BTC 5-min ≈ random walk; LLM ~50% blind. Edge comes from **edge gate + tape lag**, not raw prediction.

## Key Insight

Lever is **NOT** lowering edge threshold (edge < 0.05 = random zone, WR ~50%). Lever is:
1. Keep edge rule **strict** (min_edge 0.05, target edge >= 0.08 where data shows 87-91% WR).
2. Modify SYSTEM_PROMPT so LLM **gives UP/DOWN direction** (not skip) whenever there is a directional lean, computing edge honestly. "Default skip" → "pick the slight-lean direction, confidence 55-62, edge as computed."
3. Loosen only confidence + window (not edge): min_confidence 0.68→0.60, wider time window.

## Design

### Phase A — Config + SYSTEM_PROMPT (lever)

**`scripts/start_paper_stack.sh` updates dict:**
- `ROCKY_MIN_CONFIDENCE`: 0.68 → **0.60**
- `ROCKY_MIN_EDGE`: 0.05 → **0.05** (unchanged, NOT lowered)
- `ROCKY_MAX_ENTRY`: 0.70 → **0.78**
- `ROCKY_MIN_ENTRY`: 0.28 → **0.22**
- `ROCKY_MIN_T_LEFT`: 30 → **20**
- `ROCKY_MAX_T_LEFT`: 180 → **280**
- `LLM_MAX_TOKENS`: 1024 → **350**
- `LLM_MODEL`: `zendy/deepseek` (unchanged)
- `thinking: {type: disabled}` already in payload (decision_v2.py).

**`src/decision_v2.py` SYSTEM_PROMPT changes:**
- Remove "Otherwise SKIP" default; replace with: "If directional lean exists (even small), output UP/DOWN with confidence 55–62 and edge = your_p - market_price. Only SKIP when truly no directional information (flat, equal odds, zero displacement)."
- Keep edge rule as **soft preference**: "Prefer trades with edge >= 0.06; if edge < 0.06 still output direction but confidence ≤ 62."
- Add: "Never default to skip — a slight lean is tradeable."

### Phase B — Polymarket gamma verification (automated)

**New script `scripts/verify_trades.py`:**
- Reads `logs/trades.jsonl`, finds trades with `result` set but not yet gamma-verified.
- For each: query `https://gamma-api.polymarket.com/markets/{condition_id}` (public, no auth).
- Extract official Polymarket `outcome` (UP/DOWN winner).
- Poll every 30s; if gamma not resolved within ~3 min after window close, fall back to Binance candle (Rocky's existing logic) and flag `pending`.
- Write `logs/verify.csv`: `trade_id, condition_id, rocky_result, poly_outcome, match, candle_open, candle_close, ts`.
- Print summary: verified X/Y, rocky==poly Z, poly win_rate N%.

### Phase C — Backtest (already done)

`scripts/analyze_collection.py` run on archive data (Jul 19-22). Results captured above. No new harness needed — re-run after experiment to compare new-config vs old-config buckets if desired.

## Execution Order

**A + B parallel** (C done):
1. Edit `start_paper_stack.sh` + `decision_v2.py` (SYSTEM_PROMPT) locally.
2. Commit + push + pull VPS.
3. Restart stack → LLM should now give UP/DOWN per cycle.
4. Launch `verify_trades.py` in background (nohup) on VPS.
5. Monitor until 10 real trades resolved (~10-30 min at 1 trade/cycle).
6. Cross-check gamma vs Rocky, compute win rate, report honestly.

## Success Criteria

- 10 real trades from LLM decisions (UP/DOWN, not skip).
- Win rate >= 8/10 (80%) cross-verified against Polymarket gamma.
- If < 8/10: report honestly, analyze which flags/edges lost, iterate.

## Honest Limits

- 10 trades = small sample. 87.5% WR has 95% CI ~79-93% on n=88; on n=10 even wider.
- Polymarket resolves via Chainlink oracle (aggregated), Rocky uses Binance candle proxy — small divergence possible.
- Academic consensus: 5-min BTC ≈ random walk; 80%+ WR requires edge-gate selectivity, not raw LLM prediction.
- LLM (Zendy DeepSeek, thinking disabled) is new — its directional accuracy unproven at scale.
