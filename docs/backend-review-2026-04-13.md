# TouhouCCB 后端代码审阅报告

> 审阅日期：2026-04-13
> 审阅范围：backend/app 全部模块（认证、交易、基础设施）

---

## 一、认证与用户管理 (auth.py / users.py / config.py / admin.py)

### CRITICAL

1. **硬编码 SECRET_KEY** — `config.py:35`
   - `SECRET_KEY = "GENSOKYO_SECRET_REIMU_IS_POOR_999"`，任何人可伪造 JWT

2. **硬编码管理员密码** — `admin.py:15`
   - `password == "nailoong"` 明文比较，无哈希

3. **Casdoor OAuth2 state 参数未验证** — `auth.py:37`
   - `state: Optional[str] = None` 接收但从不使用，CSRF 风险

4. **异常信息泄露** — `auth.py:52`
   - `detail=f"Casdoor 认证失败: {e}"` 暴露内部错误

### HIGH

5. **Admin panel secret_key 硬编码** — `admin.py:63`
   - `AdminAuth(secret_key="super_secret_key_123")`

6. **JWT 过期时间 7 天** — `config.py:37`
   - `ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7`，金融应用应 1 小时

7. **JWT 验证 except Exception 过宽** — `users.py:33-37`
   - 应分别捕获 ExpiredSignatureError / InvalidTokenError

8. **CORS 硬编码 + allow_methods=\*** — `main.py:18-29`
   - 应从配置读取，methods 应明确列出

9. **MySQL 默认密码 "change_me"** — `config.py:20`
   - 无生产环境校验

### MEDIUM

10. **用户名冲突检查循环查询** — `auth.py:65-72`
    - while 循环中逐个查，应用唯一约束 + IntegrityError 处理

11. **Admin session token 无有效性验证** — `admin.py:25`
    - 只检查 token 是否存在，不验证过期/签名

12. **JWT 缺少 iat/iss 声明** — `users.py:24`

13. **配置项缺少验证器** — `config.py`
    - SECRET_KEY 无最小长度、CASDOOR_ENDPOINT 无格式校验

14. **无请求日志 / 无速率限制** — 全局

---

## 二、市场交易核心 (market.py / models/base.py / lmsr.py)

### CRITICAL

1. **交易操作缺少 SELECT FOR UPDATE，严重竞态条件** — `market.py:370-415`
   - `db.get(User, user.id)` 无锁，变量名叫 `locked_user` 但没有锁
   - 并发请求可绕过余额检查，导致负余额
   - 卖出（500-550行）和结算流程同样缺锁

2. **金融计算使用 float** — `models/base.py` 全部, `market.py` 全文
   - cash/debt/total_shares/amount/cost 全部是 float
   - `1e-12` epsilon 是 ad-hoc 方案，长期会累积误差
   - 建议存储层用 Decimal(20,8)，LMSR 计算层用 float 后截断

3. **结算无防重复** — `market.py:159-256`
   - settle_market 无幂等检查，重复调用会双倍赔付
   - 未锁定市场行（无 with_for_update）

### HIGH

4. **Quote 不检查市场状态** — `market.py:744-798`
   - 已结算/熔断的市场仍可获取报价，误导用户

5. **Quote 卖出允许负数份额** — `market.py:780-787`
   - `new_q[idx] -= shares` 可能变负，LMSR 计算崩溃

6. **Position 无唯一约束** — `models/base.py`
   - 缺 `UniqueConstraint('user_id', 'outcome_id')`
   - 并发买入可创建重复 Position 记录

7. **User.cash / Position.amount 无 CHECK >= 0 约束** — `models/base.py`
   - 仅应用层检查，竞态条件可绕过

8. **LMSR 缺少边界检查** — `lmsr.py:4-17`
   - 未处理空列表、b<=0、NaN/Inf

9. **Resolve 端点逐用户锁定（N+1）** — `market.py:689-714`
   - 1000 用户结算 = 1000 次 SELECT FOR UPDATE
   - 应批量 `WHERE id IN (...) FOR UPDATE`

### MEDIUM

