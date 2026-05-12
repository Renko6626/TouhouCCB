# TouhouCCB 安全审计设计（2026-05-08）

> 本文档是"做一次"的安全审计设计 spec。审计执行将通过 writing-plans 拆分为可执行计划，然后按阶段在独立 ralph 轮次中推进。

## 0. 一句话目标

对生产中的 TouhouCCB（FastAPI + Postgres + Vue 3 + Casdoor SSO）做一次**只读代码 + 静态工具**的安全审计，覆盖业务核心/认证授权、传统 web 漏洞、DoS/性能瓶颈三个层面，最终交付**分阶段报告 + 按 P0–P3 排序的可执行 issue 清单**。审计本身**不改代码、不部署**。

## 1. 范围

### 1.1 In-scope

**后端**

- 业务核心：`backend/app/services/lmsr.py`、`loan_service.py`、`loan_sweep.py`、`redemption.py`、`realtime.py`
- 关键 API：`api/v1/{auth,market,loan,redemption,admin_redemption,stream,chart,user,site_config}.py`
- 核心模块（**只读**，不改逻辑）：`core/{config,users,oidc,admin}.py`、`models/base.py`
- 入口与中间件：`main.py`、限速配置

**前端**

- `stores/`、`src/api/`、`router/`、`pages/`、`components/`（含 `v-html` / 动态渲染）
- `vite.config.ts`、`uno.config.ts`

### 1.2 Out-of-scope

- 依赖 CVE 扫描（`pip-audit` / `npm audit`）
- 基础设施配置审计（`docker-compose.yml`、`deploy/nginx`、`.github/workflows/`、`.env*`）
- 生产环境主动探测 / DAST / 渗透
- 威胁建模、合规清单对照（GDPR/PCI 等）
- 备份策略与灾难恢复
- 主版本依赖升级评估

## 2. 方法论

| 维度 | 工具/手段 |
|---|---|
| Python 静态分析 | `bandit -r backend/app -ll`；`semgrep --config=p/python --config=p/owasp-top-ten --config=p/security-audit backend/` |
| 前端静态分析 | `eslint-plugin-security`（如未装则临时本地装，**不污染 package.json**）；grep `v-html` / `innerHTML` / `eval` / `new Function` |
| 资金/精度 | 手动审 `Decimal` 量化、`ROUND_*`、序列化边界、入参 cast |
| 鉴权矩阵 | 列出每条受保护路由 → 校验装饰器 → 推 IDOR/水平越权用例 |
| 限速覆盖 | 列出所有写路由 → 比对 nginx 限速 → 找漏网 |
| 慢查询/N+1 | 手动审 ORM，重点：图表重放、持仓估值、admin 列表分页 |
| SSE 攻击面 | 连接生命周期、广播 fan-out 上界、断连风暴、未鉴权事件泄漏 |
| 锁/事务 | market 买卖 → 余额扣减 → LMSR 状态写入 的事务边界与并发模型 |

工具产生的告警**逐条人工 triage**，不原样写进报告。

## 3. 风险评级

自定义 P0–P3，比 CVSS 更轻量，对齐项目实际：

- **P0** 资金可被未授权直接窃取/伪造；admin 完全沦陷；全员登录瘫痪
- **P1** 单用户严重越权；DoS 可瘫痪生产；持仓估值/价格可被操纵；任意 SSE 信息泄漏
- **P2** 信息泄漏（非敏感 PII）；可被绕过的限速；可恢复的性能退化；自反射 XSS
- **P3** 加固性建议（缺安全头、错误信息可改进、依赖暴露版本）

每条 issue 必含字段：**位置（file:line）、类别、复现/PoC、影响、修复建议、等级**。

## 4. 三阶段执行计划

每阶段 = 一个独立 ralph 轮次 + 一个独立分支 + 一份阶段报告。

### 4.1 阶段 1 — 业务核心 + 认证授权

