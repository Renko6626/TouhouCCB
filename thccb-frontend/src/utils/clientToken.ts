/**
 * Anti-bot L2: SubtleCrypto HMAC-SHA256 client_token 生成。
 *
 * 跟后端 app/services/anti_bot.py::verify_client_token 同算法：
 *   token = HMAC_SHA256(SECRET, "{ts}|{uid}")
 *
 * SECRET 通过 vite build-time env 注入 (VITE_CLIENT_TOKEN_SECRET)，
 * 嵌进 bundle。activity_mode=false 时后端不验，前端发了也无害。
 *
 * 详见 docs/superpowers/specs/2026-05-20-anti-bot-design.md
 */
export async function generateClientToken(
  uid: number,
): Promise<{ token: string; ts: number }> {
  const ts = Math.floor(Date.now() / 1000)
  const secret = import.meta.env.VITE_CLIENT_TOKEN_SECRET as string | undefined
  if (!secret) {
    // dev 环境没设 SECRET，返回空 token (后端 activity_mode=false 时不影响)
    return { token: '', ts }
  }

  const key = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  )
  const msg = new TextEncoder().encode(`${ts}|${uid}`)
  const sigBuf = await crypto.subtle.sign('HMAC', key, msg)
  const hex = Array.from(new Uint8Array(sigBuf))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
  return { token: hex, ts }
}
