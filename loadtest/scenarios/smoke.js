// smoke.js — 1 VU × 60s，跑通每个端点；任何 5xx 直接 fail。
//
// 用法：
//   k6 run -e BASE_URL=http://127.0.0.1:8004 \
//          -e HOT_OUTCOME_YES=11 -e HOT_MARKET_ID=6 \
//          loadtest/scenarios/smoke.js
//
// 这一步是「ramp/SSE 之前**必须**绿的门槛」：token 是否有效、市场是否 seed 好、
// HOT_OUTCOME_YES 这种环境变量是否正确，都在这里验。

import http from 'k6/http';
import { check, sleep, fail } from 'k6';
import { authHeaders, BASE_URL } from './lib/auth.js';

export const options = {
  vus: 1,
  duration: '60s',
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<500'],
    checks: ['rate>0.99'],
  },
};

const HOT_OUTCOME = __ENV.HOT_OUTCOME_YES;
const HOT_MARKET = __ENV.HOT_MARKET_ID;

if (!HOT_OUTCOME || !HOT_MARKET) {
  fail('需要 HOT_OUTCOME_YES + HOT_MARKET_ID 环境变量；从 seed_markets.sh 输出里找');
}

export default function () {
  const h = authHeaders(__VU);

  // 1) /me — token 是否有效
  let r = http.get(`${BASE_URL}/api/v1/auth/me`, { headers: h });
  check(r, { 'me 200': (x) => x.status === 200 });

  // 2) market list — 公共
  r = http.get(`${BASE_URL}/api/v1/market/list`, { headers: h });
  check(r, { 'list 200': (x) => x.status === 200 });

  // 3) market detail
  r = http.get(`${BASE_URL}/api/v1/market/${HOT_MARKET}`, { headers: h });
  check(r, { 'detail 200': (x) => x.status === 200 });

  // 4) quote — 不真实成交
  r = http.post(
    `${BASE_URL}/api/v1/market/quote`,
    JSON.stringify({ outcome_id: Number(HOT_OUTCOME), side: 'buy', shares: 1 }),
    { headers: h },
  );
  check(r, { 'quote 200': (x) => x.status === 200 });

  // 5) buy 1 share
  r = http.post(
    `${BASE_URL}/api/v1/market/buy`,
    JSON.stringify({ outcome_id: Number(HOT_OUTCOME), shares: 1 }),
    { headers: h },
  );
  check(r, {
    'buy 200': (x) => x.status === 200,
    'buy not 5xx': (x) => x.status < 500,
  });

  // 6) loan quota
  r = http.get(`${BASE_URL}/api/v1/loan/quota`, { headers: h });
  check(r, { 'loan 200': (x) => x.status === 200 });

  sleep(1);
}