10. **MarketCreate schema 无字段限制** — `schemas/base.py`
    - title/description 无长度限制，outcomes 无最大数量
    - 建议 `min_length=2, max_length=20` 限制选项数

11. **排行榜只按 cash-debt 排序，不含持仓价值** — `market.py:852-878`
    - 与 user summary 的 net_worth 计算不一致

12. **API 响应格式不一致**
    - 有的返回 `{"status": "success"}`，有的返回 `{"message": "..."}`

13. **缺少分页** — `/list` 返回所有市场

14. **结算时间戳不一致** — `market.py:170,230,687`
    - 多次调用 `datetime.now()`，应在事务外声明一次

15. **settle vs resolve 两个重复端点** — `market.py:159,585`

16. **买卖锁定顺序不同** — `market.py:384,492`
    - Buy: Outcome→Market→User→Position; Sell: Position→Outcome→Market→User
    - 极端情况下可能死锁

### LOW

17. **索引缺失** — `models/base.py`
    - 缺 `(outcome_id, created_at)` 复合索引 (Transaction)
    - 缺 `(user_id, outcome_id)` 索引 (Position)
    - 排行榜 `(cash - debt)` 表达式排序无法使用索引

18. **Transaction 缺 market_id 字段**
    - 查市场交易需要 3 表 JOIN

19. **ForeignKey 未指定 ondelete 策略**

20. **价格计算代码重复 5+ 处**
    - `shares_list = [float(o.total_shares) for o in outcomes]` 应提取函数

---

## 三、基础设施 (database.py / stream.py / chart.py / main.py)

### CRITICAL

1. **SSE Broker 队列无 maxsize / 无连接数限制** — `stream.py:12-36`
   - `asyncio.Queue()` 无大小限制，慢消费者导致内存泄漏
   - 无全局连接数限制，可被 DoS

2. **SSE 连接无最大持续时间** — `stream.py:87-115`
   - `while True` 无限循环，连接永不主动关闭
   - 建议 1 小时强制断开

### HIGH

3. **SQLite 连接池无配置** — `database.py:22-27`
   - SQLite 引擎缺 `pool_pre_ping`、`connect_args`
   - 缺 `pool_timeout` 配置

4. **缺少数据库迁移 (Alembic)** — `init_db.py`
   - `create_all` 只能建新表，无法改已有表
   - 生产环境模式变更会丢数据

5. **K线 fill 补齐可能爆内存** — `chart.py:280-289`
   - 1年 + 1秒间隔 = 31,536,000 条空 Candle
   - 应在 fill 前校验 bucket 数量

6. **CORS 未从配置读取** — `main.py:18-29`
   - 生产部署必须改代码

### MEDIUM

7. **用户资产 N+1 查询** — `user.py:54-76`
   - 每个持仓都查该市场全部 outcomes
   - 应按 market_id 缓存

8. **SSE publish 队列满时静默丢弃** — `stream.py:33-36`
   - `except QueueFull: pass` 无日志，无慢消费者剔除

9. **requirements.txt 依赖树未锁定**
   - 顶层依赖已锁版本，但传递依赖未固定
   - 建议用 pip-tools 或 Poetry

10. **缺少请求日志中间件** — `main.py`
    - 无 IP/路径/耗时记录

11. **`expire_on_commit=False` 可能导致脏读** — `database.py:34`
    - commit 后 ORM 对象属性仍引用旧数据

12. **用户称号阈值硬编码** — `user.py:23-30`
    - 应移到配置文件

13. **`on_event("startup")` 已弃用** — `main.py:32-36`
    - FastAPI 0.93+ 推荐 lifespan

### LOW

14. **init_db.py 用 `input()` 交互** — 容器部署会卡住
15. **缺少 /health 健康检查端点**
16. **SSE 心跳 15s 可能被 Nginx 默认 30s 超时截断**
17. **User.positions 用 `lazy="selectin"` 可能隐式加载大量数据**

---

## 四、优先级汇总

