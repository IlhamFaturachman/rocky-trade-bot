# Rocky Trade Bot — Audit (2026-07-19)

## Verdict (honest)

**Not "literally profitable banget" out of the box.**  
BTC 5-minute Up/Down on Polymarket is a **near-efficient, high-noise** market. Public writeups (latency/fair-value bots) report that **fees + slippage often erase small edges**. Claims of 90%+ win rates are marketing until you have **your own multi-day paper journal**.

Rocky is a solid **paper-trading scaffold**. After this audit pass it is **safer and more correct**, but **edge is not proven**.

---

## Critical bugs found (fixed)

| Issue | Impact | Fix |
|---|---|---|
| Gamma `clobTokenIds` / `outcomes` returned as **JSON strings** | tokens empty / wrong | `_parse_json_field` |
| Series list sorted ascending → **Dec 2025** windows | trading dead markets | live slug `btc-updown-5m-{unix}` |
| `outcomePrices` often null | always 0.50/0.50 | CLOB mid-price enrich |
| v1 always returned up/down (never skip) | forced coin-flip trades | skip on weak combined score |
| Risk 20% / 40% daily | account blow-up risk | 10% max / 25% daily / higher min conf |
| LLM timeout 30s / no retry | silent no-trade | 90s + 3× retry on 502 |
| v2 fail = no trade | dead cycles | fallback to v1 |
| `price_to_beat` often 0 | wrong paper resolution | Binance 5m open fill |

---

## Architecture review

### What is good
- Clean modules: scanner / intel / decision / executor
- Paper journal + state persistence
- Risk gates: min conf, consecutive losses, daily DD
- v2 keeps **position sizing in code** (not LLM) — correct
- Resolution logic matches Polymarket: close ≥ open → UP

### What is weak for profitability
1. **No latency edge** — bots that win on 5m often race Binance→Polymarket (~seconds). Rocky loops every 60–300s.
2. **No fee/slippage model** — paper assumes fill at mid; live will be worse.
3. **News keyword sentiment** is toy-grade; 5m moves rarely driven by headlines.
4. **v1 confidence is calibrated to look high** (base 50% + stacked boosts) without historical Brier score.
5. **Live executor is TODO** (`_execute_live` returns None).
6. **No backtest harness** on historical 5m windows.
7. **LLM (Grok) is not a proven 5m alpha source** — useful as veto/reasoner, not oracle.

---

## Prompt (v2) assessment

**Before:** generic “be confident 65+”, mixed momentum + mean-reversion without priority → model overtrades.

**After (this pass):**
- Explicit EV / edge vs ask price
- Hard skip bands (price ≥0.72, ≤0.28, t_left rules)
- Prefer SKIP
- Confidence must map to probability, not vibes
- JSON-only

Still not magic: Grok free path can **502** on long prompts; we retry + fall back to v1.

---

## Risk defaults (new)

| Param | Old | New |
|---|---|---|
| max risk / trade | 20% | **10%** |
| min confidence | 65% | **68%** |
| size tiers | 10/15/20% | **5/8/10%** |
| daily loss limit | 40% | **25%** |

---

## What would make it *more* profitable (roadmap)

1. **Latency sniper**: websocket Binance + Polymarket book; trade only when fair value − ask > fee+slip buffer.
2. **Only trade late window** (last 60–120s) when direction is already mostly decided and book is mispriced.
3. **Never buy favorites >0.70** unless edge ≥ 8–10¢ after fees.
4. **Backtest** 2–4 weeks of 5m candles + historical books (if available).
5. **Kill-switch** on rolling 50-trade expectancy ≤ 0.
6. Wire **real CLOB fills** before any live money.

---

## Runtime on this VPS

- Path: `/home/farm/rocky-trade-bot`
- LLM: Liam g2a `http://127.0.0.1:8080/v1` model `grok-4.5`
- Mode: **paper**
- Engine: **v2 with v1 fallback**
- Logs: `logs/rocky.log`, `logs/trades.jsonl`, `logs/state.json`

---

## Bottom line

| Question | Answer |
|---|---|
| Best practice? | **Closer now** (scanner/risk/prompt). Not institutional-grade. |
| Profitable banget? | **Unproven. Assume no until paper stats say otherwise.** |
| Safe to paper? | **Yes.** |
| Safe to live? | **No** until live executor + multi-day positive expectancy. |
