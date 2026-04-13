# Casdoor SSO 对接文档

## 背景与目标

将现有的「本地用户名密码 + 激活码注册」体系替换为 Casdoor SSO。
Casdoor 部署在独立域名（如 `https://auth.example.com`），负责所有用户身份认证；
TouhouCCB 前端和后端只负责业务逻辑，不再持有密码。

---

## 整体流程

```
用户点击登录
  │
  ▼
前端跳转 → Casdoor /login/oauth/authorize?client_id=...&redirect_uri=...
  │
  ▼
Casdoor 完成认证（登录/注册/MFA，全部在 Casdoor 域名内）
  │
  ▼
Casdoor 302 回调 → https://thccb.example.com/auth/callback?code=XXX&state=YYY
  │
  ▼
前端 Callback 页面把 code 发给本站后端 POST /api/v1/auth/callback
  │
  ▼
后端用 client_secret 向 Casdoor 换取 access_token + id_token
  │
  ▼
后端解析 JWT，查找或初始化本地用户记录（首次登录自动开户）
  │
  ▼
后端返回本站自签 JWT（或直接透传 Casdoor token，见方案选择）
  │
  ▼
前端存 token，进入应用
```

---

## Casdoor 端配置

在 Casdoor 管理后台创建一个 Application，记录以下字段：

| 字段 | 说明 |
|------|------|
| `clientId` | 前后端都需要 |
| `clientSecret` | **仅后端使用**，不要暴露给前端 |
| `organizationName` | 所属组织名 |
| `appName` | 应用名 |
| Redirect URL | 填写 `https://thccb.example.com/auth/callback` |

> Redirect URL 必须精确匹配前端实际跳转的地址，否则 OAuth 会拒绝。

---

## 前端需要做的事

### 1. 安装 SDK

```bash
npm install casdoor-js-sdk
```

（`casdoor-vue-sdk` 是对它的 Vue 封装，也可用，但直接用 js-sdk 更灵活。）

### 2. 新建 `src/api/casdoor.ts`

```typescript
import Sdk from 'casdoor-js-sdk'

export const casdoorSdk = new Sdk({
  serverUrl: import.meta.env.VITE_CASDOOR_URL,       // Casdoor 域名
  clientId: import.meta.env.VITE_CASDOOR_CLIENT_ID,
  organizationName: import.meta.env.VITE_CASDOOR_ORG,
  appName: import.meta.env.VITE_CASDOOR_APP,
  redirectPath: '/auth/callback',
})

export const getLoginUrl = () => casdoorSdk.getSigninUrl()
export const getRegisterUrl = () => casdoorSdk.getSignupUrl()
```

### 3. 修改登录/注册页面

原来的 `Login.vue` 和 `Register.vue` 里的表单逻辑全部删掉，改为一个跳转按钮：

```typescript
// 点击登录
window.location.href = getLoginUrl()

// 点击注册
window.location.href = getRegisterUrl()
```

页面只保留 UI 外壳和按钮，不再有输入框和本地验证。

### 4. 新建 `src/pages/auth/Callback.vue`

```typescript
// 从 URL 拿到 code 和 state
const code = new URLSearchParams(location.search).get('code')
const state = new URLSearchParams(location.search).get('state')

// 发给本站后端换 token
const res = await authApi.oauthCallback({ code, state })
authStore.setToken(res.token)
router.push('/')
```

### 5. 在 `routes.ts` 增加回调路由

```typescript
{
  path: '/auth/callback',
  name: 'auth-callback',
  component: () => import('@/pages/auth/Callback.vue'),
  meta: { requiresAuth: false }
}
```

### 6. 删除的前端代码

- `src/api/auth.ts` 中的 `login()` / `register()` / `activate()` 接口调用
- `src/pages/auth/Activate.vue`（激活流程迁移到 Casdoor）
- `authStore` 中的 `username`/`password` 相关逻辑

### 7. 环境变量 `.env`

```
VITE_CASDOOR_URL=https://auth.example.com
VITE_CASDOOR_CLIENT_ID=xxxxxxxxxxxxxxxx
VITE_CASDOOR_ORG=your-org
VITE_CASDOOR_APP=thccb
```

---

## 后端需要做的事

### 1. 安装 SDK

```bash
pip install casdoor
```

### 2. 新建配置项（`backend/app/core/config.py`）

```python
CASDOOR_ENDPOINT: str        # Casdoor 域名
CASDOOR_CLIENT_ID: str
CASDOOR_CLIENT_SECRET: str
CASDOOR_ORG_NAME: str
CASDOOR_APP_NAME: str
CASDOOR_CERTIFICATE: str     # Casdoor 的 JWT 公钥证书，用于本地验签
```

### 3. 初始化 SDK（`backend/app/core/casdoor.py`）

