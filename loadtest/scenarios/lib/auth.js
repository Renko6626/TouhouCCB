// auth.js — 共享：从 tokens.txt 加载 token 池，按 VU 分发。
//
// k6 启动时通过 SharedArray 一次性读 tokens.txt（每行 "uid\tusername\ttoken"），
// 后续 VU 内存共享、不重复读盘。每个 VU 拿对应行（轮转），保证身份稳定，
// 这样后端 SELECT FOR UPDATE user 的锁竞争更接近真实用户行为（不会同一个用户
// 多 VU 抢同一行）。

import { SharedArray } from 'k6/data';

export const TOKENS = new SharedArray('loadtest_tokens', function () {
  const path = __ENV.TOKENS_FILE || '../../tokens.txt';
  const raw = open(path);
  const out = [];
  raw.split('\n').forEach((line) => {
    if (!line || line.startsWith('#')) return;
    const parts = line.split('\t');
    if (parts.length < 3) return;
    out.push({ uid: parts[0], username: parts[1], token: parts[2] });
  });
  if (out.length === 0) {
    throw new Error(`no tokens loaded from ${path}; did you run mint_tokens.sh?`);
  }
  return out;
});

// VU 编号对 token 池取模，保证同一个 VU 始终用同一身份。
export function tokenForVU(vuId) {
  const idx = (vuId - 1) % TOKENS.length;
  return TOKENS[idx];
}

export function authHeaders(vuId, extra = {}) {
  const t = tokenForVU(vuId);
  return Object.assign(
    {
      Authorization: `Bearer ${t.token}`,
      'Content-Type': 'application/json',
      'X-Loadtest': '1',
    },
    extra,
  );
}

export const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8004';
