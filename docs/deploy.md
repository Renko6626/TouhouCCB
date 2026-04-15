# TouhouCCB 部署指南

本文档覆盖从零开始的完整部署流程：服务器环境准备 → 首次部署 → GitHub Actions CI/CD 自动化。

---

## 架构概览

```
                        ┌───────────────────────────┐
  浏览器 ──── HTTPS ──▶ │  nginx (宿主机 :80/:443)   │
                        │  - /* 静态文件 (dist/)      │
                        │  - /api/v1/* 反代           │
                        │  - 速率限制 + 安全头         │
                        └────┬─────────────────┬────┘
                             │                 │
                      /api/v1/*           / (前端)
                             │                 │
                    ┌────────▼─────────┐  ┌───▼──────────────┐
                    │  Docker 容器      │  │  thccb-frontend/  │
                    │  thccb-backend   │  │  dist/ (静态文件)  │
                    │  uvicorn :8004   │  │  CI 构建 → rsync  │
                    │       │          │  └──────────────────┘
                    │       ▼          │
                    │  thccb-postgres  │
                    │  PostgreSQL :5432│
                    │  (Docker volume) │
                    └──────────────────┘
```

- **nginx** — 宿主机运行，反向代理 + 静态文件 + HTTPS + 速率限制
- **Docker** — 后端运行在容器中，镜像由 CI 构建并推送到 GHCR
- **前端** — CI 中 `npm run build`，产物 rsync 到服务器，nginx 直接 serve
- **GitHub Actions** — push main 时：构建镜像 → 推 GHCR → rsync 前端 → SSH 部署 → 健康检查

---

## 一、服务器环境准备

### 1.1 安装基础依赖

```bash
# Ubuntu / Debian
sudo apt update
sudo apt install -y nginx git curl

# 安装 Docker（官方脚本）
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
# 重新登录让 docker 组生效
exit
# 重新 SSH 进来

# 验证
docker --version          # >= 24
docker compose version    # >= 2.20
nginx -v
```

> 不再需要在服务器上安装 Python、Node、PM2。所有构建在 CI 完成。

### 1.2 克隆项目

```bash
cd /data/sunyunbo/www
git clone https://github.com/你的用户名/TouhouCCB.git
cd TouhouCCB
```

### 1.3 登录 GHCR（如果仓库是 private）

```bash
# 在 GitHub 创建 Personal Access Token (PAT)：
#   Settings → Developer settings → Tokens → Generate new token
#   勾选 read:packages 权限

echo "你的PAT" | docker login ghcr.io -u 你的GitHub用户名 --password-stdin
```

> 如果仓库是 public，跳过此步，GHCR 镜像可直接拉取。

---

## 二、环境变量配置

### 2.1 项目 `.env`（唯一配置文件）

整个项目只需要**一个 `.env` 文件**，放在项目根目录：

- docker-compose.yml 自动读取（变量插值：PG_PASSWORD、UID/GID）
- 后端容器挂载为 `/app/.env`（pydantic-settings 读取所有配置）

```bash
cp .env.example .env
```

编辑 `.env`，必须填写的项：

```ini
# 运行环境
APP_ENV=production

# 容器 UID/GID（用 id -u && id -g 查看）
UID=1007
GID=1008

# 安全密钥
SECRET_KEY=用下面的命令生成

# 数据库
DB_BACKEND=postgres
PG_HOST=postgres
PG_PASSWORD=你的强密码

# Casdoor SSO
CASDOOR_ENDPOINT=https://你的casdoor地址
CASDOOR_CLIENT_ID=你的client-id
CASDOOR_CLIENT_SECRET=你的client-secret
CASDOOR_ORG_NAME=你的组织名
CASDOOR_APP_NAME=你的应用名

# CORS
CORS_ORIGINS=https://thccb.你的域名.com
```

> `APP_ENV=production` 时，SECRET_KEY、ADMIN_SECRET_KEY、Casdoor 配置缺失会**拒绝启动**。
>
> 不需要配置管理员密码——**第一个通过 SSO 登录的用户自动成为管理员**。

**生成 SECRET_KEY：**

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

