// trade_ramp.js — 主力压测：1 → 200 VU 阶梯，混合交易场景，集中打热门 outcome。
//
// 设计意图：
//   * 50% quote / 30% buy / 15% sell / 5% /me — 大致还原真实用户行为
//   * 全部 buy/sell 集中打 HOT_OUTCOME_YES/NO，制造同一 market 的 SELECT FOR UPDATE
//     队列，看后端在 200 并发上 p99 / 5xx / 锁等待。
//   * 每 VU 一个 token = 一个用户，同一用户的串行被 backend 锁保护，跨用户竞争才是
//     真正要测的东西。
//
// 用法：
//   k6 run -e BASE_URL=http://127.0.0.1:8004 \
//          -e HOT_MARKET_ID=6 -e HOT_OUTCOME_YES=11 -e HOT_OUTCOME_NO=12 \
//          --out json=loadtest/results/trade_ramp_$(date +%s).json \
//          loadtest/scenarios/trade_ramp.js
//
// 中止阈值（thresholds）一旦触发 k6 自动 abort：
//   * http_req_failed > 5%
//   * p99 > 2000ms

import http from 'k6/http';
import { check, fail } from 'k6';
import { Trend, Rate } from 'k6/metrics';
import { authHeaders, BASE_URL } from './lib/auth.js';

const HOT_MARKET = __ENV.HOT_MARKET_ID;
const HOT_YES = __ENV.HOT_OUTCOME_YES;
const HOT_NO = __ENV.HOT_OUTCOME_NO;

if (!HOT_MARKET || !HOT_YES || !HOT_NO) {
  fail('需要 HOT_MARKET_ID / HOT_OUTCOME_YES / HOT_OUTCOME_NO');
}

const buy_lat = new Trend('lt_buy_ms', true);
const sell_lat = new Trend('lt_sell_ms', true);
const quote_lat = new Trend('lt_quote_ms', true);
const buy_5xx = new Rate('lt_buy_5xx');
const sell_5xx = new Rate('lt_sell_5xx');

export const options = {
  scenarios: {
    ramp: {
      executor: 'ramping-vus',
      startVUs: 1,
      // 共 ~13 分钟：阶梯 + 平台期，给观测脚本足够采样
      stages: [
        { duration: '30s',  target: 10 },
        { duration: '1m',   target: 50 },
        { duration: '1m',   target: 100 },
        { duration: '1m',   target: 200 },
        { duration: '5m',   target: 200 },  // 200 VU 平台 5min — 主要观测期
        { duration: '30s',  target: 0 },
      ],
      gracefulRampDown: '15s',
    },
  },
  thresholds: {
    http_req_failed:   ['rate<0.05'],     // 整体失败率 < 5%（含 422 业务错误）
    http_req_duration: ['p(99)<2000'],
    lt_buy_5xx:        ['rate<0.01'],     // 5xx 严格：< 1%
    lt_sell_5xx:       ['rate<0.01'],
  },
};

function pickAction() {
  // 50/30/15/5
  const r = Math.random();
  if (r < 0.50) return 'quote';
  if (r < 0.80) return 'buy';
  if (r < 0.95) return 'sell';
  return 'me';
}

function pickOutcome() {
  return Math.random() < 0.5 ? HOT_YES : HOT_NO;
}

export default function () {
  const h = authHeaders(__VU);
  const outcome = Number(pickOutcome());
  const action = pickAction();

  if (action === 'quote') {
    const t0 = Date.now();
    const r = http.post(
      `${BASE_URL}/api/v1/market/quote`,
      JSON.stringify({
        outcome_id: outcome,
        side: Math.random() < 0.5 ? 'buy' : 'sell',
        shares: 1 + Math.floor(Math.random() * 5),
      }),
      { headers: h, tags: { name: 'quote' } },
    );
    quote_lat.add(Date.now() - t0);
    check(r, { 'quote ok': (x) => x.status === 200 || x.status === 400 });
  } else if (action === 'buy') {
    const t0 = Date.now();
    const r = http.post(
      `${BASE_URL}/api/v1/market/buy`,
      JSON.stringify({ outcome_id: outcome, shares: 1 + Math.floor(Math.random() * 3) }),
      { headers: h, tags: { name: 'buy' } },
    );
    buy_lat.add(Date.now() - t0);
    buy_5xx.add(r.status >= 500);
    // 200 = 成功；400 = 现金不足 / 市场不交易，业务正常；429 = nginx 限流（白名单没生效）
    check(r, { 'buy not 5xx': (x) => x.status < 500 });
  } else if (action === 'sell') {
    const t0 = Date.now();
    const r = http.post(
      `${BASE_URL}/api/v1/market/sell`,
      JSON.stringify({ outcome_id: outcome, shares: 1 }),
      { headers: h, tags: { name: 'sell' } },
    );
    sell_lat.add(Date.now() - t0);
    sell_5xx.add(r.status >= 500);
    check(r, { 'sell not 5xx': (x) => x.status < 500 });
  } else {
    // /me — 轻量心跳
    const r = http.get(`${BASE_URL}/api/v1/auth/me`, { headers: h, tags: { name: 'me' } });
    check(r, { 'me 200': (x) => x.status === 200 });
  }
  // 不 sleep — 让 VU 自然受 backend 响应延迟节流
}