```python
from casdoor import CasdoorSDK
from app.core.config import settings

casdoor_sdk = CasdoorSDK(
    endpoint=settings.CASDOOR_ENDPOINT,
    client_id=settings.CASDOOR_CLIENT_ID,
    client_secret=settings.CASDOOR_CLIENT_SECRET,
    certificate=settings.CASDOOR_CERTIFICATE,
    org_name=settings.CASDOOR_ORG_NAME,
    app_name=settings.CASDOOR_APP_NAME,
)
```

### 4. 替换 `POST /api/v1/auth/callback`（原 `/login`）

```python
@router.post("/callback")
async def oauth_callback(code: str, state: str, db: Session = Depends(get_db)):
    # 1. 用 code 向 Casdoor 换 token
    token = casdoor_sdk.get_oauth_token(code)

    # 2. 解析并验签 JWT，得到用户信息
    user_info = casdoor_sdk.parse_jwt_token(token.access_token)
    casdoor_id = user_info["sub"]       # Casdoor 唯一用户 ID
    username   = user_info["name"]
    email      = user_info.get("email", "")

    # 3. 查找或初始化本地用户（首次登录自动开户）
    user = db.exec(select(User).where(User.casdoor_id == casdoor_id)).first()
    if not user:
        user = User(casdoor_id=casdoor_id, username=username, email=email,
                    cash=settings.INITIAL_CASH)
        db.add(user)
        db.commit()
        db.refresh(user)

    # 4. 签发本站 JWT（payload 包含本站 user_id）
    local_token = create_access_token({"sub": str(user.id)})
    return {"access_token": local_token, "token_type": "bearer"}
```

### 5. 修改 JWT 鉴权中间件

后端不再验证密码，但仍然自签发本站 JWT（这样接口层改动最小）。
`get_current_user` 依赖函数的逻辑不变——只是 token 来源从本地登录变成了 Casdoor 回调后签发的。

### 6. User 模型新增字段

```python
class User(SQLModel, table=True):
    casdoor_id: str = Field(unique=True, index=True)  # 新增
    # 其余字段保持不变
    # 删除: hashed_password, activation_code 相关字段
```

### 7. 删除的后端代码

- `POST /api/v1/auth/register`
- `POST /api/v1/auth/login`（用 `/callback` 替代）
- `POST /api/v1/auth/activate`
- `GET /api/v1/auth/activation-codes`（如激活码逻辑迁入 Casdoor 则删除）
- `User.hashed_password` 字段

---

## 激活码的处理

原系统用激活码限制注册。迁移后有两种方案：

**方案 A：在 Casdoor 里控制（推荐）**
在 Casdoor 的 Application 里开启「邀请码注册」功能，由 Casdoor 侧管理邀请码。
TouhouCCB 后端不再关心激活码，任何能在 Casdoor 完成注册的用户都可进入游戏。

**方案 B：在本站后端做二次校验**
Casdoor 不限制注册（任意邮箱/GitHub 均可），但用户首次登录时后端检查 `User` 是否已通过游戏内白名单。
未通过则返回 403，前端提示「账号未激活，请联系管理员」。

---

## 方案选择：透传 Casdoor token vs 本站自签 token

| | 透传 Casdoor token | 本站自签 token（推荐） |
|--|--|--|
| 接口改动 | 所有接口改用 Casdoor 公钥验签 | 只改 `/callback`，其余不动 |
| 离线验证 | 需要 Casdoor 公钥证书 | 使用本站密钥，完全离线 |
| Token 过期控制 | 跟随 Casdoor 配置 | 本站自主控制 |
| 实现复杂度 | 较低 | 低 |

本站自签方案接入成本最低，**推荐使用**。

---

## 总结：改动清单

### 前端

- [ ] 安装 `casdoor-js-sdk`，新建 `src/api/casdoor.ts`
- [ ] `Login.vue` / `Register.vue` 改为跳转按钮
- [ ] 新建 `src/pages/auth/Callback.vue`
- [ ] `routes.ts` 增加 `/auth/callback` 路由
- [ ] `.env` 填写 Casdoor 配置变量
- [ ] 删除 `Activate.vue` 和相关 api/store 逻辑

### 后端

- [ ] `pip install casdoor`，新建 `app/core/casdoor.py`
- [ ] `config.py` 增加 Casdoor 配置项
- [ ] `User` 模型增加 `casdoor_id` 字段，删除密码字段
- [ ] `auth.py` 新增 `POST /callback`，删除 `/register`、`/login`、`/activate`
- [ ] 数据库迁移

### Casdoor 端

- [ ] 创建 Application，获取 clientId / clientSecret
- [ ] 配置 Redirect URL 为 `https://thccb.example.com/auth/callback`
- [ ] 按需配置邀请码或开放注册
