# 抓 thccb token 操作指南

## TL;DR

登录 thccb.com 后，打开 F12 → Application → Local Storage → `https://thccb.secret-sealing.club`，直接复制 `access_token` 和 `refresh_token` 两个键的值，填进 `quant/.env`。

---

## 背景：thccb 的认证流程

1. 浏览器点"登录" → 前端调 `POST /api/v1/auth/login-start` → 后端生成随机 state + nonce，以 HttpOnly cookie 写回浏览器
2. 前端把 state/nonce 拼进 URL，跳转到 Casdoor 登录页（`https://auth.thccb.com/login/oauth/authorize?...`）
3. 用户在 Casdoor 输账号密码，Casdoor 重定向回 `https://thccb.secret-sealing.club/auth/callback?code=xxx&state=yyy`
4. 前端拿 code + state → `POST /api/v1/auth/callback` → 后端向 Casdoor 换 token、验 id_token 签名与 nonce → 颁发**本站自签 HS256 JWT**
5. 后端响应体里明文返回 `{ access_token, refresh_token, token_type: "bearer" }`
6. 前端（Pinia auth store）把两个 token **存到 `localStorage`**，之后每次请求带 `Authorization: Bearer <access_token>` header

关键结论：**两个 token 都在 localStorage，不是 HttpOnly cookie，F12 直接能读到。**

---

## 步骤

### 1. 打开浏览器，先不要登录

用 Chrome 或 Edge（下面截图位置以 Chrome 为准）。

打开 F12（DevTools），切到 **Application** 标签。

左侧展开 **Storage → Local Storage → `https://thccb.secret-sealing.club`**。

此时如果已经登录过，能直接看到 `access_token` / `refresh_token`；如果还没登录，继续下一步。

### 2. 登录 thccb.com

访问 `https://thccb.secret-sealing.club`，点右上角登录按钮。

完成 Casdoor 登录后，浏览器会跳回 `/auth/callback`，短暂显示"正在登录…"，随后自动跳到首页。

### 3. 抓 access_token

登录完成后，回到 F12 → **Application → Local Storage**，页面刷新后应看到：

| Key | Value |
|-----|-------|
| `access_token` | `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI...` |
| `refresh_token` | `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI...` |
| `user` | `{"id":...,"username":"..."}` |

点击 `access_token` 这行，底部预览框会显示完整值，**右键复制**（或直接双击值区域全选复制）。

access_token 是 HS256 JWT，三段 `eyJ...`，格式长这样：
```
eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIiwiZXhwIjox....<signature>
```

**有效期 60 分钟**。量化脚本会在 token 过期后自动用 refresh_token 续期，所以初始值哪怕快过期也没关系。

### 4. 抓 refresh_token

同样在 Local Storage 面板，点击 `refresh_token` 这行，复制完整值。

refresh_token 格式与 access_token 相同（同为 HS256 JWT），但 payload 里 `"type": "refresh"`，**有效期 7 天**。

> 注意：state/nonce 那两个 cookie（`thccb_oauth_state`、`thccb_oauth_nonce`）是 HttpOnly 的，但你不需要它们，也不要去动它们。你只需要 localStorage 里的两个 token。

### 5. 填到 quant/.env

编辑项目根目录下的 `quant/.env`（参考 `quant/.env.example`）：

```dotenv
THCCB_BASE_URL=https://thccb.secret-sealing.club
THCCB_ACCESS_TOKEN=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI...（你复制的完整值）
THCCB_REFRESH_TOKEN=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI...（你复制的完整值）
```

注意：**整行不能有换行**，JWT 是一整行字符串。

**BASE_URL 怎么选**：
- 量化脚本和 prod docker **跑在同一台机器** → 用 `http://127.0.0.1:8004`（走 docker 暴露在 127.0.0.1 的端口，不过 nginx 不过 TLS，最快）
- 量化脚本跑在**另一台机器**（如远程开发机调 prod） → 用 `https://thccb.secret-sealing.club`（走公网）

两种情况下 token 都通用——token 是后端用同一个 `SECRET_KEY` 签的 HS256 JWT，与 URL 无关。

---

## 验证

填好 .env 后，执行以下 curl 命令验证 access_token 有效：

```bash
source quant/.env
curl -s -H "Authorization: Bearer $THCCB_ACCESS_TOKEN" \
  "$THCCB_BASE_URL/api/v1/auth/me" | python3 -m json.tool
```

预期输出（能看到自己的账号信息）：
```json
{
    "id": 1,
    "username": "你的用户名",
    "email": "xxx@example.com",
    "is_superuser": true,
    "is_active": true,
    "cash": "500.000000",
    "debt": "0.000000",
    "tos_accepted_at": "2026-..."
}
```

如果看到 `{"detail": "Token expired"}` → access_token 已过期，但 refresh_token 未过期时脚本会自动续期，可以直接跑脚本；如果看到 `{"detail": "Invalid token"}` → 复制时漏字符，重新复制。

---

## token 过期了怎么办？

- **access_token 60 分钟过期** → 量化脚本的 `TokenManager` 会自动调 `/api/v1/auth/refresh` 续期，**无需手动操作**。
- **refresh_token 7 天过期** → `TokenManager` 续期失败，脚本会报 `TokenExpiredError`。此时重做上述步骤 2–5，重新登录抓新的 access_token + refresh_token 填回 .env，再重启脚本即可。

---

## 常见坑

**坑 1：Local Storage 里没有 token / 只有空值**

可能是登录没有成功完成（停在 Casdoor 那边没跳回来）。检查浏览器地址栏：
- 如果卡在 `auth.thccb.com`：Casdoor 那边登录失败，检查账号密码
- 如果地址是 `/auth/callback` 且有红色错误提示：state 校验失败（可能是同一个 tab 反复点了多次登录），关 F12、清除 cookie 后重新登录

**坑 2：复制了 access_token 填进 .env 但 curl 报 401**

最常见原因：复制时多了空格或换行。JWT 必须是三段 base64 中间两个点（`eyJ...eyJ...xxxxx`），可以用以下命令检查：
```bash
source quant/.env
echo $THCCB_ACCESS_TOKEN | tr -cd '.' | wc -c
# 应该输出 2（三段之间两个点）
```

**坑 3：curl 跑通了但脚本跑起来报 403**

403 是权限不足（不是 token 问题）。检查账号是否有足够余额，或操作的市场是否已关闭。

**坑 4：多账号串了（token 是另一个账号的）**

如果浏览器里之前用其他账号登录过，Local Storage 里的 token 可能是旧账号的。验证方法：`curl /auth/me` 看返回的 `username` 是不是你想用的那个账号。如果不对，在浏览器里先登出（清 localStorage），再用正确账号重新登录。

**坑 5：Casdoor 跳回时 URL 带的 redirect_uri 被拒绝**

后端 `redirect_uri` 硬编码为 `${FRONTEND_URL}/auth/callback`（即 `https://thccb.secret-sealing.club/auth/callback`）。如果你在本地跑开发环境想抓 token，redirect_uri 会不匹配导致 Casdoor 报错。直接在生产站 thccb.secret-sealing.club 上登录抓 token，不要在本地环境操作。

**坑 6：量化脚本同时跑多个实例导致 token 冲突**

两个实例共用同一个 .env 里的 refresh_token，当 access 同时过期时两个实例都会去 refresh，其中一个会先刷新成功；另一个拿旧 access_token 重试时会再刷一次（后端无状态，refresh_token 没有单次使用限制，两次都能成功）。不是 bug，可以正常运行。