| 等级 | 数量 | 关键问题 |
|------|------|---------|
| CRITICAL | 6 | 交易无锁竞态、float金融计算、重复结算、硬编码密钥、SSE内存泄漏 |
| HIGH | 12 | JWT过宽、Quote无校验、Position无唯一约束、无CHECK约束、无Alembic、K线爆内存 |
| MEDIUM | 16 | N+1查询、Schema无限制、排行榜不含持仓、日志缺失、CORS硬编码 |
| LOW | 8 | 索引缺失、代码重复、心跳间隔、init交互式 |

## 五、建议立即修复清单

### 本周必须
1. SECRET_KEY / 管理员密码 从环境变量读取
2. 交易操作加 `SELECT FOR UPDATE`
3. Position 加 `UniqueConstraint('user_id', 'outcome_id')`
4. User.cash / Position.amount 加 `CHECK >= 0`
5. settle_market 加幂等检查
6. SSE Queue 加 maxsize + 全局连接数限制
7. Quote 检查市场状态 + 负数份额校验

### 下个迭代
8. 金融字段 float → Decimal
9. 集成 Alembic 数据库迁移
10. 添加请求日志中间件
11. K线 fill 加 bucket 数量上限
12. Resolve 批量锁定用户

---

## 六、已修复问题（2026-04-13）

| 问题 | 修复文件 | 说明 |
|------|----------|------|
| CRITICAL: SECRET_KEY 硬编码 | config.py | 改为环境变量，未配置时自动生成+警告 |
| CRITICAL: 管理员密码硬编码 | admin.py | 改为从 ADMIN_PASSWORD_HASH 读取 bcrypt 哈希 |
| CRITICAL: Admin panel secret_key 硬编码 | admin.py | 改为从 ADMIN_SECRET_KEY 配置读取 |
| CRITICAL: 异常信息泄露 | auth.py | Casdoor 错误不再暴露内部 exception |
| CRITICAL: SSE 无连接数限制 | realtime.py | 每市场 500 上限，满时拒绝 |
| CRITICAL: SSE 无最大时长 | stream.py | 1 小时强制断开 |
| CRITICAL: SSE 满队列静默丢弃 | realtime.py | 满时清理慢消费者+记日志 |
| HIGH: JWT 过期 7 天 | config.py | 改为 1 小时 |
| HIGH: JWT except Exception 过宽 | users.py | 分类捕获 Expired/Invalid/ValueError |
| HIGH: JWT 缺 iat/iss | users.py | 添加 iat 和 iss 声明 |
| HIGH: CORS 硬编码 | config.py + main.py | 从 CORS_ORIGINS 配置读取 |
| HIGH: allow_methods=\* | main.py | 改为明确列出 GET/POST/PUT/DELETE/OPTIONS |
| HIGH: Quote 不检查市场状态 | market.py | 添加 `market.status != "trading"` 检查 |
| HIGH: Quote 允许负数份额 | market.py | 卖出前检查 `new_q[idx] < shares` |
| HIGH: User.cash 无 CHECK 约束 | models/base.py | 添加 `CHECK(cash >= 0)` |
| HIGH: Position.amount 无 CHECK 约束 | models/base.py | 添加 `CHECK(amount >= 0)` |
| LOW: market.py 中文弯引号 | market.py | 替换为 ASCII 直引号 |

### 审阅中确认的「误报」

以下问题在审阅报告中被标记为 CRITICAL，但实际代码已经正确实现：

| 问题 | 实际情况 |
|------|----------|
| 交易无 SELECT FOR UPDATE | 代码已有 `_lock_market`/`_lock_user`/`_lock_outcomes_for_market` 等函数，均使用 `with_for_update()` |
| settle 无幂等检查 | settle 使用 `_require_halt(market)` 在锁定行后检查状态，并发调用第二个会被拒绝 |
| resolve 无幂等 | resolve 在 616-628 行显式检查 `market.status == "settled"` 并返回 |
| Position 无唯一约束 | 代码已有 `UniqueConstraint("user_id", "outcome_id")` |
| resolve N+1 锁定 | 代码 199-205 行已使用 `SELECT ... WHERE id IN (...) FOR UPDATE` 批量锁定 |
