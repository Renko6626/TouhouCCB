# 开源整理归档 — 2026-06-08（PR1 可公开闸门 + PR2 去私有化）

本文件记录为「整理代码与文档以便开源」所做的全部敏感/不可逆改动，供回溯。
分支：`feat/2026-06-08-oss-gate`。审计报告见记忆 `project_oss_readiness_audit.md`。

## ⚠️ 仍需所有者本人处理（代码无法代办）

1. **rotate 弹幕 HMAC 密钥**：旧密钥 `114514`（含同学真名）曾出现在 git 历史（commit `f0759f0`）。
   新共享密钥定为 `114514`（已写入 config.py 默认值）——**需通知弹幕服务合作方把他们那边的
   SECRET_KEY 也改成 `114514`**，否则双方验签不一致、兑换码失效。旧码（用 114514 签的）会失效。
   **在历史重写完成前，仓库不得 public**（历史里仍有同学真名）。
2. **确认个人信息**：git 历史含两个个人邮箱（PKU 学号邮箱 + QQ 邮箱）。如介意公开需额外处理。
3. **确认 `17.csv` 性质**：疑为未使用的真实兑换码（工作区，未入历史）。本批已 gitignore，未删除你的本地副本。
4. **favicon.ico 来源**：报告标记来源不明，开源前自行确认授权。
5. ~~**GitHub Secrets**~~ ✅ 已于 2026-06-08 用 `gh secret set` 配好：
   `VITE_API_BASE_URL` `VITE_APP_TITLE` `VITE_CASDOOR_URL` `VITE_CASDOOR_CLIENT_ID`
   `VITE_CASDOOR_ORG` `VITE_CASDOOR_APP` `ACR_NAMESPACE`（值取自本地 .env.production + docker-compose）。
   注：`VITE_APP_TITLE` 已于 2026-06-08 按所有者确认改为权威标题「东方炒炒币」（与 README/PROJECT_NAME 一致）。
6. **force-push 决定**：历史重写后推送到 live main = force-push + 触发 prod 部署。本批不自动 push，等所有者拍板。

## PR1 — 可公开闸门

- 新增 `LICENSE`（MIT，Copyright 2026 Renko6626）
- 新增根 `README.md`（含东方同人 IP 声明）
- 新增 `thccb-frontend/.env.production.example`
- `.gitignore` 硬化：移除 `!thccb-frontend/.env.production` 白名单；新增 `*.csv` `*.xlsx` `/danmuku.py` `loadtest/*.json` `texput.log` 等规则
- `git rm --cached thccb-frontend/.env.production`（停止追踪，保留本地文件）
- `backend/app/core/config.py` `DANMUKU_SECRET_KEY` 默认值 `"114514"`（含同学真名）
  → `"114514"`（占位 meme 值，无 PII）；`.env.example` 对应行注释化以让默认值生效
- 删除 `danmuku.py`（未追踪的破损片段，含明文密钥）
- **git 历史重写（最后执行，前置 backup bundle）**：用 git-filter-repo 从全历史移除
  `thccb-frontend/.env.production`，并把 `114514` 替换为 `114514`。详见文末「历史重写」节。

## PR2 — 去私有化 + CI 安全网

私有标识符 → 占位符映射（原值已脱敏，避免本归档文档自身泄漏）：
| 原值（类别） | 占位符 |
|------|--------|
| 〈生产站域名〉 | `your-instance.example.com` |
| 〈Casdoor 域名〉 | `your-casdoor.example.com` |
| 〈阿里云 ACR 镜像地址〉 | `your-registry.example.com` |
| 〈ACR namespace〉 | `your-namespace` |
| 〈服务器项目绝对路径〉 | `/path/to/TouhouCCB` |
| 〈真实 k6 压测机 IP〉 | `<your-k6-machine-ip>` |

