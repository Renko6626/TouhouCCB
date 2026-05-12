/**
 * Casdoor OAuth2 登录 — 服务端生成 state/nonce，前端只负责跳转。
 *
 * 标准 OAuth2 Authorization Code 流程：
 *   1. 前端调 POST /api/v1/auth/login-start
 *      → 后端生成 state+nonce，写 HttpOnly cookie，body 同值返回
 *   2. 前端拼 authorize URL（带 state + nonce）→ 跳转到 Casdoor 登录页
 *   3. 用户登录后 Casdoor 重定向回 /auth/callback?code=xxx&state=yyy
 *   4. 前端把 code+state 发给后端换本站 JWT
 *      → 后端从 cookie 校验 state + id_token.nonce
 */

import http from './index'

const CASDOOR_URL = import.meta.env.VITE_CASDOOR_URL as string
const CLIENT_ID = import.meta.env.VITE_CASDOOR_CLIENT_ID as string
const ORG_NAME = import.meta.env.VITE_CASDOOR_ORG as string
const APP_NAME = import.meta.env.VITE_CASDOOR_APP as string

const REQUIRED = { VITE_CASDOOR_URL: CASDOOR_URL, VITE_CASDOOR_CLIENT_ID: CLIENT_ID, VITE_CASDOOR_ORG: ORG_NAME, VITE_CASDOOR_APP: APP_NAME }
for (const [key, val] of Object.entries(REQUIRED)) {
  if (!val) console.error(`[Casdoor] 缺少环境变量: ${key}`)
}

const REDIRECT_URI = `${window.location.origin}/auth/callback`

interface LoginStartResponse {
  state: string
  nonce: string
}

/** 调后端 /auth/login-start 拿 state+nonce（后端同时写 HttpOnly cookie） */
async function fetchStateAndNonce(): Promise<LoginStartResponse> {
  return http.post<LoginStartResponse>('/api/v1/auth/login-start')
}

async function buildAuthorizeUrl(type: 'login' | 'signup'): Promise<string> {
  const { state, nonce } = await fetchStateAndNonce()
  const params = new URLSearchParams({
    client_id: CLIENT_ID,
    response_type: 'code',
    redirect_uri: REDIRECT_URI,
    scope: 'openid profile email',
    state,
    nonce,
  })
  const path = type === 'signup'
    ? `/signup/${APP_NAME}`
    : `/login/oauth/authorize`
  return `${CASDOOR_URL}${path}?${params.toString()}`
}

export const getLoginUrl = () => buildAuthorizeUrl('login')
export const getRegisterUrl = () => buildAuthorizeUrl('signup')
