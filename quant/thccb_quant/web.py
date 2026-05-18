"""thccb-quant WebUI — 跑在 trader 进程里的轻量监控页面。

设计：FastAPI + uvicorn 在同一个 asyncio loop。读 RUNTIME 单例拿活体策略
状态 + SSE subscriber 健康度；DB 读历史成交 / 下单 / 决策。

端口由 config.yaml `web.port` 配（默认 8765），绑 127.0.0.1（生产侧
建议 ssh -L 转发；不裸暴露公网）。失败不阻塞 trader 启动。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from thccb_quant.runtime import RUNTIME


_INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>thccb-quant monitor</title>
<style>
  :root {
    --bg:#0f1115; --fg:#e5e7eb; --muted:#9ca3af; --accent:#22d3ee;
    --red:#ef4444; --green:#22c55e; --border:#1f2937; --card:#1a1d24;
  }
  *{box-sizing:border-box;font-family:'JetBrains Mono','Menlo',monospace;}
  body{margin:0;background:var(--bg);color:var(--fg);padding:16px;}
  h1{font-size:18px;margin:0 0 12px 0;letter-spacing:.5px;}
  h2{font-size:14px;margin:18px 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;}
  .card{background:var(--card);border:1px solid var(--border);padding:12px;border-radius:0;}
  .kv{display:flex;justify-content:space-between;font-size:12px;padding:2px 0;}
  .kv .k{color:var(--muted);}
  .kv .v{font-weight:600;}
  .v.ok{color:var(--green);}
  .v.warn{color:#fbbf24;}
  .v.err{color:var(--red);}
  table{width:100%;border-collapse:collapse;font-size:11px;}
  th,td{padding:4px 6px;text-align:left;border-bottom:1px solid var(--border);}
  th{color:var(--muted);font-weight:500;text-transform:uppercase;letter-spacing:.5px;}
  .num{text-align:right;font-variant-numeric:tabular-nums;}
  .side-buy{color:var(--green);}
  .side-sell{color:var(--red);}
  .pill{display:inline-block;padding:2px 6px;font-size:10px;border:1px solid var(--border);border-radius:0;margin-right:4px;}
  .pill.ok{border-color:var(--green);color:var(--green);}
  .pill.warn{border-color:#fbbf24;color:#fbbf24;}
  .pill.err{border-color:var(--red);color:var(--red);}
  .signal-bar{height:6px;background:var(--border);position:relative;margin-top:4px;}
  .signal-bar .marker{position:absolute;top:0;height:100%;width:2px;background:var(--accent);}
  .signal-bar .band{position:absolute;top:0;height:100%;background:rgba(34,211,238,.15);}
  .small{font-size:10px;color:var(--muted);}
  .err-row{color:var(--red);}
  .footer{margin-top:18px;font-size:10px;color:var(--muted);}
</style>
</head>
<body>
<h1>thccb-quant monitor <span id="dryrun-pill"></span> <span class="small" id="uptime"></span></h1>

<div class="grid">
  <div class="card">
    <h2>system</h2>
    <div class="kv"><span class="k">base_url</span><span class="v" id="base-url">—</span></div>
    <div class="kv"><span class="k">uptime</span><span class="v" id="uptime-2">—</span></div>
    <div class="kv"><span class="k">strategies enabled</span><span class="v" id="strat-count">—</span></div>
  </div>
  <div class="card">
    <h2>SSE pipeline</h2>
    <div class="kv"><span class="k">status</span><span class="v" id="sse-status">—</span></div>
    <div class="kv"><span class="k">idle</span><span class="v" id="sse-idle">—</span></div>
    <div class="kv"><span class="k">subscribed markets</span><span class="v" id="sse-markets">—</span></div>
    <div class="kv"><span class="k">dedup skipped</span><span class="v" id="sse-dedup">—</span></div>
  </div>
</div>

<h2>strategies</h2>
<div class="grid" id="strategies"></div>

<h2>recent observed trades</h2>
<div class="card">
  <table id="trades-table">
    <thead><tr>
      <th>ts</th><th>mkt</th><th>outcome</th><th>side</th>
      <th class="num">shares</th><th class="num">price</th><th>user</th>
    </tr></thead>
    <tbody></tbody>
  </table>
</div>

<h2>recent strategy orders</h2>
<div class="card">
  <table id="orders-table">
    <thead><tr>
      <th>ts</th><th>strategy</th><th>outcome</th><th>side</th>
      <th class="num">shares</th><th class="num">price</th><th class="num">cost</th><th>status</th>
    </tr></thead>
    <tbody></tbody>
  </table>
</div>

<h2>recent decisions (skip + 触发)</h2>
<div class="card">
  <table id="decisions-table">
    <thead><tr>
      <th>ts</th><th>strategy</th><th>action</th><th>outcome</th><th>reason</th>
    </tr></thead>
    <tbody></tbody>
  </table>
</div>

<div class="footer">auto-refresh every 2s. raw: <a href="/api/status" target="_blank">/api/status</a> · <a href="/api/strategies" target="_blank">/api/strategies</a></div>

<script>
const fmt = (n, d=4) => n == null ? '—' : Number(n).toFixed(d);
const fmt2 = n => fmt(n, 2);
const fmtPct = n => n == null ? '—' : (Number(n)*100).toFixed(2)+'%';
const fmtSec = s => {
  if (s == null) return '—';
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = Math.floor(s%60);
  return (h>0?h+'h':'') + (m>0||h>0?m+'m':'') + sec+'s';
};
const fmtTs = ts => {
  if (!ts) return '—';
  // SQLite timestamp can be ISO8601 with or without +00:00 / Z
  const d = new Date(ts.includes('T') ? ts : ts.replace(' ', 'T'));
  if (isNaN(d.getTime())) return ts.slice(11, 19);
  return d.toISOString().slice(11, 19);
};

async function get(url) {
  const r = await fetch(url, {cache: 'no-store'});
  if (!r.ok) throw new Error(`${url} → ${r.status}`);
  return r.json();
}

function strategyCard(s) {
  const pill = (label, kind='') => `<span class="pill ${kind}">${label}</span>`;
  let badges = pill(s.type);
  if (s.market_id != null) badges += pill('mkt='+s.market_id);
  if (s.warmup_done === false) badges += pill('warmup '+s.event_count+'/'+s.warmup_events, 'warn');
  else if (s.warmup_done === true) badges += pill('active', 'ok');

  // ─── Fair value + 触发价区间（meanrev 核心一眼判读）──────────────
  let fairBlock = '';
  if (s.ema_price != null) {
    const dev = s.last_signal?.deviation;
    const inDb = s.last_signal?.in_deadband;
    const lastPx = s.last_signal?.price_a;
    fairBlock = `
      <div class="kv" style="font-size:13px;">
        <span class="k">📍 fair value (EMA)</span>
        <span class="v ok">${fmt(s.ema_price, 4)}</span>
      </div>
      <div class="kv">
        <span class="k">trigger range</span>
        <span class="v">${fmt(s.trigger_price_low, 4)} ← <span class="ok">deadband</span> → ${fmt(s.trigger_price_high, 4)}</span>
      </div>
      ${lastPx != null ? `<div class="kv">
        <span class="k">last seen price</span>
        <span class="v ${inDb ? 'ok' : 'warn'}">${fmt(lastPx, 4)}${inDb===false ? ' ⚡ over threshold' : ''}</span>
      </div>` : ''}
    `;
  }

  // ─── Signal viz bar ──────────────────────────────────────────────
  let signalViz = '';
  if (s.threshold_logit != null && s.last_signal?.deviation != null) {
    const t = Math.max(0.001, s.threshold_logit);
    const dev = s.last_signal.deviation;
    const ratio = Math.max(-3, Math.min(3, dev / t)) / 6 + 0.5;
    const mult = s.last_signal.size_multiplier;
    signalViz = `
      <div class="signal-bar">
        <div class="band" style="left:${(0.5 - 0.5/3)*100}%;width:${(1/3)*100}%"></div>
        <div class="marker" style="left:${ratio*100}%"></div>
      </div>
      <div class="small">last signal: dev=${fmt(dev,4)} (${(dev/t).toFixed(2)}×thr)  mult=${fmt(mult,2)}  ${ageStr(s.last_signal.ts)} ago</div>
    `;
  }

  // ─── 最近一次成交（解释上次为什么动）──────────────────────────────
  let lastTradeBlock = '';
  if (s.last_trade) {
    const t = s.last_trade;
    const sideCls = 'side-' + t.side;
    lastTradeBlock = `
      <div class="kv" style="margin-top:6px;border-top:1px solid var(--border);padding-top:6px;">
        <span class="k">last trade</span>
        <span class="v ${sideCls}">${t.side.toUpperCase()} outcome=${t.outcome_id} shares=${t.shares}</span>
      </div>
      <div class="small">@ price ${fmt(t.price_a, 4)}  dev=${fmt(t.deviation, 4)}  cost=${t.cost}  ${ageStr(t.ts)} ago</div>
    `;
  }

  // ─── 持仓 ────────────────────────────────────────────────────────
  const holdings = s.holding ? Object.entries(s.holding).map(([oid, amt]) =>
    `<div class="kv"><span class="k">holding o=${oid}</span><span class="v">${amt}</span></div>`
  ).join('') : '';

  return `<div class="card">
    <div style="font-weight:600;margin-bottom:6px;">${s.name}</div>
    <div style="margin-bottom:6px;">${badges}</div>
    ${fairBlock}
    ${signalViz}
    ${holdings}
    ${s.cash != null ? `<div class="kv"><span class="k">cash</span><span class="v">${s.cash}</span></div>` : ''}
    <div class="small" style="margin-top:4px;">
      ${s.trade_pct_of_cash != null ? `pct=${s.trade_pct_of_cash} ` : ''}
      ${s.size_scale_cap != null ? `cap=×${s.size_scale_cap} ` : ''}
      ${s.max_holding_per_side != null ? `max_h=${s.max_holding_per_side}` : ''}
    </div>
    ${lastTradeBlock}
  </div>`;
}

// 把 wall-time epoch 秒转成 "Xs / Xm / Xh ago" 形式
function ageStr(epochSec) {
  if (epochSec == null) return '?';
  const sec = Date.now() / 1000 - epochSec;
  if (sec < 60) return Math.round(sec) + 's';
  if (sec < 3600) return Math.floor(sec/60) + 'm' + Math.round(sec%60) + 's';
  return Math.floor(sec/3600) + 'h' + Math.floor((sec%3600)/60) + 'm';
}

async function refresh() {
  try {
    const [st, ss, trades, orders, decisions] = await Promise.all([
      get('/api/status'),
      get('/api/strategies'),
      get('/api/trades?limit=15'),
      get('/api/orders?limit=15'),
      get('/api/decisions?limit=15'),
    ]);

    // System card
    document.getElementById('base-url').textContent = st.base_url;
    document.getElementById('uptime').textContent = '· up '+fmtSec(st.uptime_sec);
    document.getElementById('uptime-2').textContent = fmtSec(st.uptime_sec);
    document.getElementById('strat-count').textContent = st.strategies_count;
    document.getElementById('dryrun-pill').innerHTML = st.dry_run
      ? '<span class="pill warn">DRYRUN</span>'
      : '<span class="pill err">LIVE</span>';

    // SSE card
    const idle = st.sse.idle_sec;
    const sseEl = document.getElementById('sse-status');
    if (!st.sse.active) { sseEl.textContent = 'inactive'; sseEl.className = 'v warn'; }
    else if (idle != null && idle > 60) { sseEl.textContent = 'stalled'; sseEl.className = 'v err'; }
    else if (idle != null && idle > 30) { sseEl.textContent = 'slow'; sseEl.className = 'v warn'; }
    else { sseEl.textContent = 'healthy'; sseEl.className = 'v ok'; }
    document.getElementById('sse-idle').textContent = idle != null ? fmt(idle, 1)+'s' : '—';
    document.getElementById('sse-markets').textContent = JSON.stringify(st.sse.subscribed_markets);
    const dedupEl = document.getElementById('sse-dedup');
    const ddp = st.sse.dedup_skipped_count;
    dedupEl.textContent = ddp ?? '—';
    // dedup 不应该高频涨，>10 标黄 >100 标红提醒
    dedupEl.className = 'v ' + (ddp > 100 ? 'err' : ddp > 10 ? 'warn' : 'ok');

    // Strategies
    document.getElementById('strategies').innerHTML = ss.map(strategyCard).join('') || '<div class="card small">no enabled strategies</div>';

    // Tables
    const renderRow = (tr) => `<tr>
      <td>${fmtTs(tr.ts)}</td><td>${tr.market_id ?? '—'}</td><td>${tr.outcome_id ?? '—'}</td>
      <td class="side-${(tr.side ?? tr.type ?? '').toLowerCase()}">${tr.side ?? tr.type ?? ''}</td>
      <td class="num">${tr.shares ?? ''}</td><td class="num">${fmt(tr.price, 4)}</td><td>${tr.username ?? ''}</td>
    </tr>`;
    document.querySelector('#trades-table tbody').innerHTML = trades.map(renderRow).join('') || '<tr><td colspan="7" class="small">no recent trades</td></tr>';

    document.querySelector('#orders-table tbody').innerHTML = orders.map(o => `<tr>
      <td>${fmtTs(o.ts)}</td><td>${o.strategy}</td><td>${o.outcome_id}</td>
      <td class="side-${o.side.toLowerCase()}">${o.side}</td>
      <td class="num">${o.shares}</td><td class="num">${fmt(o.price, 4)}</td>
      <td class="num">${fmt(o.cost, 2)}</td><td>${o.status}</td>
    </tr>`).join('') || '<tr><td colspan="8" class="small">no orders yet</td></tr>';

    document.querySelector('#decisions-table tbody').innerHTML = decisions.map(d => `<tr class="${d.action==='skip'?'small':''}">
      <td>${fmtTs(d.ts)}</td><td>${d.strategy}</td>
      <td class="side-${(d.action==='buy'||d.action==='sell')?d.action:''}">${d.action}</td>
      <td>${d.outcome_id ?? '—'}</td><td>${d.reason ?? ''}</td>
    </tr>`).join('') || '<tr><td colspan="5" class="small">no decisions yet</td></tr>';

  } catch (e) {
    console.error(e);
  }
}

refresh();
setInterval(refresh, 2000);
</script>
</body></html>
"""