### 2.3 前端生产环境变量

```bash
cat > thccb-frontend/.env.production <<'EOF'
VITE_API_BASE_URL=https://thccb.你的域名.com
VITE_APP_TITLE=东方炒炒币
VITE_CASDOOR_URL=https://你的casdoor地址
VITE_CASDOOR_CLIENT_ID=你的client-id
VITE_CASDOOR_ORG=你的组织名
VITE_CASDOOR_APP=你的应用名
EOF
```

---

## 三、nginx 配置

### 3.1 修改域名

编辑 `deploy/nginx.conf`，把 `thccb.example.com` 改成你的域名。

### 3.2 启用站点

```bash
sudo ln -s /data/sunyunbo/www/TouhouCCB/deploy/nginx.conf \
           /etc/nginx/sites-enabled/thccb.conf
sudo nginx -t && sudo systemctl reload nginx
```

### 3.3 HTTPS

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d thccb.你的域名.com
```

启用后，编辑 `deploy/nginx.conf` 取消 HSTS 注释：

```nginx
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
```

### 3.4 速率限制

| 区域 | 限速 | 保护目标 |
|------|------|---------|
| `api_auth` | 5 r/s | `/api/v1/auth/*` — 防暴力登录 |
| `api_trade` | 10 r/s | `/api/v1/market/buy\|sell\|quote` — 防刷单 |
| `api_admin` | 2 r/s | `/api/v1/admin/*` — 防爆破 |
| `api_general` | 20 r/s | 其余 API — 通用保护 |

---

## 四、首次部署

### 4.1 创建数据目录

```bash
mkdir -p backend/data backups
```

### 4.2 拉取镜像并启动

```bash
cd /data/sunyunbo/www/TouhouCCB
docker compose pull
docker compose up -d
```

### 4.3 初始化数据库

```bash
docker compose exec backend python init_db.py
```

按提示输入 `YES`。这会创建所有表和一个示例市场。

> 不需要手动创建管理员——**第一个通过 SSO 登录的用户自动成为管理员（superuser）**。
>
> 之后的部署不需要再跑 init_db.py。容器启动时 `create_all` 是幂等的。

### 4.4 构建前端（首次手动）

首次部署还没有 CI，需要手动构建：

```bash
cd thccb-frontend
npm ci && npm run build
cd ..
sudo nginx -t && sudo systemctl reload nginx
```

> 后续由 CI 自动完成，不再需要在服务器上装 Node。

### 4.5 验证

```bash
# 后端
curl http://127.0.0.1:8004/
# 返回：{"message":"欢迎来到大天狗交易所","docs":"/docs"}

# 容器状态
docker compose ps
# thccb-backend   running (healthy)

# 前端
curl -I https://thccb.你的域名.com/
# 返回 200
```

---

## 五、后续部署

### 5.1 自动部署（推荐）

push 到 main 后，GitHub Actions 自动完成一切：

```
push main
  → CI 构建 Docker 镜像，推送到 GHCR
  → CI 构建前端 dist/，rsync 到服务器
  → SSH 执行 deploy.sh：
      备份数据库 → docker compose pull → up -d → 健康检查
  → CI 远程验证后端存活
```

配置方法见第六节。

### 5.2 手动部署

```bash
cd /data/sunyunbo/www/TouhouCCB
bash deploy/deploy.sh
```

deploy.sh 自动完成：

```
[0/4] 环境校验 — 检查 .env、Docker
[1/4] 备份数据库 — 复制到 backups/，保留最近 10 个
[2/4] 拉取新镜像 — docker compose pull
[3/4] 重启容器 — docker compose up -d（8 秒优雅停机）
[4/4] 健康检查 — 6 次重试，失败自动回滚到旧镜像
```

### 5.3 回滚

```bash
# 查看可用的历史镜像 tag
docker images ghcr.io/renko6626/thccb-backend

# 回滚到指定版本
docker compose pull   # 如果需要先拉旧镜像
# 或者直接用本地缓存的旧镜像：
docker tag ghcr.io/renko6626/thccb-backend:<旧sha> ghcr.io/renko6626/thccb-backend:latest
docker compose up -d
```

### 5.4 数据库恢复

**PostgreSQL（默认）：**

```bash
docker compose stop backend
# 从 SQL 备份恢复（会覆盖现有数据）
docker compose exec -T postgres psql -U thccb -d thccb < backups/thccb_想恢复的时间戳.sql
docker compose start backend
```

**SQLite：**

```bash
docker compose stop backend
cp backups/thccb_想恢复的时间戳.db backend/data/thccb.db
docker compose start backend
```

---

## 六、GitHub Actions CI/CD

### 6.1 工作原理

```
push main
  │
  ├── Backend Check & Build
  │   ├── py_compile 语法检查
  │   ├── import check（所有模块能加载）
  │   └── Docker build → push ghcr.io/.../thccb-backend:latest
  │
  └── Frontend Check & Build
      ├── vue-tsc 类型检查
      ├── npm run build
      └── upload dist/ artifact
  │
  ▼ 两个都通过
  Deploy
  ├── rsync dist/ 到服务器
  ├── SSH: bash deploy/deploy.sh
  └── SSH: curl 健康检查
```

CI/CD 用到的凭证全部存在 GitHub Secrets 里（加密存储），代码和日志中不可见。你需要配置 3 个 Secret + 1 个权限。下面逐步说明。

### 6.2 在服务器上准备

#### 6.2.1 生成部署专用 SSH 密钥

在你的部署用户下执行（比如 `deploy` 用户）：

```bash
# 生成密钥对（不设密码，GitHub Actions 不能输密码）
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/github_deploy -N ""

# 把公钥加入 authorized_keys（允许用这个私钥登录）
cat ~/.ssh/github_deploy.pub >> ~/.ssh/authorized_keys

# 修正权限（SSH 对权限很敏感）
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys

# 查看私钥 — 下一步要完整复制到 GitHub
cat ~/.ssh/github_deploy
```

终端会输出类似：

```
-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUA...
（很多行 base64）
-----END OPENSSH PRIVATE KEY-----
```

**完整复制这段内容**，包括第一行 `-----BEGIN...` 和最后一行 `-----END...`。

#### 6.2.2 配置 sudo 免密

deploy.sh 中 nginx reload 需要 sudo，但 SSH 自动登录时不能输密码：

```bash
echo "deploy ALL=(ALL) NOPASSWD: /usr/sbin/nginx, /bin/systemctl reload nginx" \
  | sudo tee /etc/sudoers.d/thccb-deploy
```

> 把 `deploy` 替换成你的实际用户名。这只给 nginx 两条命令免密，不影响其他 sudo。

验证：

```bash
sudo -n nginx -t    # 不弹密码提示直接执行 = 配置正确
```

### 6.3 在 GitHub 配置 Secrets（图文步骤）

#### 第一步：进入 Secrets 配置页

1. 打开你的 GitHub 仓库页面（比如 `github.com/Renko6626/TouhouCCB`）
2. 点击顶部标签栏的 **Settings**（齿轮图标，在 Code / Issues / Pull requests 右边）
3. 左侧菜单滚动找到 **Secrets and variables**，点击展开
4. 点击 **Actions**

```
仓库页面
  └── Settings（顶部标签栏）
        └── 左侧菜单：Secrets and variables
              └── Actions
                    └── 你在这里
```

#### 第二步：逐个添加 3 个 Secret

点击绿色按钮 **New repository secret**，添加以下 3 个：

**Secret 1：DEPLOY_HOST**

```
Name:   DEPLOY_HOST
Secret: 123.45.67.89          ← 你服务器的公网 IP 地址
```

点击 **Add secret**。

**Secret 2：DEPLOY_USER**

```
Name:   DEPLOY_USER
Secret: deploy                 ← 你服务器上的部署用户名
```

点击 **Add secret**。

**Secret 3：DEPLOY_KEY**

```
Name:   DEPLOY_KEY
Secret:                        ← 粘贴 6.2.1 步复制的私钥完整内容
```

把之前复制的 `-----BEGIN OPENSSH PRIVATE KEY-----` 到 `-----END OPENSSH PRIVATE KEY-----` 整段粘贴进去。

点击 **Add secret**。

添加完成后，页面应该显示 3 个 secret：

```
DEPLOY_HOST    Updated just now
DEPLOY_KEY     Updated just now
DEPLOY_USER    Updated just now
```

> Secret 的值添加后就不可查看了（只能覆盖更新），这是正常的安全设计。

#### 第三步：配置 Workflow 权限（允许推送 Docker 镜像）

CI 需要把 Docker 镜像推送到 GitHub Container Registry（GHCR），需要给 workflow 写权限：

1. 还是在 **Settings** 页面
2. 左侧菜单找到 **Actions** → **General**
3. 滚动到页面底部 **Workflow permissions** 区域
4. 选择 **Read and write permissions**（不是默认的 Read-only）
5. 点击 **Save**

```
Settings
  └── Actions → General
        └── 滚动到底部
              └── Workflow permissions
                    ☑ Read and write permissions    ← 选这个
                    ☐ Read repository contents and packages permissions
```

> 这让 CI 中的 `GITHUB_TOKEN` 有权限推送镜像到 ghcr.io。不需要手动创建 PAT。

#### 第四步：如果仓库是 Private，配置 GHCR 包可见性

如果你的仓库是 **private**，推送后的 Docker 镜像默认也是 private，服务器 pull 时需要认证。

有两种方式（选一种）：

**方式 A：在服务器上 docker login（推荐，简单）**

```bash
# 在 GitHub 创建 Personal Access Token：
#   github.com → Settings → Developer settings → Personal access tokens → Tokens (classic)
#   点 Generate new token (classic)
#   勾选 read:packages
#   生成后复制 token

# 在服务器上登录
echo "ghp_你的token" | docker login ghcr.io -u Renko6626 --password-stdin
```

**方式 B：把仓库改为 Public**

如果项目本身是开源的，直接把仓库设为 Public 即可，GHCR 镜像自动可公开拉取。

### 6.4 验证 CI/CD

所有配置完成后，做一次测试：

```bash
# 本地随便改个文件，push 触发 CI
echo "" >> docs/deploy.md
git add -A
git commit -m "test: trigger CI/CD"
git push origin main
```

然后去 GitHub 仓库的 **Actions** 标签页（仓库顶部标签栏，在 Pull requests 右边）：

```
应该看到一个运行中的 workflow，包含 3 个 job：

✅ Backend Check & Build     ← 编译检查 + Docker 镜像构建推送
✅ Frontend Check & Build    ← TypeScript 检查 + 构建 + 上传产物
✅ Deploy                     ← rsync 前端 + SSH 部署 + 健康检查
```

点击任意 job 可以展开看实时日志。如果某一步红了，看日志定位问题。

### 6.5 完整配置检查清单

全部配好后，用这个清单确认没有遗漏：

```
服务器端：
  [ ] 部署用户已创建并加入 docker 组
  [ ] SSH 密钥已生成，公钥已加入 authorized_keys
  [ ] sudo 免密已配置（nginx -t 和 systemctl reload）
  [ ] .env 已配置（cp .env.example .env 并填好）
  [ ] docker compose up -d 能正常启动
  [ ] 如果 private 仓库：已 docker login ghcr.io

GitHub 端：
  [ ] Secret: DEPLOY_HOST（服务器公网 IP）
  [ ] Secret: DEPLOY_USER（部署用户名）
  [ ] Secret: DEPLOY_KEY（SSH 私钥完整内容）
  [ ] Settings → Actions → General → Workflow permissions → Read and write
  [ ] push main 后 Actions 页面 3 个 job 全绿
```

### 6.6 常见问题

**Q: Docker 镜像推送报 403 Forbidden？**

→ Workflow permissions 没有设为 "Read and write"。按 6.3 第三步操作。

**Q: Docker 镜像推送报 "repository name must be lowercase"？**

→ 已修复。CI 中有自动转小写步骤。如果仍然报错，检查 ci.yml 是否是最新版本。

**Q: Deploy job 报 SSH 连接超时？**

检查：
- `DEPLOY_HOST` 是否填的公网 IP（不是内网 IP 如 192.168.x.x）
- 服务器防火墙是否放行了 22 端口：`sudo ufw status` 或 `sudo iptables -L`
- SSH 服务是否运行：`systemctl status sshd`

**Q: Deploy job 报 "Permission denied (publickey)"？**

检查：
- `DEPLOY_KEY` 是否完整复制（包括 BEGIN/END 行，没有多余空行）
- `DEPLOY_USER` 是否与服务器上生成密钥的用户一致
- 服务器上 `~/.ssh/authorized_keys` 权限是否 600
- 验证方法：在本地用同一私钥手动测试
  ```bash
  ssh -i /path/to/private_key deploy@你的IP
  ```

**Q: rsync 报 "Permission denied"？**

→ `DEPLOY_USER` 对项目目录没有写权限。执行：
```bash
sudo chown -R deploy:deploy /data/sunyunbo/www/TouhouCCB
```

**Q: deploy.sh 报 "SECRET_KEY is still the default value"？**

→ 服务器上的 `.env` 里 SECRET_KEY 还是模板值。生成并填入：
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

**Q: 健康检查失败？**

```bash
# SSH 到服务器查看
docker compose logs --tail 50 backend
docker compose ps
```

常见原因：
- `.env` 中 `APP_ENV=production` 但缺少必填项 → 容器启动失败
- PostgreSQL 还没初始化 → 先跑 `docker compose exec backend python init_db.py`
- 端口被占用 → `sudo lsof -i :8004`

**Q: PR 会触发部署吗？**

不会。CI 文件中 deploy job 有条件限制：`if: github.ref == 'refs/heads/main' && github.event_name == 'push'`。PR 只跑检查，不部署。

**Q: 怎么只跑 CI 检查不部署？**

发 PR 而不是直接 push main。PR 的 CI 只跑 backend check + frontend check，通过后 merge 到 main 才触发部署。

---

## 七、日常运维

### 查看日志

```bash
docker compose logs -f backend          # 实时日志
docker compose logs --tail 100 backend  # 最近 100 行
```

### 查看容器状态

```bash
docker compose ps       # 运行状态 + 健康检查
docker stats            # 实时资源占用
```

### 进入容器排查

```bash
docker compose exec backend bash
```

### 清理旧镜像

```bash
docker image prune -f   # 清理悬空镜像
```

### SSH 密钥轮换

建议每 3-6 个月轮换一次，步骤见服务器生成新密钥 → 更新 GitHub Secret → 移除旧密钥。

---

## 八、从旧版（PM2）迁移

如果你之前使用 PM2 部署，切换步骤：

```bash
# 1. 确认 Docker 版本部署正常
docker compose ps   # 应该显示 healthy

# 2. 停止并移除 PM2 进程
pm2 delete thccb-backend
pm2 save

# 3. 清理旧文件（可选）
rm -rf backend/venv
# 如果服务器不再需要 Node（前端已由 CI 构建）：
# sudo npm uninstall -g pm2
```

---

## 九、项目文件结构

```
TouhouCCB/
├── .env                         # 唯一配置文件（Docker + 后端共用，不提交）
├── .env.example                 # ↑ 模板
├── .github/workflows/ci.yml    # CI/CD（Docker 构建 + rsync + 部署）
├── docker-compose.yml           # Docker Compose 服务定义（backend + postgres）
├── deploy/
│   ├── nginx.conf               # nginx 站点配置（速率限制）
│   └── deploy.sh                # 部署脚本（备份 + pull + 健康检查）
├── backups/                     # 数据库自动备份（不提交）
├── backend/
│   ├── Dockerfile               # 后端 Docker 镜像定义
│   ├── .dockerignore
│   ├── requirements.txt
│   └── app/                     # FastAPI 应用代码
├── thccb-frontend/
│   ├── .env.production          # 前端生产环境变量（不提交）
│   ├── .env.example
│   ├── dist/                    # CI 构建产物（rsync 到服务器，不提交）
│   └── src/                     # Vue 源码
└── docs/
    └── deploy.md                # 本文档
```
