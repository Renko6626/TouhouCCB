// sse_fanout.js — 200 VU 维持 SSE 长连，另起 background 交易触发广播。
//
// 设计意图：
//   * 200 个用户同时订阅 /api/v1/stream/market/{HOT_MARKET_ID}（SSE 不需要 auth）
//   * 50 个 background trader VU 不断 buy/sell HOT_OUTCOME，每笔成交触发
//     BROKER.publish → fan-out 给所有 200 个订阅者
//   * 主要看：SSE 连接建立耗时、首包 snapshot 延迟、心跳是否准时（25s timeout
//     里收不到事件就发 ping）、连接是否被 nginx / docker / kernel keepalive 杀掉。
//
// k6 的 http.get 不直接支持持续 SSE，用 net/http 模式 + 手动 chunked 读：
// 这里用 k6/experimental/streams 的简化方案 —— 用 http.get 拿 stream，但 k6
// 默认会等到 EOF。SSE 在 backend 是 1 小时上限，VU 期间一直挂着即可。
//
// 用法：
//   k6 run -e BASE_URL=http://127.0.0.1:8004 -e HOT_MARKET_ID=6 \
//          -e HOT_OUTCOME_YES=11 -e HOT_OUTCOME_NO=12 \
//          --out json=loadtest/results/sse_fanout_$(date +%s).json \
//          loadtest/scenarios/sse_fanout.js

import http from 'k6/http';
import { check, fail, sleep } from 'k6';
import { Trend, Counter, Rate } from 'k6/metrics';
import { authHeaders, BASE_URL } from './lib/auth.js';

const HOT_MARKET = __ENV.HOT_MARKET_ID;
const HOT_YES = __ENV.HOT_OUTCOME_YES;
const HOT_NO = __ENV.HOT_OUTCOME_NO;

if (!HOT_MARKET || !HOT_YES || !HOT_NO) {
  fail('需要 HOT_MARKET_ID / HOT_OUTCOME_YES / HOT_OUTCOME_NO');
}

const sse_connect_ms = new Trend('lt_sse_connect_ms', true);
const sse_first_byte_ms = new Trend('lt_sse_first_byte_ms', true);
const sse_failed = new Rate('lt_sse_failed');
const trader_buys = new Counter('lt_trader_buys');

export const options = {
  scenarios: {
    sse_subscribers: {
      executor: 'ramping-vus',
      exec: 'sseSubscriber',
      startVUs: 0,
      stages: [
        { duration: '30s', target: 200 },
        { duration: '5m',  target: 200 },
        { duration: '15s', target: 0 },
      ],
      gracefulRampDown: '15s',
    },
    background_traders: {
      executor: 'ramping-vus',
      exec: 'backgroundTrader',
      startVUs: 0,
      // trader 比订阅者后启动 10s，确保订阅都建好再制造广播
      stages: [
        { duration: '10s', target: 0 },
        { duration: '20s', target: 50 },
        { duration: '5m',  target: 50 },
        { duration: '15s', target: 0 },
      ],
      gracefulRampDown: '15s',
    },
  },
  thresholds: {
    lt_sse_failed: ['rate<0.05'],
    lt_sse_connect_ms: ['p(95)<3000'],
  },
};

export function sseSubscriber() {
  // SSE 端点不需要 auth，但加 X-Loadtest 方便后端 access log 过滤
  const t0 = Date.now();
  const r = http.get(`${BASE_URL}/api/v1/stream/market/${HOT_MARKET}`, {
    headers: { 'X-Loadtest': '1', Accept: 'text/event-stream' },
    timeout: '120s',  // 不设大点 k6 会主动 abort
    tags: { name: 'sse' },
  });
  sse_connect_ms.add(Date.now() - t0);

  // r.body 里第一帧应该是 snapshot 事件
  const ok = r.status === 200 && r.body && r.body.indexOf('snapshot') >= 0;
  sse_failed.add(!ok);
  if (ok) {
    // 用 timings.waiting 近似 TTFB
    sse_first_byte_ms.add(r.timings.waiting);
  }
  check(r, {
    'sse 200': (x) => x.status === 200,
    'sse has snapshot': (x) => x.body && x.body.indexOf('snapshot') >= 0,
  });
}

export function backgroundTrader() {
  const h = authHeaders(__VU);
  const outcome = Math.random() < 0.5 ? HOT_YES : HOT_NO;
  // 故意慢节奏：每 trader 大约每秒 1 笔，50 trader = 50 events/s，
  // 给 200 订阅者 fan-out 出 200×50=10000 个推送/s 的压力（broker 那一侧）
  const r = http.post(
    `${BASE_URL}/api/v1/market/buy`,
    JSON.stringify({ outcome_id: Number(outcome), shares: 1 }),
    { headers: h, tags: { name: 'trader_buy' } },
  );
  trader_buys.add(1);
  check(r, { 'trader buy not 5xx': (x) => x.status < 500 });
  sleep(1);
}