**分支**：`ralph/2026-05-09-secaudit-p1-core`
**重点文件**：`services/lmsr.py`、`api/v1/{auth,market,loan,redemption,admin_redemption,user}.py`、`core/{users,oidc,admin}.py`、`models/base.py`

Checklist：

- [ ] **LMSR 数值安全**：定价边界、负份额、零流动性、Decimal 量化点是否一致；价格被 0/极小值除的路径
- [ ] **资金一致性**：buy/sell/quote 的事务原子性；并发买卖能否产生负余额、负份额、价格抢跑套利
- [ ] **持仓估值**：清算价值是否含手续费+滑点（`4a49d2e` 修正点）；Decimal→Number 是否在边界丢精度
- [ ] **贷款复利**：repay 的双封顶（`60847ad`）；利率/到期/sweep 的边界态、负 cash 防御
- [ ] **兑换码**：码生成熵、码—批次—资金流水审计表三方一致性、双花/重放
- [ ] **SSO/Casdoor**：state/nonce 校验、回调 URL 白名单、token 校验签名/issuer/audience、过期处理
- [ ] **首位 admin 自动晋升**：竞态下能否让两人同时拿到 admin
- [ ] **admin gate**：每条 `admin_*` 路由是否走 `require_admin`（grep + 路由清单比对）
- [ ] **IDOR**：所有按 ID 查询的接口（持仓/订单/兑换/贷款）→ 是否校验 ownership
- [ ] **`models/base.py`**：CLAUDE.md 提及"无迁移机制"——审字段近期变更与 `create_all` 不动旧列的实际后果
- [ ] **静态扫**：`bandit` + `semgrep p/python p/owasp-top-ten`，triage

### 4.2 阶段 2 — 传统 Web 漏洞

**分支**：`ralph/2026-05-1X-secaudit-p2-web`
**重点**：跨整个 `backend/api/` + `frontend/src/`

Checklist：

- [ ] **SQL 注入**：grep 原生 SQL（`text(`、`execute(`、f-string SQL）；ORM `filter(... == request.X)` 但 X 未类型校验
- [ ] **前端 XSS**：grep `v-html`、`innerHTML`、未净化富文本（公告、市场描述、用户昵称等）
- [ ] **CSRF**：SSO 回调 state 是否绑定会话；写接口的 cookie/token 模式是否易被跨站触发
- [ ] **SSRF**：任何接受 URL 的字段（兑换码导入？头像？富文本？）→ 内网/`file://` 拦截
- [ ] **路径穿越**：文件读写路径拼接（如有上传/导出）
- [ ] **开放重定向**：登录回跳、SSO 回调 next 参数白名单
- [ ] **错误信息泄漏**：500 是否回栈、SQLAlchemy 异常是否裸露表名
- [ ] **CORS**：`allow_origins` 配置；是否 `*` 配 `allow_credentials=True`
- [ ] **Cookie/Token 属性**：HttpOnly / Secure / SameSite / 过期
- [ ] **HTTP 安全头**：CSP、X-Frame-Options、X-Content-Type-Options（nginx 层不动，但列出建议）
- [ ] **静态扫**：`semgrep p/security-audit`、前端 `eslint-plugin-security`，triage

### 4.3 阶段 3 — DoS / 性能瓶颈

**分支**：`ralph/2026-05-2X-secaudit-p3-dos`
**重点**：限速配置、热路径、SSE、慢查询

Checklist：

- [ ] **限速覆盖审计**：所有写路由 vs `auth 5r/s` `market 10r/s` `admin 2r/s`，找漏网
- [ ] **限速绕过**：换 IP/Cookie/User 重置桶；SSE 不走限速会不会成为侧门
- [ ] **SSE 资源膨胀**：单用户连接数上限、广播 fan-out 上界、慢消费者背压、断连重连风暴
- [ ] **慢查询/N+1**：图表逐笔重放（`chart.py`）的复杂度；持仓估值是否对每个 holding 跑完整 LMSR 重算；admin 列表分页是否有上限
- [ ] **锁/事务范围**：market buy/sell 的事务包不包外部调用；长事务 → 连接池耗尽
- [ ] **重计算缓存**：LMSR/价格热路径是否有可缓存项被反复算
- [ ] **大对象响应**：未分页的 list 接口；交易记录、admin 报表的最大返回行数
- [ ] **登录前可达接口**：未鉴权路由的输入是否有大小/复杂度上限（zip bomb 风格）
- [ ] **依赖压测结论**：参考 `loadtest/` 200 VU 报告，结合代码复盘瓶颈