改动文件：`docker-compose.yml`、`.github/workflows/ci.yml`、`deploy/nginx.conf`、`.env.example`、
`docs/deploy.md`、`loadtest/{README.md,scenarios/.env.example,seed/*.sh}`、
`quant/{.env.example,docs/get-token.md}`、`.claude/skills/writing-quant-strategy/SKILL.md`。

CI 安全网：
- backend job 新增 `pytest` 步骤
- deploy job + 镜像 push 步骤加 `github.repository_owner == 'Renko6626'` 守卫（fork 不再报红/误部署）
- frontend build 从 GitHub Secrets 注入 `VITE_*`（替代被移除的 `.env.production`）
- ACR 镜像 namespace 改 `${{ secrets.ACR_NAMESPACE }}`

删除（AI session 日志 / 废弃脚本，私有路径主要来源）：
- `docs/superpowers/`（AI plans/specs，含大量服务器绝对路径与作者名）
- `docs/ralph-log.md`（121KB AI 开发日志）
- `backend/market_test.py`、`backend/user_test.py`（废弃裸脚本，污染 pytest collection）

## 本批【未做】（刻意推迟，避免破坏 prod）

- `requirements.txt` 移除疑似遗留依赖（fastapi-users/pwdlib/argon2/itsdangerous 等）—— 需 pipdeptree 验证传染依赖，误删炸 build，留待专门一批
- 敏感配置改 `pydantic.SecretStr` —— 触及 auth 读取路径，需全量改用 `.get_secret_value()` 并实测登录，留待专门一批
- 文档大刀阔斧重写（PR3）、Casdoor 自建指引 + 一键 compose（PR4）、质量收尾（PR5）
- 代码注释里残留的 `docs/superpowers/specs/...` 悬空引用（约 15 文件：market.py/chart.py/anti_bot.py/
  ledger.py 等 + docs/README.md/api.md/archive/*）—— 仓库相对路径非隐私，留 PR3 注释整理时清
- AI 工具脚手架目录（`.claude/` `.clinerules/` `.codex/` `.agents/` `backend/.claude/ralph-loop.local.md`
  `CLAUDE.md`）—— 对外部用户无意义，是否随开源移除/改写留所有者定（CLAUDE.md→CONTRIBUTING 属 PR3）
- `sample-title-codes.csv` 现被 `*.csv` 规则忽略（本就未追踪）；如要留作示例数据需 `git add -f`

## 历史重写（待执行 — 公开前最后一步）

**为何尚未执行**：历史重写在「真正公开（force-push）」前零收益，却会让本地 main 与
origin 彻底分叉、破坏后续正常 push。应在 rotate 弹幕密钥后、即将公开的那一刻执行。
`git-filter-repo` 已确认安装。

执行步骤（按序）：

```bash
# 0. 前置：① 弹幕密钥已通知合作方 rotate ② 本分支已合入 main ③ 工作树干净
cd /path/to/TouhouCCB
git checkout main && git merge --ff-only feat/2026-06-08-oss-gate   # 或正常 merge
git status            # 必须 clean

# 1. 备份（关键，唯一回滚手段）
git bundle create ../TouhouCCB-backup-20260608.bundle --all

# 2. 替换规则文件（换成无害占位值，连同学真名一起从历史抹掉）
printf '114514==>114514\n' > /tmp/thccb-replacements.txt

# 3. 重写全历史：删 .env.production 文件 + 抹掉密钥字符串
git filter-repo --force \
  --path thccb-frontend/.env.production --invert-paths \
  --replace-text /tmp/thccb-replacements.txt

# 4. 验证（两条都应无输出）
git log --all --oneline -S 114514 -- backend/app/core/config.py
git log --all --oneline -- thccb-frontend/.env.production

# 5. filter-repo 会移除 origin，重新加回
git remote add origin <your-repo-url>

# 6. 公开时 force-push（⚠️ 触发 prod 部署，确认无误再执行）
git push --force origin main
```

