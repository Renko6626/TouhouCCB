import Sdk from 'casdoor-js-sdk'

const REQUIRED_ENV = [
  'VITE_CASDOOR_URL',
  'VITE_CASDOOR_CLIENT_ID',
  'VITE_CASDOOR_ORG',
  'VITE_CASDOOR_APP',
] as const

for (const key of REQUIRED_ENV) {
  if (!import.meta.env[key]) {
    console.error(`[Casdoor] 缺少必要的环境变量: ${key}，请检查 .env 文件`)
  }
}

const casdoorSdk = new Sdk({
  serverUrl: import.meta.env.VITE_CASDOOR_URL as string,
  clientId: import.meta.env.VITE_CASDOOR_CLIENT_ID as string,
  organizationName: import.meta.env.VITE_CASDOOR_ORG as string,
  appName: import.meta.env.VITE_CASDOOR_APP as string,
  redirectPath: '/auth/callback',
})

/** 生成随机 state 并存入 sessionStorage，用于 CSRF 防护 */
const withState = (url: string): string => {
  const state = crypto.randomUUID()
  sessionStorage.setItem('oauth_state', state)
  // Casdoor SDK 生成的 URL 已带 state 参数，替换为我们自己的
  return url.replace(/([?&])state=[^&]*/, `$1state=${state}`)
}

export const getLoginUrl = () => withState(casdoorSdk.getSigninUrl())
export const getRegisterUrl = () => withState(casdoorSdk.getSignupUrl())