每阶段产出：`docs/ralph-log.md` 一条 + `docs/security-audit-2026-05-XX-pN.md` 阶段报告。

## 5. 交付物

```
docs/
├── security-audit-2026-05-XX-p1-core.md      # 阶段 1 原始报告
├── security-audit-2026-05-XX-p2-web.md       # 阶段 2 原始报告
├── security-audit-2026-05-XX-p3-dos.md       # 阶段 3 原始报告
├── security-audit-2026-05-XX-summary.md      # 总报告（合并 + 执行摘要）
└── security-audit-2026-05-XX-issues.md       # P0→P3 排序的可执行 issue 清单
```

### 5.1 issue 条目模板

```markdown
### [P1] LMSR 价格在零流动性时除零
- **位置**：backend/app/services/lmsr.py:128
- **类别**：业务核心 / 数值安全
- **复现**：当 b=0 时 quote() 触发 DivisionByZero
- **影响**：可让 quote 接口 500，被作为侧门探测系统
- **修复建议**：入口加 b > MIN_LIQUIDITY 校验，或回退到固定价
- **状态**：未修复
```

### 5.2 总报告骨架

- 执行摘要（数字 + 一句话总结）
- 按等级聚合的发现
- 按子系统聚合的发现
- 已知但不在范围的风险
- 后续修复路径建议（哪些可立刻 fix、哪些需要架构变更）

## 6. 护栏

- 每阶段独立 ralph 分支，**不在 main 直接提交**
- **不 push**（push = 自动部署）
- **不动**：`.env*`、`backups/`、`backend/data/`、`docker-compose.yml`、`deploy/`、`.github/workflows/`、`init_db.py`、`core/{config,users,oidc,admin}.py` 的逻辑（**只读审**）
- **不跑**：`init_db.py`、任何 `DROP`/`TRUNCATE`、`docker compose down -v`
- 高敏感文件（`lmsr.py` / `realtime.py` / `market.py` / `auth.py` / `models/base.py`）审计阶段**只读，不改**
- **审计期间不修复**——即使发现 P0 也仅打报告 + 单独问用户是否起补丁轮次（与"交付=报告+issue 清单"一致）
- 静态工具产生的告警**人工 triage 后**才进报告，不直接 commit 工具原始输出
- 每轮 `docs/ralph-log.md` 追加一条
- **不升级依赖**做对比；CVE 扫描已 out-of-scope
- 跑工具时若需临时装包：本地 venv 或临时全局，**不污染** `requirements.txt` / `package.json`

## 7. 工作量预估（参考，不绑死）

- 阶段 1（业务核心+认证）：**最重**，1-2 个 ralph 轮次
- 阶段 2（传统 web）：1 轮
- 阶段 3（DoS/性能）：1 轮
- 总报告 + issue 清单合并：1 个轻量轮次

合计 **4-5 个 ralph 轮次**。每轮自然停下交付，可单独验收。

## 8. 明确不做

- 不做依赖 CVE 扫描
- 不做基础设施配置审计
- 不做生产环境主动探测
- 不做威胁建模（留给后续）
- 不做合规清单对照（GDPR/PCI 等）
- 不做主版本依赖升级评估

## 9. 后续

设计获批后，调用 `superpowers:writing-plans` 把每个阶段（特别是阶段 1）拆成可执行的实施计划文档（`docs/superpowers/plans/`），交给 ralph 轮次执行。
