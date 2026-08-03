#!/usr/bin/env python3
"""Rocky live paper dashboard — Opened vs Resolved tables + shadow data collection."""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("ROCKY_ROOT", Path(__file__).resolve().parents[1]))
LOGS = ROOT / "logs"
PORT = int(os.environ.get("ROCKY_DASHBOARD_PORT", "8787"))
HOST = os.environ.get("ROCKY_DASHBOARD_HOST", "0.0.0.0")


def _read_json(path: Path, default=None):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default if default is not None else {}


def _read_jsonl(path: Path, limit: int = 300):
    rows = []
    if not path.exists():
        return rows
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        return rows
    return rows[-limit:]


def build_snapshot() -> dict:
    state = _read_json(LOGS / "state.json", {})
    last = _read_json(LOGS / "last_cycle.json", {})
    trades = _read_jsonl(LOGS / "trades.jsonl", 400)
    skips = _read_jsonl(LOGS / "skips.jsonl", 150)

    # Prefer explicit _event; also accept result win/loss as resolved (legacy rows)
    opened_all = [t for t in trades if t.get("_event") == "opened" or (
        t.get("_event") is None and t.get("result") in (None, "", "pending", "open")
    )]
    resolved_all = [
        t for t in trades
        if t.get("_event") == "resolved" or t.get("result") in ("win", "loss")
    ]

    # Latest open/resolve per trade_id
    latest_open = {}
    for t in opened_all:
        tid = t.get("trade_id")
        latest_open[tid] = t
    latest_res = {}
    for t in resolved_all:
        tid = t.get("trade_id")
        latest_res[tid] = t

    # Pending = opened and not yet resolved
    now = time.time()
    pending = []
    stale_pending = []
    for tid, t in latest_open.items():
        if tid in latest_res:
            continue
        age = now - float(t.get("timestamp") or now)
        # Mark very old unresolved as stale (process died before resolve)
        t = {**t, "_age_sec": round(age, 1), "_stale": age > 900}
        if age > 900:
            stale_pending.append(t)
        pending.append(t)
    pending.sort(key=lambda x: x.get("timestamp") or 0, reverse=True)

    # Resolved list (unique by trade_id, latest)
    resolved = list(latest_res.values())
    resolved.sort(key=lambda x: x.get("resolved_at") or x.get("timestamp") or 0, reverse=True)

    def is_shadow(t):
        return bool(t.get("is_shadow")) or (t.get("flag") not in (None, "", "real") and t.get("flag") != "real" and t.get("is_shadow") is not False and t.get("flag") not in ("real",))

    # Prefer explicit is_shadow field
    def shadowish(t):
        if "is_shadow" in t:
            return bool(t.get("is_shadow"))
        return t.get("flag") not in (None, "", "real")

    real_res = [t for t in resolved if not shadowish(t)]
    shadow_res = [t for t in resolved if shadowish(t)]
    real_open_pending = [t for t in pending if not shadowish(t)]
    shadow_open_pending = [t for t in pending if shadowish(t)]

    wins = [t for t in real_res if t.get("result") == "win"]
    losses = [t for t in real_res if t.get("result") == "loss"]
    total_pnl = sum(float(t.get("pnl") or 0) for t in real_res)
    win_rate = (len(wins) / len(real_res)) if real_res else 0.0

    sh_wins = [t for t in shadow_res if t.get("result") == "win"]
    sh_losses = [t for t in shadow_res if t.get("result") == "loss"]
    sh_pnl = sum(float(t.get("pnl") or 0) for t in shadow_res)
    sh_wr = (len(sh_wins) / len(shadow_res)) if shadow_res else 0.0

    # Flag breakdown on shadow resolved
    flag_stats = {}
    for t in shadow_res:
        f = t.get("flag") or "shadow"
        flag_stats.setdefault(f, {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        flag_stats[f]["n"] += 1
        if t.get("result") == "win":
            flag_stats[f]["wins"] += 1
        elif t.get("result") == "loss":
            flag_stats[f]["losses"] += 1
        flag_stats[f]["pnl"] += float(t.get("pnl") or 0)

    skip_stats = last.get("skip_stats") or {}
    if not skip_stats and skips:
        for s in skips:
            c = s.get("code") or "other"
            skip_stats[c] = skip_stats.get(c, 0) + 1

    return {
        "ts": time.time(),
        "root": str(ROOT),
        "state": state,
        "last_cycle": last,
        "stats": {
            "balance": state.get("balance", last.get("balance")),
            "trade_count": state.get("trade_count", 0),
            "real_resolved": len(real_res),
            "real_wins": len(wins),
            "real_losses": len(losses),
            "real_win_rate": win_rate,
            "real_pnl": total_pnl,
            "real_pending": len(real_open_pending),
            "shadow_resolved": len(shadow_res),
            "shadow_wins": len(sh_wins),
            "shadow_losses": len(sh_losses),
            "shadow_win_rate": sh_wr,
            "shadow_pnl": sh_pnl,
            "shadow_pending": len(shadow_open_pending),
            "stale_pending": len(stale_pending),
            "consecutive_losses": state.get("consecutive_losses", 0),
            "skip_stats": skip_stats,
            "flag_stats": flag_stats,
        },
        # Prefer fresh pending first; include age so UI can show stuck ones
        "opened": pending[:40],
        "stale_opened": stale_pending[:20],
        "resolved": resolved[:50],
        "recent_skips": skips[-40:],
    }


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Rocky Paper Dashboard</title>
<style>
  :root { --bg:#0b0f14; --card:#121821; --muted:#8b9bb4; --text:#e7eefc; --good:#3ddc97; --bad:#ff6b6b; --accent:#6ea8fe; --warn:#ffd166; --line:#1e293b; --shadow:#a78bfa; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background:radial-gradient(1200px 600px at 10% -10%, #152033 0%, var(--bg) 50%); color:var(--text); }
  header { padding:18px 22px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; }
  h1 { margin:0; font-size:18px; letter-spacing:.3px; }
  .sub { color:var(--muted); font-size:12px; }
  .grid { display:grid; grid-template-columns: repeat(auto-fit,minmax(140px,1fr)); gap:10px; padding:14px 22px; }
  .card { background:linear-gradient(180deg, #141c27, var(--card)); border:1px solid var(--line); border-radius:14px; padding:12px 14px; box-shadow:0 10px 30px rgba(0,0,0,.25); }
  .label { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.08em; }
  .value { font-size:22px; font-weight:700; margin-top:4px; }
  .good { color:var(--good); } .bad { color:var(--bad); } .warn { color:var(--warn); } .accent { color:var(--accent); } .shadow { color:var(--shadow); }
  main { padding:0 22px 28px; display:grid; grid-template-columns: 1fr 1fr; gap:14px; }
  @media (max-width: 1100px) { main { grid-template-columns: 1fr; } }
  h2 { font-size:13px; margin:0 0 10px; color:var(--muted); text-transform:uppercase; letter-spacing:.08em; display:flex; justify-content:space-between; align-items:center; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th, td { padding:7px 5px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }
  th { color:var(--muted); font-weight:600; position:sticky; top:0; background:var(--card); }
  .scroll { max-height:420px; overflow:auto; }
  .pill { display:inline-block; padding:2px 7px; border-radius:999px; font-size:10px; border:1px solid var(--line); white-space:nowrap; }
  .pill.up { color:var(--good); border-color:#245c45; background:#10261c; }
  .pill.down { color:var(--bad); border-color:#5c2424; background:#2a1212; }
  .pill.skip { color:var(--warn); border-color:#5c4a20; background:#2a2210; }
  .pill.real { color:var(--accent); border-color:#2a4a7a; background:#101a2a; }
  .pill.ghost { color:var(--shadow); border-color:#4c3a7a; background:#1a1430; }
  .mono { font-variant-numeric: tabular-nums; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .muted { color:var(--muted); }
  .full { grid-column: 1 / -1; }
  footer { padding:8px 22px 20px; color:var(--muted); font-size:11px; }
</style>
</head>
<body>
<header>
  <div>
    <h1>🪨 Rocky Paper · Data Collection</h1>
    <div class="sub">Real trades move balance · Shadow trades are flagged skips resolved for research</div>
  </div>
  <div class="sub" id="updated">loading…</div>
</header>

<div class="grid" id="kpis"></div>

<main>
  <section class="card">
    <h2><span>Opened (pending)</span><span class="muted" id="openCount"></span></h2>
    <div class="scroll"><table>
      <thead><tr>
        <th>ID</th><th>Type</th><th>Flag</th><th>Dir</th><th>Stake</th><th>Entry</th><th>Edge</th><th>t_left</th><th>BTC</th><th>Market</th>
      </tr></thead>
      <tbody id="openedBody"></tbody>
    </table></div>
  </section>

  <section class="card">
    <h2><span>Resolved (closed)</span><span class="muted" id="resCount"></span></h2>
    <div class="scroll"><table>
      <thead><tr>
        <th>ID</th><th>Type</th><th>Flag</th><th>Dir</th><th>Stake</th><th>Entry</th><th>Edge</th><th>Result</th><th>PnL</th><th>Market</th>
      </tr></thead>
      <tbody id="resolvedBody"></tbody>
    </table></div>
  </section>

  <section class="card full">
    <h2><span>Shadow flag performance</span><span class="muted">would-be win/loss by skip reason</span></h2>
    <div class="scroll"><table>
      <thead><tr><th>Flag</th><th>N</th><th>Wins</th><th>Losses</th><th>WR</th><th>Would-be PnL</th></tr></thead>
      <tbody id="flagBody"></tbody>
    </table></div>
  </section>

  <section class="card full">
    <h2>Last cycle</h2>
    <pre id="lastCycle" class="mono muted" style="white-space:pre-wrap;margin:0;font-size:12px;"></pre>
  </section>
</main>
<footer>Auto-refresh 5s · <span id="root"></span></footer>

<script>
const $ = (id) => document.getElementById(id);
function money(n){ if(n==null||isNaN(n)) return '—'; const v=Number(n); return (v<0?'-':'')+'$'+Math.abs(v).toFixed(4); }
function pct(n){ if(n==null||isNaN(n)) return '—'; return (Number(n)*100).toFixed(0)+'%'; }
function dirPill(d){ d=(d||'').toLowerCase(); if(d==='up') return '<span class="pill up">UP</span>'; if(d==='down') return '<span class="pill down">DOWN</span>'; return '<span class="pill skip">'+(d||'—')+'</span>'; }
function typePill(t){ return t ? '<span class="pill ghost">SHADOW</span>' : '<span class="pill real">REAL</span>'; }
function flagPill(f){ f=f||'—'; const cls = f==='real' ? 'real' : 'skip'; return '<span class="pill '+cls+'">'+f+'</span>'; }
function shortM(m){ m=m||''; return m.length>48?m.slice(0,48)+'…':m; }
function isShadow(t){ if('is_shadow' in t) return !!t.is_shadow; return t.flag && t.flag!=='real'; }

function kpi(label, value, cls){
  return `<div class="card"><div class="label">${label}</div><div class="value ${cls||''}">${value}</div></div>`;
}

async function refresh(){
  try {
    const r = await fetch('/api/status?_='+Date.now());
    const d = await r.json();
    const s = d.stats || {};
    $('updated').textContent = 'updated '+new Date(d.ts*1000).toLocaleTimeString();
    $('root').textContent = d.root || '';
    $('kpis').innerHTML = [
      kpi('Balance', money(s.balance), (s.real_pnl||0)>=0?'good':'bad'),
      kpi('Real PnL', money(s.real_pnl), (s.real_pnl||0)>=0?'good':'bad'),
      kpi('Real WR', pct(s.real_win_rate), 'accent'),
      kpi('Real W/L', `${s.real_wins||0}/${s.real_losses||0}`),
      kpi('Real pending', s.real_pending||0, 'warn'),
      kpi('Shadow PnL*', money(s.shadow_pnl), 'shadow'),
      kpi('Shadow WR*', pct(s.shadow_win_rate), 'shadow'),
      kpi('Shadow W/L*', `${s.shadow_wins||0}/${s.shadow_losses||0}`, 'shadow'),
      kpi('Shadow pending', s.shadow_pending||0, 'shadow'),
      kpi('Stale pending', s.stale_pending||0, (s.stale_pending||0)>0?'bad':'good'),
      kpi('Consec losses', s.consecutive_losses||0),
    ].join('');

    const opened = d.opened || [];
    $('openCount').textContent = opened.length+' open' + ((s.stale_pending||0)>0 ? ` · ${s.stale_pending} stale` : '');
    $('openedBody').innerHTML = opened.map(t => {
      const sh = isShadow(t);
      const age = t._age_sec!=null ? (t._age_sec>3600 ? (t._age_sec/3600).toFixed(1)+'h' : Math.round(t._age_sec/60)+'m') : '—';
      const stale = t._stale ? ' <span class="pill skip">STALE</span>' : '';
      return `<tr>
        <td class="mono">#${t.trade_id??'—'}</td>
        <td>${typePill(sh)}${stale}</td>
        <td>${flagPill(t.flag|| (sh?'shadow':'real'))}</td>
        <td>${dirPill(t.direction)}</td>
        <td class="mono">${money(t.stake_usd)}</td>
        <td class="mono">${Number(t.entry_price||0).toFixed(3)}</td>
        <td class="mono">${Number(t.edge||0).toFixed(3)}</td>
        <td class="mono">${age}</td>
        <td class="mono">${t.btc_price_at_entry?Number(t.btc_price_at_entry).toFixed(2):'—'}</td>
        <td class="muted">${shortM(t.market_question)}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="10" class="muted">no open trades</td></tr>';

    const resolved = d.resolved || [];
    $('resCount').textContent = resolved.length+' shown';
    $('resolvedBody').innerHTML = resolved.map(t => {
      const sh = isShadow(t);
      const res = (t.result||'—');
      const resCls = res==='win'?'good':(res==='loss'?'bad':'');
      const pnl = Number(t.pnl||0);
      return `<tr>
        <td class="mono">#${t.trade_id??'—'}</td>
        <td>${typePill(sh)}</td>
        <td>${flagPill(t.flag|| (sh?'shadow':'real'))}</td>
        <td>${dirPill(t.direction)}</td>
        <td class="mono">${money(t.stake_usd)}</td>
        <td class="mono">${Number(t.entry_price||0).toFixed(3)}</td>
        <td class="mono">${Number(t.edge||0).toFixed(3)}</td>
        <td class="${resCls}">${res}</td>
        <td class="mono ${pnl>=0?'good':'bad'}">${money(pnl)}</td>
        <td class="muted">${shortM(t.market_question)}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="10" class="muted">no resolved trades</td></tr>';

    const flags = s.flag_stats || {};
    const rows = Object.entries(flags).sort((a,b)=>b[1].n-a[1].n);
    $('flagBody').innerHTML = rows.map(([f,v]) => {
      const wr = v.n ? (v.wins/v.n) : 0;
      return `<tr>
        <td>${flagPill(f)}</td>
        <td class="mono">${v.n}</td>
        <td class="mono good">${v.wins}</td>
        <td class="mono bad">${v.losses}</td>
        <td class="mono">${pct(wr)}</td>
        <td class="mono ${v.pnl>=0?'good':'bad'}">${money(v.pnl)}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="6" class="muted">no shadow resolves yet — wait ~5m after skips</td></tr>';

    $('lastCycle').textContent = JSON.stringify(d.last_cycle||{}, null, 2);
  } catch(e) {
    $('updated').textContent = 'error: '+e;
  }
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        return

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, HTML.encode(), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            body = json.dumps(build_snapshot(), default=str).encode()
            self._send(200, body, "application/json")
            return
        if path == "/api/trades":
            body = json.dumps(_read_jsonl(LOGS / "trades.jsonl", 400), default=str).encode()
            self._send(200, body, "application/json")
            return
        if path == "/api/skips":
            body = json.dumps(_read_jsonl(LOGS / "skips.jsonl", 200), default=str).encode()
            self._send(200, body, "application/json")
            return
        if path == "/api/log":
            log_path = LOGS / "rocky.log"
            text = ""
            if log_path.exists():
                try:
                    lines = log_path.read_text(errors="replace").splitlines()
                    text = "\n".join(lines[-120:])
                except Exception as e:
                    text = f"log read error: {e}"
            self._send(200, text.encode(), "text/plain; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Rocky dashboard on http://{args.host}:{args.port}  root={ROOT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