def make_app() -> FastAPI:
    app = FastAPI(title="thccb-quant monitor", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return _INDEX_HTML

    @app.get("/api/status")
    async def status():
        sub = RUNTIME.subscriber
        idle = None
        markets: list[int] = []
        dedup_count = 0
        if sub is not None:
            try:
                idle = time.monotonic() - sub.last_event_handled_ts
                markets = sorted(sub._market_ids)
                dedup_count = getattr(sub, "dedup_skipped_count", 0)
            except Exception:
                pass
        return {
            "uptime_sec": time.monotonic() - RUNTIME.started_at,
            "started_at_wall": RUNTIME.started_at_wall,
            "dry_run": RUNTIME.dry_run,
            "base_url": RUNTIME.base_url,
            "strategies_count": len(RUNTIME.strategies),
            "sse": {
                "active": sub is not None,
                "idle_sec": idle,
                "subscribed_markets": markets,
                "dedup_skipped_count": dedup_count,
            },
        }

    @app.get("/api/strategies")
    async def strategies():
        out = []
        for s in RUNTIME.strategies:
            try:
                out.append(s.snapshot())
            except Exception as e:
                out.append({"name": getattr(s, "name", "?"), "error": str(e)})
        return out

    @app.get("/api/trades")
    async def trades(limit: int = Query(50, ge=1, le=500),
                     market_id: Optional[int] = None):
        if RUNTIME.store is None:
            return []
        return await RUNTIME.store.recent_trades_observed(
            market_id=market_id, limit=limit,
        )

    @app.get("/api/orders")
    async def orders(strategy: Optional[str] = None,
                     limit: int = Query(50, ge=1, le=500)):
        if RUNTIME.store is None:
            return []
        return await RUNTIME.store.recent_orders(strategy=strategy, limit=limit)

    @app.get("/api/decisions")
    async def decisions(strategy: Optional[str] = None,
                        limit: int = Query(50, ge=1, le=500)):
        if RUNTIME.store is None:
            return []
        return await RUNTIME.store.recent_decisions(strategy=strategy, limit=limit)

    return app


async def serve(host: str, port: int, logger) -> None:
    """启动 uvicorn server，绑同一个 asyncio loop。失败仅 log warning 不
    抛出，避免 web UI 失败把 trader 拖死。"""
    try:
        app = make_app()
        config = uvicorn.Config(
            app, host=host, port=port,
            log_level="warning", access_log=False,
            # asgi lifespan disabled — we already in a running loop, app has
            # no lifespan handlers itself
            lifespan="off",
        )
        server = uvicorn.Server(config)
        logger.info("web_starting", host=host, port=port)
        await server.serve()
    except Exception:
        logger.exception("web_server_crashed")
