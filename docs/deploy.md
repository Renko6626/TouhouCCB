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

# Admin 后台
ADMIN_PASSWORD_HASH=用下面的命令生成
```

> `APP_ENV=production` 时，SECRET_KEY、ADMIN_SECRET_KEY、Casdoor 配置缺失会**拒绝启动**。

**生成 SECRET_KEY：**

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

**生成 ADMIN_PASSWORD_HASH：**

```bash
docker run --rm python:3.13-slim bash -c \
  "pip install -q bcrypt && python -c \"import bcrypt; print(bcrypt.hashpw(b'你的密码', bcrypt.gensalt()).decode())\""
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

按提示输入 `YES`。这会创建所有表 + 管理员账号 + 示例市场。

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

### 6.2 在服务器上生成部署密钥

```bash
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/github_deploy -N ""
cat ~/.ssh/github_deploy.pub >> ~/.ssh/authorized_keys
chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys

# 复制私钥 → 填到 GitHub DEPLOY_KEY
cat ~/.ssh/github_deploy
```

### 6.3 sudo 免密（nginx reload）

```bash
echo "$USER ALL=(ALL) NOPASSWD: /usr/sbin/nginx, /bin/systemctl reload nginx" \
  | sudo tee /etc/sudoers.d/thccb-deploy
```

### 6.4 在 GitHub 配置 Secrets

仓库 → Settings → Secrets and variables → Actions → New repository secret：

| Secret 名称 | 内容 |
|---|---|
| `DEPLOY_HOST` | 服务器公网 IP |
| `DEPLOY_USER` | SSH 登录用户名 |
| `DEPLOY_KEY` | 6.2 步的私钥全文 |

> 不需要额外配置 GHCR 凭证——CI 使用内置的 `GITHUB_TOKEN` 推送镜像。

### 6.5 验证

```bash
git add -A && git commit -m "test: trigger CI/CD" && git push origin main
```

去 GitHub Actions 页面查看，应该看到 3 个绿色 job。

### 6.6 常见问题

**Q: Docker 镜像推送失败？**

检查仓库 Settings → Actions → General → Workflow permissions 是否勾选了 "Read and write permissions"。

**Q: 服务器 docker compose pull 报 unauthorized？**

仓库是 private 时需要在服务器上 `docker login ghcr.io`，见 1.3 节。

**Q: rsync 报 permission denied？**

确保 `DEPLOY_USER` 对 `/data/sunyunbo/www/TouhouCCB/thccb-frontend/dist/` 有写权限。

**Q: 健康检查失败？**

```bash
# 查看容器日志
docker compose logs --tail 50 backend
# 查看容器状态
docker compose ps
```

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
