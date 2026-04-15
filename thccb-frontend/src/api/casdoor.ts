/**
 * Casdoor OAuth2 登录 — 纯 URL 拼接，不依赖 casdoor-js-sdk。
 *
 * 标准 OAuth2 Authorization Code 流程：
 *   1. 前端拼 authorize URL → 跳转到 Casdoor 登录页
 *   2. 用户登录后 Casdoor 重定向回 /auth/callback?code=xxx&state=yyy
 *   3. 前端把 code 发给后端换本站 JWT
 */

const CASDOOR_URL = import.meta.env.VITE_CASDOOR_URL as string
const CLIENT_ID = import.meta.env.VITE_CASDOOR_CLIENT_ID as string
const ORG_NAME = import.meta.env.VITE_CASDOOR_ORG as string
const APP_NAME = import.meta.env.VITE_CASDOOR_APP as string

const REQUIRED = { VITE_CASDOOR_URL: CASDOOR_URL, VITE_CASDOOR_CLIENT_ID: CLIENT_ID, VITE_CASDOOR_ORG: ORG_NAME, VITE_CASDOOR_APP: APP_NAME }
for (const [key, val] of Object.entries(REQUIRED)) {
  if (!val) console.error(`[Casdoor] 缺少环境变量: ${key}`)
}

const REDIRECT_URI = `${window.location.origin}/auth/callback`

/** 生成随机 state 存入 sessionStorage，用于 CSRF 防护 */
function generateState(): string {
  const state = crypto.randomUUID()
  sessionStorage.setItem('oauth_state', state)
  return state
}

/** 拼接 Casdoor OAuth2 authorize URL */
function buildAuthorizeUrl(type: 'login' | 'signup'): string {
  const state = generateState()
  const params = new URLSearchParams({
    client_id: CLIENT_ID,
    response_type: 'code',
    redirect_uri: REDIRECT_URI,
    scope: 'openid profile email',
    state,
  })
  const path = type === 'signup'
    ? `/signup/${APP_NAME}`
    : `/login/oauth/authorize`
  return `${CASDOOR_URL}${path}?${params.toString()}`
}

export const getLoginUrl = () => buildAuthorizeUrl('login')
export const getRegisterUrl = () => buildAuthorizeUrl('signup')
