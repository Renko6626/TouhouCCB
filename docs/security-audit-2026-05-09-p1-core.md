# 安全审计阶段 1 报告：业务核心 + 认证授权

**审计日期**：2026-05-09 起
**分支**：ralph/2026-05-09-secaudit-p1-core
**审计员**：Claude（代码 + 静态工具，只读）
**评级体系**：P0–P3（详见 docs/superpowers/specs/2026-05-08-security-audit-design.md §3）
**状态**：进行中

## 执行摘要

（最后填写）

## 发现明细

### LMSR 数值安全

**审计文件**：`backend/app/services/lmsr.py`（41 行，全文阅读）+ 调用点 `backend/app/api/v1/market.py`（buy 403-507 / sell 510-621 / quote 782-842 / list&detail 132-396 / movers 1031-1054）、`backend/app/api/v1/user.py`（87-95 / 149-156）、`backend/app/api/v1/loan.py`（20-42）、`backend/app/api/v1/stream.py`（37-54）、`backend/app/api/v1/chart.py`（92, 141, 149）
**审计日期**：2026-05-09
**LMSR 公共 API**：
- `quantize_cost(value: float|Decimal) -> Decimal`（6 位小数 / ROUND_HALF_UP）
- `quantize_price(value: float|Decimal) -> Decimal`（8 位小数 / ROUND_HALF_UP）
- `calculate_lmsr_cost(shares_list: List[float], b: float) -> float`
- `get_current_price(shares_list: List[float], target_index: int, b: float) -> float`

**审计要点 vs 发现**：

| 检查项 | 结果 | 证据 |
|---|---|---|
| 除零路径（b=0 / 空选项） | 风险（依赖外层 schema 拦截，lmsr 内部裸算） | `lmsr.py:33-34`、`lmsr.py:40-41`；外层校验仅 `schemas/market.py:13`（`MarketCreate.liquidity_b: gt=0`） |
| 负份额输入 | OK（多层防御） | `schemas/market.py:41,96`（pydantic `gt=0`）+ `market.py:410-411,517-518`（`shares_d <= ZERO` 二次校验） |
| 极大份额 DoS | 风险（无上限） | `schemas/market.py:41`（仅 `gt=0`，无上界）+ `market.py:434,552,809`（`float(shares_d)` 转换在过大 Decimal 上抛 OverflowError 500） |
| 量化精度一致 | 不一致 | `lmsr.py:13,20` 显式 `ROUND_HALF_UP`；`market.py:562,572,827,504-505,618-619` 等多处 `.quantize(...)` 未指定 rounding，默认走 `ROUND_HALF_EVEN` |
| ROUND 模式一致 | 不一致（同上） | 同上 |
| 浮点污染 | 设计内污染（已知） | `lmsr.py:24` 注释「内部保持 float」；`market.py:434,449,552,567,809` 在 `Decimal ↔ float` 边界往返 |
| TOCTOU on LMSR state | 写路径 OK；读路径未锁（仅做线索） | `market.py:413-456` 有 `_lock_market/_lock_outcomes/_lock_user`；`loan.py:20-42`、`stream.py:25-54`、`chart.py` 仅读不锁（→ Task 2） |

**发现**：

#### [P2] LMSR 内部缺少 b=0 / 空 shares_list 守卫，依赖外层 schema
- **位置**：`backend/app/services/lmsr.py:33-34`（`(q - max_q) / b` 与 `max_q / b`），`lmsr.py:40-41`（`get_current_price` 同样除以 `b`，且未守 `shares_list` 是否为空 → `max(shares_list)` 会 `ValueError`）
- **类别**：业务核心 / 数值安全
- **复现**：
  1. `calculate_lmsr_cost` 在 `lmsr.py:30-31` 守了「空列表返回 0」，但未守 `b == 0`；
  2. `get_current_price` 既未守 `b == 0` 也未守 `shares_list == []`（`lmsr.py:39 max(shares_list)`）；
  3. 当前生产链路，`b > 0` 由 `schemas/market.py:13` 的 `Field(default=100.0, gt=0)` 担保；空选项由 `models/base.py` Outcome 创建逻辑与 `market.py:323`「无 outcomes → 400」担保。
  4. 但 `models/base.py:63` 的 ORM 字段 `liquidity_b: float = Field(default=100.0)` 自身**没有 `gt=0` 约束**，且无迁移机制（CLAUDE.md）；任何绕过 `MarketCreate` 的写入路径（脚本 / 数据修复 / 未来新接口忘了用 schema）就会让 b=0/负数 进入 lmsr，产生 `ZeroDivisionError` → 500，并导致全市场报价、列表、SSE 快照、持仓估值、贷款额度都 500。
- **影响**：单一脏数据可让所有依赖该市场的读路径（行情、SSE、持仓估值、loan quota）持续 500，相当于该市场级 DoS。属业务核心健壮性问题，但需先有越权写入或运维误操作才能触发，因此 P2。
- **修复建议**：在 `calculate_lmsr_cost` / `get_current_price` 入口加 `if b <= 0: raise ValueError("b must be positive")` 与 `if not shares_list: raise ValueError("empty shares_list")`，作为最后一道防线；调用方仍保持 schema 校验。
- **状态**：未修复

#### [P2] 单笔 shares 无上界，极端 Decimal → float 溢出 / 算力 DoS
- **位置**：`backend/app/schemas/market.py:41`（`TradeRequest.shares: Decimal = Field(..., gt=0)`，无 `le=`）、`backend/app/schemas/market.py:96`（QuoteRequest 同），`backend/app/api/v1/market.py:434, 552, 809`（`float(shares_d)` / `float(req.shares)`）
- **类别**：DoS / 数值安全
- **复现**：
  1. 携带 `shares = 1e400`（pydantic 接受任意精度 Decimal）调 `/market/quote`；
  2. `market.py:809 float(req.shares)` 在量级 > ~1.8e308 时抛 `OverflowError: int too large to convert to float`，FastAPI 返回 500（未被 try 捕获）；
  3. 即便量级在 float 可表示范围内（如 1e100），`calculate_lmsr_cost` 中 `(q - max_q) / b` 对其他选项产生极大负数，`math.exp(-1e98)` 还能跑（结果 0），不会立刻 OOM；但持仓 cost_basis、Outcome.total_shares 一旦真被修改成超大 Decimal，后续重放 / chart / 持仓估值都会被该市场污染（CLAUDE.md 点名 `models/base.py` 无迁移，回滚困难）。
  4. quote 路径不持久化但仍 500；buy 路径需先 `cash >= pay`，对超大 shares 会被 `pay > cash` 拦下（`market.py:444`），实际写入概率低，但 500 仍是 DoS 表面。
- **影响**：任意已登录用户可对 `/market/quote` 与 `/market/buy` `/market/sell` 触发 500（OverflowError 不在已捕获异常列表）。`/quote` 限速 10r/s（CLAUDE.md），单 IP 攻击带宽有限，但仍属可重复触发的 DoS / 可观测错误污染。属 P2。
- **修复建议**：
  1. `TradeRequest.shares` 加 `le=Decimal("1e9")` 或更小的业务合理上界；
  2. 在 `lmsr.py` 入口拒绝 `abs(q) / b > 700`（`math.exp(>709)` 会 inf）的极端值并显式 400。
- **状态**：未修复

#### [P3] 量化 / 舍入模式在 `lmsr.py` 与 `market.py/user.py` 之间不一致（HALF_UP vs HALF_EVEN）
- **位置**：`backend/app/services/lmsr.py:13,20`（`ROUND_HALF_UP` 显式）；`backend/app/api/v1/market.py:504-505, 562, 572, 618-619, 827`（`.quantize(Decimal("0.01"))` / `.quantize(Decimal("0.000001"))` 均未传 `rounding=`，Python 默认 `ROUND_HALF_EVEN`）；`backend/app/api/v1/user.py:102-107, 169-175`（`.quantize(Decimal("0.01"))` 同样未指定）
- **类别**：数值安全 / 一致性
- **复现**：
  1. 任意金额尾数恰为 `*.5` 时，`lmsr.py` 的 `quantize_cost/quantize_price` 走 `HALF_UP`（向上），而 `market.py:562 fee = (proceeds * 0).quantize(Decimal("0.000001"))` 与 `market.py:572` `cost_basis` 减法走 `HALF_EVEN`（银行家舍入）；
  2. 当前 `SELL_FEE_RATE = Decimal("0")`（`market.py:37`），fee 永远是 0，差异短期不显化；但 cost_basis 减法（`market.py:572 (position.cost_basis * sold_ratio).quantize(Decimal("0.000001"))`）会在卖出时按比例舍入，与 lmsr 量化使用的 HALF_UP 不一致，长期累积可能产生 ±1 ULP 的零和偏差，影响后续 `unrealized_pnl` 与排行榜净值。
- **影响**：当前 fee=0 没暴露，但 cost_basis 舍入路径已经在用与 lmsr 不一致的模式；若未来开手续费或调精度，会出现「卖完所有持仓后系统总现金不等于初始」的零和偏差。P3 加固类。
- **修复建议**：在所有 `.quantize(...)` 调用显式传 `rounding=ROUND_HALF_UP`，与 lmsr 保持一致；或反之统一切到 `ROUND_HALF_EVEN`。任选其一，全局一致即可。
- **状态**：未修复

#### [P3] LMSR 内部使用 `math.exp` / `math.log`（float），与外层 Decimal 精度承诺不严格匹配
- **位置**：`backend/app/services/lmsr.py:33-34, 40-41`，调用点 `market.py:434, 449, 552, 567, 809`（Decimal → float 往返）
- **类别**：数值安全 / 设计权衡（已显式注释）
- **复现**：
  1. `lmsr.py:24` 注释「内部保持 float，数学运算不受影响」是项目设计选择；
  2. `quantize_cost` / `quantize_price` 在边界把 float cost 量化回 Decimal，理论上 6/8 位精度内 float 双精度足够（~15-17 位有效数字）；
  3. 但 `float(shares_d)`（`market.py:434, 552, 809`）当 `shares_d` 超过 ~1e15 时丢精度，且 `total_shares` 在 DB 是 `Decimal(16,6)`（见 `models/base.py`，未读细节，仅推断），存量被截断；
  4. 单笔小额交易没问题，长期累计 + 大额单笔交易共存时 float 累积误差进入持久化 cost_basis / total_shares。
- **影响**：精度漂移，长期可能造成 `unrealized_pnl` 与实际成交价反算不符。当前规模影响有限。
- **修复建议**（不强制）：保持现状但加注释 + 单元测试覆盖「100 万次 1e-3 量级买卖后系统零和」；或迁移到 `decimal.Context.exp()` 实现的 LMSR（性能换精度）。
- **状态**：未修复（设计取舍）

#### [INFO] 缺少 LMSR 单元测试
- **位置**：`backend/tests/`（无 `test_lmsr*.py`，grep 全无命中）
- **类别**：测试覆盖
- **复现**：`grep -rn "lmsr\|calculate_lmsr\|get_current_price" backend/tests/` 返回空。
- **影响**：lmsr.py 是 CLAUDE.md 标注的「定价核心」，零单元测试覆盖。任何后续精度 / 边界改动只能靠 e2e 验证。
- **修复建议**：新增 `tests/test_lmsr.py` 覆盖：(i) 两选项对称 0 份额→各 0.5；(ii) 单选项加仓后价格单调上升；(iii) 总成本差 = LMSR 公式参考实现；(iv) 极小/极大 b 边界；(v) `quantize_cost/price` 输入 float 与 Decimal 等价。
- **状态**：未修复

**留给后续阶段的线索**：

- LMSR 写路径与读路径锁粒度不同（`stream.py / loan.py / chart.py / user.py /me/holdings` 仅读不锁，可能与并发买卖产生瞬时不一致快照）→ Task 2「资金一致性 / 事务原子性」详查。
- `models/base.py:63 liquidity_b: float = Field(default=100.0)` 在 ORM 层未约束 `> 0`，又无迁移 → Task 10「无迁移机制风险」一并审。
- 极端 shares 上界（`TradeRequest.shares` 无 `le=`）属本任务发现，但根因「Pydantic schema 没有上界」与「全局 DoS 防护」交叉，留作阶段 3 / Task 11 静态扫 triage 时再次比对。
- 持仓估值用 LMSR 清算价值（`user.py:87-95, 149-156` 已与 `services/realtime.py` 推送口径对齐），与 Task 3「持仓估值与精度」交叉。

### 资金一致性 / 事务原子性

**审计文件**：`backend/app/api/v1/market.py`（1058 行，全文阅读）+ `backend/app/core/database.py`（61 行，全文）+ `backend/app/services/lmsr.py` 调用点（重读 buy/sell/quote/resolve）+ `backend/app/models/base.py`（187 行，全文，DB 约束）+ `backend/app/services/loan_service.py:15-27`（accrue_interest）+ `backend/app/services/site_config.py:17-42`（get_decimal 只读）+ `backend/app/schemas/market.py`（TradeRequest / QuoteRequest）+ `backend/tests/`（仅 `test_redemption_api.py` 命中关键字，无 buy/sell 并发测试）
**审计日期**：2026-05-09

**Session/事务模型**：

- 每请求一会话，由 `database.py:44-46 get_async_session` 通过 `async_session_maker()` 注入；`expire_on_commit=False`（`database.py:34`），commit 后仍能读列。
- 写边界统一用 `database.py:49-61 managed_transaction(db)` 包：若 session 已在 tx 内则 commit，否则 `async with session.begin()` 显式开 tx 并由 contextmanager 在退出时 commit / 异常时 rollback。`market.py` 所有写路径（`buy_shares` 413、`sell_shares` 520、`resolve_market` 640、`close_market` 207、`resume_market` 888）都包了 `managed_transaction`。**例外**：`create_market` 直接 `await db.commit()`（`market.py:191`），但写的是新建 Market + Outcomes，无并发资金风险，可接受。
- 行级锁辅助函数全部通过 `with_for_update()`：`_lock_market` (48)、`_lock_user` (59)、`_lock_outcomes_for_market` (73)、`_lock_outcome` (84) 与 `resolve_market` 内联的 `Market` (642)、`Outcome` (664)、`Position` (685)、`User` (728) FOR UPDATE。`buy/sell` 还对 Position 走 `select(...).with_for_update()`（455 / 524）。
- 连接池：`pool_pre_ping=True`、`pool_recycle=settings.DB_POOL_RECYCLE`、`pool_size`、`max_overflow` 全部走配置（`database.py:21-26`），SQLite 走 `check_same_thread=False`。
- DB 层防线：`User.cash >= 0` / `User.debt >= 0` / `Position.amount >= 0` 三条 `CheckConstraint`（`models/base.py:25-27, 124`）。即便应用层漏判，PG 会抛 `IntegrityError` 让 `managed_transaction` 回滚。

**审计要点 vs 发现**（按 buy / sell / quote / resolve 列出）：

#### BUY 路径（market.py:403-507）
| 检查项 | 结果 | 证据 |
|---|---|---|
| 单事务 | OK | `market.py:413 async with managed_transaction(db)` 包裹 414-481 全部读写，单 commit |
| User 行级锁 | OK | `_lock_user` 用 `with_for_update()`（`market.py:59`），`buy_shares` 在 423 调用，先于 444 现金校验 |
| Outcome / LMSR 状态锁 | OK | 414 `_lock_outcome` + 418 `_lock_outcomes_for_market` 全部 `with_for_update()`；写入在 449 |
| Market 锁 | OK | 415 `_lock_market` `with_for_update()`，保证状态机不被并发改 |
| Position 锁 | OK | 452-456 `select(Position).with_for_update()`；不存在时 460 `db.add` 新建 |
| 滑点保护 | **缺失** | `schemas/market.py:39-42 TradeRequest` 仅 `outcome_id + shares: Decimal(gt=0)`，**无 max_price / max_cost / slippage_bps**；`market.py:444 if locked_user.cash < pay` 仅校验现金够用，不校验客户端预期价 |
| 资金下界 | OK（多层） | 444 应用层 `cash < pay → 400`；DB 层 `ck_user_cash_non_negative`（`base.py:25`）兜底 |
| 负 cash 可能性 | 未发现 | 锁顺序 + 应用判断 + DB CHECK 三层；下方并发场景已逐步推演 |
| 异常回滚 | OK | `managed_transaction` 在异常时 `await session.rollback()` 后 raise（`database.py:56-58`）；handler 内**无** `try/except` 吞异常 |
| 幂等性 | **缺失** | 无 `Idempotency-Key` / `client_request_id` 表；客户端重试或代理重发会重复成交，每次都扣钱、加仓、推送 SSE |
| Fee 精度 | N/A | BUY 无手续费；`Transaction.fee = ZERO`（475-481），`gross = pay`，`avg_price = quantize_price(pay/shares_d)`（465）走 lmsr `quantize_price` 显式 HALF_UP |
| Decimal/float 精度 | 已知设计 | 434/467/468 `float(shares_d)` 喂 LMSR；边界用 `quantize_cost/price` 转回 Decimal（已在 Task 1 [P3] 备案） |

#### SELL 路径（market.py:510-621）
| 检查项 | 结果 | 证据 |
|---|---|---|
| 单事务 | OK | 520 `managed_transaction` 包 521-595 |
| Position 锁（卖出关键） | OK | 521-525 `select(Position).with_for_update()`，527 `position.amount < shares_d → 400` |
| Outcome / LMSR 状态锁 | OK | 530 `_lock_outcome` + 534 `_lock_outcomes_for_market`，写入在 567 |
| Market 锁 | OK | 531 `_lock_market` |
| User 行级锁 | OK | 539 `_lock_user`；net 在 566 加到 `locked_user.cash` |
| 锁顺序 | **风险** | BUY 顺序 = Outcome → Market → Outcomes → User → Position；SELL 顺序 = Position → Outcome → Market → Outcomes → User。**不同顺序 → 同选项的并发 buy + sell 可能互相等锁死锁**（PG 自动检测并 abort 一方为 deadlock 40P01；无吞死锁的代码，FastAPI 会返回 500） |
| 滑点保护 | **缺失** | 同上，`TradeRequest` 无 min_proceeds 字段 |
| 负 shares / 持仓不足 | OK（多层） | 527 应用层判断；548 `old_q[target_idx] < float(shares_d)` 二次校验；DB `ck_position_amount_non_negative`（`base.py:124`）兜底 |
| Outcome.total_shares 下界 | **无 DB 约束** | `models/base.py:97-104` Outcome 无 CHECK；应用层 548 用 float 比较，转 Decimal 后做 `outcomes[idx].total_shares -= shares_d` 在 567。理论上若 Position.amount 与 Outcome.total_shares 失同步（例如旧数据），可能让 total_shares 微负；DB 不拒 |
| 异常回滚 | OK | 同 BUY |
| 幂等性 | **缺失** | 同 BUY，重复 sell 会反复变现并放大 LMSR 价格冲击 |
| Fee 精度 | OK（当前 fee=0） | `SELL_FEE_RATE = Decimal("0")`（37）；562 `(proceeds * 0).quantize(Decimal("0.000001"))` 不指定 rounding，默认 HALF_EVEN；**与 lmsr 的 HALF_UP 不一致**（已在 Task 1 [P3] 记录） |
| cost_basis 比例减仓 | 风险（精度） | 569-572 `sold_ratio = shares_d / position.amount` 在 Decimal 里做除法，`position.cost_basis * sold_ratio` 再 quantize HALF_EVEN；累计多次小额卖出后 cost_basis 可能漂移（→ 持仓估值 Task 3） |

#### QUOTE 路径（market.py:782-842）
| 检查项 | 结果 | 证据 |
|---|---|---|
| 纯读 | OK | 全程 `db.get` / `db.execute(select(...))`，无 `db.add` / `db.delete`，无 commit；`@router.post` 但仍是 read-only |
| 副作用 | 无 | 不写 DB，不发布 BROKER 事件 |
| 一致性快照 | **风险（弱）** | `db.get(Outcome)` 788、`db.get(Market)` 792、`select(Outcome)` 799 三段查询不在显式事务内，**未加 FOR UPDATE / FOR SHARE**；中间若有并发 buy 提交，`outcomes` 可能读到三段不一致的版本（READ COMMITTED 默认）。买家依据该 quote 立刻下 buy，落地价格可能不同；这是 LMSR + 显式风险，无法完全消除，但应在 UI 提示「报价仅供参考」 |
| Spam / DB 写 | 无 | 无任何写入；唯一读成本是 1× User get（依赖注入） + 3 次 outcome/market query。CLAUDE.md 限速 10r/s |

#### RESOLVE 路径（market.py:624-779，超管）
| 检查项 | 结果 | 证据 |
|---|---|---|
| 单事务 | OK | 640 `managed_transaction` 包 641-755 |
| Market 锁 | OK | 641-644 `with_for_update()` |
| Outcome 全锁 | OK | 660-666 |
| Position 全锁 | OK | 676-687 join 加 FOR UPDATE |
| 重复结算 | OK（幂等） | 648 `if market.status == SETTLED → 直接返回当前状态`，不再重发兑付 |
| User cash 加锁 | OK | 727-729 每个受益用户 FOR UPDATE 后 `u.cash += pay` |
| Position 删除时机 | OK | 718 `await db.delete(pos)` 在事务内，与 cash 更新原子 |
| 兑付精度 | OK | `payout_unit = quantize_cost(req.payout)`（636）；`payout_amt = pos.amount * payout_unit`（700）保持 Decimal，无 float 中转 |
| 异常回滚 | OK | 732 用户消失时 raise → managed_transaction rollback |
| 锁顺序 | OK（一致） | 单一处理器内固定 Market → Outcome → Position → User，与 buy/sell 不冲突（resolve 是超管路径，不与买卖并发预期重叠） |

#### 其他写路径
- `close_market` (201-217)、`resume_market` (882-901)：仅改 Market.status，单 `_lock_market` + managed_transaction，无资金风险。
- `create_market` (171-198)：直接 `db.commit()`（191），新建市场无并发资金问题；但**与项目其他写路径模式不一致**（其它都用 managed_transaction），属代码异味。

**并发竞态分析**（Step 4：同用户、同 outcome、两次并发 buy，每次 50% 现金）：

设 user.cash=100，outcome.total_shares=0，b=100，市场处于 TRADING。

请求 A 与请求 B 同时到达 `/market/buy`，shares 都让 LMSR 成本恰好 = 50。

时序（PG，READ COMMITTED + FOR UPDATE）：

1. `市场.py:413 A` 进入 `managed_transaction` → `_lock_outcome(market.py:84)` 在该 outcome 上拿到行写锁；`B` 在 84 行阻塞。
2. `A` 接着 `_lock_market` 415、`_lock_outcomes_for_market` 418、`_lock_user` 423，全部成功（B 仍卡在第 1 步）。
3. `A` 在 444 校验 cash=100 ≥ pay=50，扣 cash 至 50，`outcomes[idx].total_shares += shares_d`，写 Position，写 Transaction，managed_transaction 退出 → commit → 释放所有锁。
4. `B` 此时拿到 outcome 锁，往下走；其后 `_lock_user` 423 拿到 user 锁，**重新读到 user.cash=50**（FOR UPDATE 强制读最新版本）；新一轮 LMSR 计算 pay≈50（因为 total_shares 已变，价格已上移，pay 实际略 > 50），444 判 50 < 50.x → 400「现金不足」回滚。
5. **结论：单用户 + 两次并发 buy 不会让 cash 下穿 0**。锁顺序保证 A 持有 outcome 锁直到 commit，B 即便先到 444 也读到 A 写后的 cash。即便顺序倒换（B 先到 user 锁），由于 outcome 锁仍由 A 持有，B 同样阻塞在 outcomes_for_market 步。

**LMSR 状态一致性**：`_lock_outcome` + `_lock_outcomes_for_market` 在 A commit 前持有所有 outcome 行锁，B 读到的 `total_shares` 必然是 A commit 之后的值，不会出现「两个并发 buy 读到同一旧 LMSR 状态、各自计算成本」的 TOCTOU。

**潜在死锁**（Step 4 副产物）：BUY 路径锁顺序 = `Outcome → Market → Outcomes → User → Position`；SELL 路径锁顺序 = `Position → Outcome → Market → Outcomes → User`。同一用户先 buy 再 sell 不会冲突（串行）；但 **用户 X buy outcome O 与用户 Y sell outcome O 并发** 时：X 持 Outcome O 锁等 Position(X,O) 锁；Y 持 Position(Y,O) 锁等 Outcome O 锁——锁的 row id 不同（不同 Position 行），实际不会死锁。但若 X 与 Y 是**同一用户**同时 buy + sell 同一 outcome（极端：客户端 BUG / 重放）：X 等 Position(X,O)，Y 已持 Position(X,O) 等 Outcome O，X 已持 Outcome O——**死锁成立**。PG 会自动 abort 一方（40P01），FastAPI 返回 500。同账号同时 buy + sell 同选项的业务场景虽罕见，但 SSE 重连/前端按错按钮可触发。

**发现**：

#### [P1] 缺少滑点保护（max_price / max_cost），LMSR 价格被并发 / 大额单方向交易拉走时用户资金被静默消耗
- **位置**：`backend/app/schemas/market.py:39-42`（`TradeRequest`），`backend/app/api/v1/market.py:436-445`（buy 计算 pay 后只校验 `cash < pay`），`market.py:554-566`（sell 计算 proceeds 后无下界校验）
- **类别**：业务核心 / 资金安全
- **复现**：
  1. 用户 U 在 t0 调 `/market/quote`，得到 avg_price=0.40，shares=100，gross=40。
  2. t1（数十毫秒后）在被另外用户 V 大额 buy 拉价后，U 提交 `/market/buy {outcome_id, shares=100}`。
  3. 此时 LMSR 实际成本 = 60（价格 0.60），`market.py:444 cash(100) >= pay(60)` 通过 → 用户实际花 60 拿到与 quote 时同等数量的 shares，多付 50%。Sell 同理可能少收。
  4. 没有 `max_price` / `max_cost` / `min_proceeds` 字段供客户端发出价格容忍区间，服务端也没有「与最近 quote 的偏差超过 X% 即拒绝」的兜底。
- **影响**：单笔最多可让对手方 / 套利者吃掉用户预期的差价；与拉抬-诱单（price-pump-then-fill）组合时构成可重复的资金转移路径。LMSR 是 AMM，价格冲击与 b 成反比（b=100 默认），小市场（`liquidity_b` 小）冲击放大。属典型 DeFi/AMM「无 slippage cap」类问题。**实际可能的最大单笔损失**受 `cash` 上界限制（不会负），但相对损失可能 ≥ 50%。**P1**（单用户严重资金损失，可被市场操纵触发；非 P0 因为不能伪造负 cash）。
- **修复建议**：
  1. `TradeRequest` 增加可选 `max_cost: Optional[Decimal]`（buy）/ `min_proceeds: Optional[Decimal]`（sell），后端在 444 / 559 之后比对，超限直接 400。
  2. 服务端默认 slippage cap：若客户端不传，按 `pay > expected_cost * 1.05` 拒绝（expected 基于交易前 LMSR 现价 × shares 估算）。
  3. 文档明确 quote 与 buy/sell 之间不保证一致，前端 UI 显示价格冲击。
- **状态**：未修复

#### [P1] BUY 与 SELL 锁顺序不一致，同账号 buy+sell 同 outcome 并发触发死锁 → 500（DoS / 一致性）
- **位置**：`backend/app/api/v1/market.py:414-456`（buy：先 `_lock_outcome` 414 / `_lock_outcomes_for_market` 418 / `_lock_user` 423 / Position FOR UPDATE 452-456）vs `market.py:521-539`（sell：先 Position FOR UPDATE 521-525 / `_lock_outcome` 530 / `_lock_outcomes_for_market` 534 / `_lock_user` 539）
- **类别**：业务核心 / 并发一致性
- **复现**：
  1. 同账号在前端按错按钮或 SSE 重连后重发请求，导致同一 user × 同一 outcome 同时发出 buy + sell。
  2. 请求 A（buy）拿到 Outcome O 行锁（414）后等 Position(U,O) FOR UPDATE（452-456）。
  3. 请求 B（sell）拿到 Position(U,O) 行锁（521-525）后等 Outcome O 行锁（530）。
  4. PG 检测到环 → 选 victim abort，返回 `40P01 deadlock detected`，SQLAlchemy 抛 `DBAPIError`，FastAPI 默认 500。Handler 无 retry。
- **影响**：可被恶意客户端用来周期性产生 500，污染日志、消耗连接池槽位（pool_size 配置型）；并非资金被偷，但属可重复触发的可观测错误，且违反「同账号操作可串行化」的直觉。**P1**（DoS + 数据完整性轻度风险）。
- **修复建议**：
  1. 把 SELL 的锁顺序改为与 BUY 一致：先 `_lock_outcome` → `_lock_market` → `_lock_outcomes_for_market` → `_lock_user` → Position FOR UPDATE（且 Position 不存在则直接 400 「持仓不足」，因为 buy 路径才会创建 Position）。
  2. 对 `OperationalError` (deadlock) 在 handler 外层加 1 次自动重试，间隔 50ms。
- **状态**：未修复

#### [P2] 无幂等键，重复请求 / 客户端重试会被多次成交
- **位置**：`backend/app/schemas/market.py:39-42`（无 `client_request_id` / `Idempotency-Key`），`backend/app/api/v1/market.py:403-507`（buy）、`510-621`（sell）；模型层 `models/base.py:137-170 Transaction` 也无 client-side 唯一约束
- **类别**：业务核心 / 资金安全
- **复现**：
  1. 客户端因网络抖动 / 504 / SSE 重连发出两次 `/market/buy {outcome_id, shares: 50}`；
  2. 两次请求都成功，扣两次现金，建两次 Transaction，触发两次 SSE「trade」事件；
  3. 没有任何键能让服务端识别「这是同一意图的重试」并幂等返回。
- **影响**：网络不稳定时用户实际成交量翻倍。攻击面有限（限速 10r/s），但属典型金融接口必备特性缺失。P2。
- **修复建议**：
  1. `TradeRequest` 增 `client_request_id: UUID`；新建 `idempotency_key` 表 `(user_id, key) UNIQUE` + 缓存响应；同一键二次到达直接返回首次结果。
  2. 短期：依赖 Web 客户端去重（不可靠）。
- **状态**：未修复

#### [P2] QUOTE 路径不在事务内、无锁，与 BUY 之间存在 TOCTOU（用户层）
- **位置**：`backend/app/api/v1/market.py:782-842`（quote 全程 `db.get` / `db.execute(select)` 不加锁、不显式开 tx）
- **类别**：业务核心 / 一致性
- **复现**：
  1. quote 在 t0 读到 outcome 状态 S0，返回 avg_price = P0。
  2. 期间另一笔 buy 在 t0+ε 提交，total_shares 变。
  3. 用户拿 P0 调 `/market/buy`，实际 LMSR 成本与 P0 不一致；如未来加滑点保护可缓解，否则参见 [P1 滑点]。
- **影响**：单看 quote 这是 LMSR + 多用户系统的固有现象，但当前 schema 也未让客户端表达「我能接受的价差」，两者叠加放大可利用面。已在 [P1] 同步覆盖修复路径，这里仅记录现状。P2 加固性。
- **修复建议**：与 [P1] 一并：客户端附带 max_cost / min_proceeds；可选项是 quote 返回 `quote_token` + 短 TTL（5 秒）服务端缓存的不可重放 token，buy 时携带回来比对。
- **状态**：未修复

#### [P3] BUY 路径调用 `_lock_outcomes_for_market` 重复锁了 `_lock_outcome` 已锁的行（无害但冗余）
- **位置**：`backend/app/api/v1/market.py:414`（先锁单 outcome）+ 418（再锁该 market 全部 outcomes，包括第 414 行已锁的那个）
- **类别**：代码加固
- **复现**：PG `SELECT ... FOR UPDATE` 在同事务内对同一行可重入；不会死锁也不会 double-lock，但产生额外一次 SQL roundtrip。
- **影响**：性能微影响，无安全问题。
- **修复建议**：移除 414 `_lock_outcome` 调用，直接用 418 的 `_lock_outcomes_for_market` 结果中的 `outcomes[target_idx]`。SELL 同理。
- **状态**：未修复

#### [P3] `Outcome.total_shares` 缺 DB-level `CHECK >= 0`
- **位置**：`backend/app/models/base.py:97-104`（Outcome 没有 `__table_args__` CheckConstraint）
- **类别**：加固 / 模型层防线
- **复现**：当前应用层（`market.py:548`）已用 float 校验 `old_q[target_idx] < float(shares_d) → 400`；但若数据迁移 / 修复脚本 / 未来直 SQL 写入未守此线，DB 不拒。
- **影响**：与 User.cash / Position.amount 的 CHECK 保护一致性不齐；目前不构成可利用 bug，是加固类。P3。
- **修复建议**：与 Task 10「无迁移机制风险」合并：起草迁移加 `CheckConstraint("total_shares >= 0")`。
- **状态**：未修复

**留给后续阶段的线索**：

- 限速绕过 / 单 IP 击穿（buy 10r/s, quote 10r/s 但无滑点 → 拉抬攻击成本低）→ **阶段 3 / Task 11**。
- 长事务 + 连接池：`buy_shares` 在事务内还要 `_loan_site_config.get_decimal` 与 `_loan_accrue`（buy 426-427），未来若增高复杂度可能导致事务持锁时间过长 → **阶段 3 性能 / DoS**。
- `cost_basis` 按比例减仓的精度漂移（sell 569-572 HALF_EVEN）→ **Task 3 持仓估值与精度**。
- `liquidity_b` 在 `Market` 模型层无 `>0` 约束 + 无迁移机制 → **Task 10**。
- BROKER.publish 在 commit 后 await（buy 488 / sell 602）：若 BROKER 阻塞会让该 handler 长持响应，但事务已提交，资金一致性不受影响——**阶段 3 实时层**。
- BUY/SELL 锁顺序问题修复后还应增加 `pytest.mark.asyncio` 并发测试覆盖。


### 持仓估值与精度

**审计日期**：2026-05-09

**审计文件**：
- `backend/app/api/v1/user.py`（178 行，全文）
- `backend/app/api/v1/loan.py`（144 行，全文）
- `backend/app/api/v1/market.py`（leaderboard 904-930，重读）
- `backend/app/services/realtime.py`（90 行，全文）
- `backend/app/api/v1/stream.py`（138 行，全文）
- `backend/app/schemas/user.py`（HoldingRead / UserSummary）
- `thccb-frontend/src/types/user.ts`（Holding / UserSummary 类型定义）
- `thccb-frontend/src/stores/user.ts`（全文）
- `thccb-frontend/src/api/user.ts`（全文）
- `thccb-frontend/src/pages/user/Portfolio.vue`（全文）
- `thccb-frontend/src/pages/home/Home.vue`（全文）
- `thccb-frontend/src/pages/loan/Loan.vue`（全文）

---

**Commit 4a49d2e 修正概要**：

`user.py` 的 `get_user_summary`（行 78-92）与 `get_my_holdings`（行 143-156）中，`market_value`（即 `liquidation_value`）的计算从 LMSR 成本差额追加乘 `(1 - SELL_FEE_RATE)`，使估值口径与用户实际平仓可得现金完全一致；同时修正了 `schemas/user.py` 中 `HoldingRead.market_value` 的注释（原为 `amount * current_price`，改为 LMSR 清算价值描述）。

---

**估值入口清单**：

| 文件:行 | 入口 | 是否走新清算函数 | 备注 |
|---|---|---|---|
| `user.py:47-109` | `GET /user/summary`（holdings_value / net_worth / unrealized_pnl） | **是** | `calculate_lmsr_cost(old) - calculate_lmsr_cost(after_sell)` × `(1 - SELL_FEE_RATE)`；`quantize_cost` 统一 |
| `user.py:112-177` | `GET /user/holdings`（每条 HoldingRead.market_value） | **是** | 同上；两处路径代码一致，无遗漏 |
| `loan.py:20-42` | `_holdings_value()`（贷款额度/净值） | **否（旧路径）** | 使用 `p.amount * price`（瞬时价 × 数量）；`price = get_current_price(...)` 未考虑卖出滑点与手续费；函数注释已承认「瞬时价估算」 |
| `market.py:904-930` | `GET /market/leaderboard`（net_worth） | **不含持仓** | `net = user.cash - user.debt`；排行榜只计算现金净值，**不含持仓市值**，属设计选择，有注释歧义风险 |
| `stream.py:25-71` | SSE 首包 snapshot（各 outcome 现价） | 不涉及 | 仅发 LMSR 边际价格 `get_current_price`，不发用户持仓估值；持仓估值不由 SSE 推送 |

---

**SSE / realtime 推送一致性**：

`realtime.py` 是内存 pubsub broker，仅在 `market.py` 中的 `buy_shares` / `sell_shares` / `resolve_market` / `close_market` / `resume_market` 完成 commit 后异步 `BROKER.publish`（推送 trade 事件或 market_status 变更）。SSE 推送内容是**市场级行情**（成交价、outcome 当前边际价格），**不包含任何用户持仓估值字段**。前端在收到 SSE trade 事件后，需用户主动调用 `/user/summary` 或 `/user/holdings` 才能刷新持仓估值。因此 SSE 路径与持仓估值函数没有直接耦合关系：SSE 不复用也不需要复用清算估值函数，架构上正确隔离。

**潜在问题**：SSE trade 事件到达后，前端并不会自动触发重新拉取 `/user/summary`（`useSSE.ts` 无此逻辑，`TradingView.vue` 仅在用户本人提交成功后 `loadUserData()`）。其他人成交导致价格漂移时，当前页面展示的持仓估值会滞后，直到用户手动刷新或重新访问 Portfolio。这不是安全漏洞，但属 UX 潜在混淆。

---

**前端精度审计**：

| 文件:行 | 来源字段 | 是否 Number() 后做算术 | 风险 |
|---|---|---|---|
| `Portfolio.vue:128-129` | `h.market_value` / `h.unrealized_pnl`（reduce 求和） | 用 JS number 算术 `sum + h.market_value` | 低：服务端已 `.quantize(Decimal("0.01"))` 返回字符串/float，值最多 6 位有效小数，JS number 精度足够（无超 15 位场景） |
| `Portfolio.vue:79` | `row.market_value / row.amount`（卖出均价列） | 是，JS number 除法 | 低：纯显示用，`toFixed(4)` 截断；除法不写回存储，不影响逻辑 |
| `Portfolio.vue:181,188` | `Number(userStore.summary.debt)` | 是，用于条件判断 + toFixed(2) | 低：debt 是货币值（后端 `.quantize("0.01")`），不超过 2 位小数，Number() 无精度损失 |
| `Home.vue:107,109` | `Number(userStore.summary!.debt)` | 是，条件判断 + toFixed(2) | 同上，低 |
| `Loan.vue:21-23` | `Number(store.quota?.debt/cash/max_borrow)` | 是，用于 UI 控件 max 绑定与还款上限计算 `Math.min(debt, cash)` | **中低**：max_borrow / debt / cash 均为 2 位小数 Decimal 字符串，Number() 无精度损失；但若将来后端精度升到 8 位小数，Number() 在 >1e15 的金额上才会出问题，当前规模安全 |
| `stores/user.ts:16` | `summary.value?.holdings_value ?? 0` | 直接返回 number 类型（TS 接口定义为 `number`） | 低：后端返回 `.quantize(Decimal("0.01"))`，2 位小数；实际通过 JSON 反序列化为 JS number 时已是 float；精度差异只有在极端大额（>2^53）时才显现 |

**TypeScript 接口精度声明问题**：`types/user.ts` 中 `UserSummary` 和 `Holding` 的所有金融字段（`cash`、`debt`、`holdings_value`、`market_value` 等）均声明为 `number`（JS float）。后端实际以 `float` 形式通过 JSON 返回这些字段（FastAPI Pydantic 序列化 Decimal 时走 JSON float），因此当前不存在「字符串被 Number() 转精度丢失」的问题。**真正的精度边界**是后端返回的 float 本身在 >2^53 时已丢精度，但现实货币量级（< 10^10）不会触发。

---

**发现**：

#### [P2] `loan.py:_holdings_value` 使用瞬时价 × 数量，与 `/user/summary` 的 LMSR 清算估值口径不一致

- **位置**：`backend/app/api/v1/loan.py:20-42`，调用点 `loan.py:53, 83, 100, 137`（GET /quota、POST /borrow、POST /repay）
- **类别**：估值口径不一致 / 贷款额度计算
- **复现**：
  1. `_holdings_value` 第 40-41 行：`price = Decimal(str(get_current_price(shares, idx, m.liquidity_b)))` + `total += (p.amount * price).quantize("0.000001")`——这是瞬时边际价格 × 持仓数量，不扣卖出滑点，不考虑 LMSR 价格冲击。
  2. `user.py:get_user_summary` 用的是 `calculate_lmsr_cost(shares_before) - calculate_lmsr_cost(shares_after)` × `(1 - SELL_FEE_RATE)`——这是 LMSR 清算价值（全部卖出时实际拿到的现金）。
  3. 对于大量持仓，清算价值会因卖出冲击而低于「瞬时价 × 数量」；LMSR 的 `b` 越小，差距越大。
  4. 贷款额度 `max_borrow = k × (cash - debt + holdings_value) - debt`（`loan_service.py:122-124`）；若 `holdings_value` 被高估，用户可借到超过安全线的金额，造成坏账风险。
- **影响**：持仓量大或流动性参数 `b` 小的市场中，贷款额度（loan quota 与 max_borrow）被系统性高估，用户能借到多于实际可平仓价值对应的金额，增加坏账暴露。函数注释虽注明「瞬时价估算」，但这属于贷款系统与估值系统口径不一致，属 P2（金融功能一致性缺陷，不是 P1 因为当前 `SELL_FEE_RATE=0` 且实际差距取决于持仓规模与市场 b）。
- **修复建议**：将 `_holdings_value` 改为与 `user.py:get_user_summary` 相同的 LMSR 清算价值计算（`calculate_lmsr_cost(old) - calculate_lmsr_cost(after_sell)` × `(1 - SELL_FEE_RATE)`），或提取为公用函数 `_lmsr_liquidation_value(db, user_id)` 在 `user.py` 和 `loan.py` 共享。
- **状态**：未修复

#### [P2] 排行榜 `net_worth` 不含持仓市值，与 `/user/summary` 的 `net_worth` 口径不一致

- **位置**：`backend/app/api/v1/market.py:904-930`（leaderboard endpoint）
- **类别**：估值口径不一致 / UX 混淆
- **复现**：
  1. `leaderboard`：`net = user.cash - user.debt`（仅现金 - 负债，无持仓）。
  2. `/user/summary`：`net_worth = user.cash - user.debt + holdings_value`（含 LMSR 清算价值）。
  3. 两个接口都叫"净资产 / net_worth"，但计算口径完全不同；用户对比自己的 `/user/summary` 与排行榜时会看到两个不同的净值数字。
- **影响**：UX 混淆；重仓用户在排行榜上净值被大幅低估（只显示现金部分），轻仓现金用户则准确；会误导用户对相对资产规模的判断。属 P2（无资金安全风险，但口径不一致可能导致用户错误决策）。
- **修复建议**：
  1. 修复排行榜：为每个用户在 SQL 层无法高效计算 LMSR 清算价值，可改为「定期任务缓存 holdings_value」或改为「排行榜只排 cash - debt，注明『不含持仓』」并在 UI 明确说明。
  2. 至少在 API 响应中加说明字段或重命名为 `cash_net_worth`，避免与 `/user/summary.net_worth` 混淆。
- **状态**：未修复

#### [P3] `sell_avg_price` 列在前端用 JS number 除法计算，未对 `amount=0` 做完整防护（minor）

- **位置**：`thccb-frontend/src/pages/user/Portfolio.vue:79`
- **类别**：前端健壮性
- **复现**：`row.amount > 0 ? row.market_value / row.amount : 0`——有守 0 值，但当 `market_value` 来自历史快照而 `amount` 极小（如 0.000001）时，除法结果 > 1（违反概率 < 1 的直觉）不会被拦截；仅用于展示，无逻辑影响。
- **影响**：纯显示问题，不影响任何后端逻辑或资金计算，P3。
- **状态**：未修复

#### [INFO] TypeScript 接口将所有金融字段声明为 `number`（JS float），精度依赖后端输出范围

- **位置**：`thccb-frontend/src/types/user.ts`（`UserSummary`、`Holding`）
- **类别**：精度声明 / 技术债
- **说明**：当前后端以 JSON float 序列化 Decimal（FastAPI 默认行为），前端接收到的已是 float，所以 `number` 声明不引入额外精度损失。但若后端某天将精度升到 8 位以上、或金额超过 10^15，JS float 会截断。建议在接口注释中注明「后端保证输出值不超过 float64 安全精度范围」，明确风险边界。
- **状态**：记录，不修复（当前无影响）

---

**留给后续阶段的线索**：

- `loan.py:_holdings_value` 瞬时价高估问题与 Task 4「贷款 / 复利 / 还款」直接相关，建议在 Task 4 一并审计 max_borrow 计算的完整安全性。
- `cost_basis` 按比例减仓的舍入漂移（Task 2 已记录）会影响 `holdings/unrealized_pnl` 的长期准确性，但属精度累积类 P3，不影响单笔清算。
- 持仓估值代码在 `user.py:get_user_summary` 与 `user.py:get_my_holdings` 有两处几乎相同的 7 行 LMSR 清算逻辑，未提取为公用函数；一旦手续费逻辑变更（SELL_FEE_RATE 非 0）只改一处漏改另一处，会导致 summary 与 holdings 明细的 market_value 不一致——属代码卫生问题，建议重构为单一 `_position_liquidation_value(pos, market, all_outcomes)` 辅助函数。

### 贷款 / 复利 / 还款

**审计日期**：2026-05-09
**审计文件**（只读）：
- `backend/app/services/loan_service.py`（125 行，全文）
- `backend/app/services/loan_sweep.py`（98 行，全文）
- `backend/app/services/loan_migrate.py`（74 行，全文，迁移脚本本任务范围外但 skim）
- `backend/app/api/v1/loan.py`（143 行，全文）
- `backend/app/api/v1/user.py:286-349`（force_loan / forgive_debt）
- `backend/app/api/v1/site_config.py`（86 行，全文）
- `backend/app/api/v1/market.py:425-427, 541-543`（buy/sell 的 accrue 钩点）
- `backend/app/models/base.py:23-56`（User cash/debt/last_accrued_at + CHECK 约束）
- `backend/app/schemas/loan.py`（56 行，全文）
- `backend/tests/test_loan_service.py` / `test_loan_api.py` / `test_loan_admin.py` / `test_loan_sweep.py`（558 行）

**前置 fix 复盘**：
- `60847ad`（2026-04-27）：repay 双封顶——`decrease_debt(consume_cash=True)` 内 `effective = min(amount, post-accrual debt, cash)`。修前用户在「cash 刚好等于 pre-accrual debt + 距上次结息有较长时间」的场景下，复利让 post-accrual debt 超过 cash，effective 用 `min(amount, debt)` 取到 post-accrual debt，但 cash 只够支付到 pre-accrual debt → cash 跑负。同时移除了 api 层基于 PRE-accrual snapshot 的 cash 预检（必然有偏差），改为 service 层在锁内统一处理。
- `5771b45`（2026-04-27）：`increase_debt` / `decrease_debt` 末尾加 `if u.debt < 0 or u.cash < 0: raise LoanServiceError(...)` 后置不变量，并在 `force_loan`/`forgive_debt`/`repay` 入口把 `ValueError` + `LoanServiceError` 映射为 HTTP 400（之前裸抛 500）。提交说明已声明「未发现实际能让 debt 跑负的代码路径」，是防御性兜底。

#### 数据模型概览

| 字段 | 类型 / 约束 | 说明 |
|---|---|---|
| `User.cash` | `Numeric(16, 6)`，CHECK `cash >= 0` | 现金 |
| `User.debt` | `Numeric(16, 6)`，CHECK `debt >= 0` | 债务 |
| `User.debt_last_accrued_at` | `DateTime(timezone=True)`，nullable | 上次结息时间；debt=0 时为 None |

**没有独立 Loan / LoanRecord 表**。债务是 User 的两列，全程没有借/还/结息流水（只有 `logger.info` 文本日志）——审计/对账只能靠应用日志，不能靠 DB。**这是 P2 级别的可观测性缺口**（见下文 [P3-LOAN-04]）。

#### BORROW 路径 (`api/v1/loan.py:68-105`)

| 检查项 | 结果 | 证据 |
|---|---|---|
| 额度上限 (max_borrow) | ✅ 有 | `compute_max_borrow = max(0, k×(cash-debt+hv) - debt)`；amount > max_borrow 返回 400 |
| 原子性（cash + debt + 锁） | ✅ | `increase_debt` 用 `select(User).with_for_update()`；`db.commit()` 在 handler |
| 负本金 / 0 本金 | ✅ | pydantic `condecimal(gt=0)` (422) + service `if amount <= 0: ValueError` 双层 |
| 启用开关 | ✅ | `loan_enabled=false` → 403 |
| **持仓估值口径**（影响 max_borrow） | ❌ **buggy（Task 3 P2 straggler）** | `_holdings_value` 用 LMSR 瞬时价 × 数量，未扣卖出滑点+手续费；持仓大或 b 小时高估 → 借超 |
| **TOCTOU**（额度预检 vs 实际写入） | ❌ **未锁内重检** | 详见 [P2-LOAN-01] |
| 免费现金路径（cash 加但 debt 不加） | ✅ 不存在 | `increase_debt(grant_cash=True)` 是唯一路径，cash 与 debt 同增同量 |
| 限速 / 重放保护 | ⚠️ 限速由 nginx 层兜底 | 见 CLAUDE.md「限速」段；service / api 层无幂等 token |
| ZeroDivisionError 兜底（b=0 → 500） | ❌ | `_holdings_value` 调 `get_current_price`，未 try-except；P1 Task 1 已记录 b=0 → 500，此处会跟着 500（不是 P0，但不友好） |

#### REPAY 路径 (`api/v1/loan.py:108-143`)（含 60847ad 修正验证）

| 检查项 | 结果 | 证据 |
|---|---|---|
| 双封顶（amount, post-accrual debt, cash） | ✅ | `loan_service.py:102-105`，`effective = min(amount, u.debt).quantize(_QUANT); if consume_cash: effective = min(effective, u.cash).quantize(_QUANT)` |
| 先 accrue 再封顶 | ✅ | `loan_service.py:101` `accrue_interest(u, daily_rate, now)` 在 `effective` 计算之前 |
| 0 / 负还款 | ✅ | pydantic `gt=0` (422) + `if amount <= 0: ValueError` (400) |
| 并发 repay + sweep | ✅ | 两端都 `with_for_update`；先到的拿锁，后到的在 lock 释放后再 accrue（无重复结息） |
| 不变量（debt/cash >= 0） | ✅ | `loan_service.py:114-116` 兜底；DB CHECK 约束作为最终防线 |
| ValueError → 400 | ✅ | `api/v1/loan.py:128-130` 显式 catch + rollback |
| effective 字段返回 | ✅ | `LoanActionResponse.effective: Optional[Decimal]`；前端可展示「实际还款 ¥N」 |
| **跨时区一致性** | ✅ | accrue 用 `_compat_now` 处理 SQLite naive；生产 Postgres 全 UTC，无 DST 问题 |
| **stale test**（已发现） | ❌ | 见 [P3-LOAN-02] |

**60847ad fix 场景 walk-through**（按要求带数字）：

初始：`cash=1000, debt=1000, last_accrued_at=now-24h, daily_rate=0.01`，用户 POST `/loan/repay {"amount": "3000"}`：

1. handler 入口（`api/v1/loan.py:121`）：`user.cash=1000 > 0`，不早 reject。
2. 进入 `decrease_debt`（`loan_service.py:79`）：
   - `select(User).with_for_update()` 拿锁，`u.debt=1000, u.cash=1000`。
   - `accrue_interest(u, 0.01, now)`：`factor = 1 + 0.01 * 86400/86400 = 1.01`，`u.debt = 1000 * 1.01 = 1010.000000`，`u.debt_last_accrued_at = now`。
   - `effective = min(amount=3000, u.debt=1010) = 1010`（第一道封顶：debt）。
   - `consume_cash=True` → `effective = min(1010, u.cash=1000) = 1000`（第二道封顶：cash）。这一步是 60847ad 加上的关键。
   - `u.debt = 1010 - 1000 = 10.000000`，`u.cash = 1000 - 1000 = 0.000000`。
   - 不变量检查：`u.debt=10 >= 0`、`u.cash=0 >= 0`，通过。
3. handler `db.commit()`，返回 `effective=1000`。
4. 终态：`cash=0, debt=10`。**没跑负，符合预期。**

如果没有第二道封顶（fix 前）：`u.debt = 1010 - 1010 = 0`，`u.cash = 1000 - 1010 = -10` → 触发 DB CHECK `ck_user_cash_non_negative`，整个事务回滚 → 500。修前因为既无封顶又无后置不变量，直接持久化 `cash=-10`（这与 5771b45 commit 描述「未发现实际能让 debt 跑负的代码路径」吻合——是 cash 跑负，不是 debt）。

**仍然可疑的场景**（未发现 P0/P1，但记录）：
- **场景 A：rate 超大 + 长间隔**。若 daily_rate=0.999、距上次结息 30 天，`factor = 1 + 0.999*30 = 30.97`（不是 `1.01^30`，因为公式是线性 elapsed 不是真复利——见下文 ACCRUE 分析）。debt=1000 → 30970。post-accrual debt 远超 cash 不要紧（被封顶），但**accrue 出现单次巨大跳变**，触发链可能引发数值溢出（Decimal 16,6 上限 ~9.9e9）。site_config 已限制 `0 < daily_rate < 1`，attack surface 很窄。
- **场景 B：amount 极大（接近 Decimal 16,6 上限）**。pydantic `condecimal(max_digits=16, decimal_places=6)` 限制 amount ≤ 9_999_999_999.999999；service 层取 min 后不会写入超大值。但 `user.cash = u.cash - effective` 中间值若负数会触发 5771b45 不变量（防御 OK）。
- **场景 C：后置不变量绕过**。理论上 `decrease_debt` 后置 `if u.debt < 0 or u.cash < 0` 不可能为真（前面有 min 封顶）；但 `5771b45` 注释明确这是「为未来重构留兜底」，当前路径下不可达——这是合理的纵深防御。

#### ACCRUE 路径 (`loan_service.py:15-27`)

| 检查项 | 结果 | 证据 |
|---|---|---|
| Decimal 量化 | ✅ | `(user.debt * factor).quantize(_QUANT)`，`_QUANT = 0.000001` |
| debt=0 / last_accrued=None / elapsed<=0 早返 | ✅ | `loan_service.py:20-24` |
| 时钟倒退兜底 | ✅ | `if elapsed_sec <= 0: return`（test_accrue_negative_elapsed_noop 覆盖） |
| **复利公式严谨度** | ⚠️ **线性 elapsed，不是真连续复利** | 见 [P3-LOAN-03] |
| 长闲置账户上限 | ❌ **无 cap** | 见 [P2-LOAN-04] |
| **rate 运行时变更追溯**（admin 改 daily_rate） | ❌ **会回溯影响整个 elapsed 区间** | 见 [P2-LOAN-05] |
| 已禁用 / 已 ban 用户继续累息 | ⚠️ | sweep 与 buy/sell accrue 都不看 `is_active`；ban 用户欠债仍持续涨 |
| 锁保护 | ✅ | sweep 内 `with_for_update`；market.py buy/sell 内 `_lock_user` |

#### SWEEP 路径 (`services/loan_sweep.py`)

| 检查项 | 结果 | 证据 |
|---|---|---|
| 触发方式 | **APScheduler in-process job**，APP lifespan 启停 | `main.py:14, 47` 调 `start_scheduler`；间隔由 `loan_sweep_interval_sec` 配置（默认 60s，clamp 到 [10, 3600]） |
| 鉴权 | ✅ 不暴露 HTTP；只能从 in-process 触发 | grep 全 codebase，无 endpoint 直接调用 `run_sweep_once` |
| 间隔修改入口 | ✅ `PUT /api/v1/site-config/loan_sweep_interval_sec`，superuser only | `site_config.py:39, 44, 76-80` |
| max_instances=1 | ✅ APScheduler 配置；上一轮没跑完不会触发新轮 | `loan_sweep.py:80` |
| 单用户独立事务 | ✅ 每个 uid 独立 `async_session_maker()` + `session.begin()` | `loan_sweep.py:46-56`；用户 N 失败不回滚 N-1 |
| 全局回滚（rate 查询失败） | ✅ skip 整轮 | `loan_sweep.py:33-35` |
| 幂等（重复触发） | ✅ 实质幂等 | accrue 把 `last_accrued_at` 推到 now；后续短时间内再 tick，elapsed≈0 → no-op；不会双扣。但**不是显式幂等键**（无 sweep_id / batch_id），见 [P3-LOAN-06] |
| 用户 repay 与 sweep 同时 | ✅ | 两端都 `with_for_update`；先到拿锁，后到看到的是已结息后的状态 |
| 多实例部署冲突 | ❌ **没有分布式锁** | 见 [P2-LOAN-07] |
| 调度器进程崩溃 / 漏跑 | ✅ accrue 用 elapsed 自动补齐；下次触发时 `elapsed_sec` 是真实间隔 | `accrue_interest` 公式按时间累计而非按 tick 数 |
| 异常吞噬 | ✅（_tick_safe）然 ❌ 缺告警 | `loan_sweep.py:62-66` 只 `logger.exception`；连续失败无告警机制 |

**强平 / 风控**：

**审计结论：当前系统没有任何形式的强平（force-liquidate / margin call）。**

证据：
- `grep -rn "强平\|liquidate\|liquidation\|force.sell\|margin" backend/app/` 仅命中 Task 3 已审计的 `user.py:get_user_summary` 内的 LMSR 清算价值估算（用于「净资产」展示），**那是只读计算，不会触发卖出**。
- `loan_sweep.run_sweep_once` 只调 `accrue_interest`，没有任何卖持仓 / 扣 cash / 标记违约的代码路径。
- `compute_max_borrow` 只控**新增**借款上限，对已有 debt 因复利涨过净资产线（margin call 触发线）无任何反应——账户可以一直在水下（cash + holdings_value < debt），系统不会自动减仓。
- 唯一让 debt 减少的入口是用户主动 repay（消耗 cash）或管理员 forgive_debt。

**这意味着**：账户进入水下（net worth 为负）后，债务在 daily_rate=1% 下每天涨 1%，永远涨下去，直到 Decimal(16, 6) 溢出（理论上 ~10^10，按 1%/d 复利从 1000 起步约 1900 天 ≈ 5 年才会溢出）。**这是设计选择还是疏漏？需要产品确认**——但从安全审计角度看，这不是 P0/P1 资金漏洞（没有钱被无中生有），是产品功能缺口（坏账无清理机制）。Task 3 已标 `_holdings_value` buggy 让 max_borrow 偏高，加剧了这个缺口的暴露面。**整体评定 P2**（[P2-LOAN-08]）。

**60847ad fix 复核结论**：fix 实现正确，min 三方封顶逻辑严密。所有写入路径（borrow / repay / force_loan / forgive_debt / market.py buy/sell 的 accrue 钩点）都通过 `loan_service` 或 `_lock_user`，没有绕过封顶的旁路。

**5771b45 invariant 复核结论**：
- 不变量是 `u.debt >= 0 AND u.cash >= 0`，断言点在 `increase_debt` 末尾（`loan_service.py:72-74`）和 `decrease_debt` 末尾（`loan_service.py:114-116`）。
- 绕过路径分析：
  - market.py buy/sell 直接 `locked_user.cash -= pay`（行 448、`+= net` 行 566）——**不经 loan_service 的不变量检查**。但有 `if locked_user.cash < pay: HTTPException("现金不足")`（行 444）作为本地防御。卖出加 cash 不可能让 cash 变负。
  - market.py settle 路径（`u.cash += pay` 行 734）只增加 cash，不会让 cash 变负。
  - `force_loan` / `forgive_debt` 走 `loan_service`，受不变量保护。
  - 所有 debt 写入仅经 `loan_service` 两个函数 + `accrue_interest`（accrue 按 factor 乘正数 → 不可能为负）。
- **不变量在 loan 写入路径上无可绕过的旁路**；market.py 的 cash 写入有自己的 `cash < pay` 检查。整体防御层次完整，是合理的纵深防御。
- **DB CHECK 约束**（`ck_user_cash_non_negative` / `ck_user_debt_non_negative`）作为最终防线，即使应用层都漏，DB 也会 raise（事务回滚 + 500）。这是一道好底线。

#### 发现

##### [P2-LOAN-01] BORROW 额度预检 TOCTOU（concurrent borrow 可越线）
- **位置**：`backend/app/api/v1/loan.py:82-93`
- **类别**：并发安全 / 业务逻辑
- **复现**：
  1. `user` 从 `Depends(current_active_user)` 取（无锁）；`hv = await _holdings_value(...)` 也无锁；`max_borrow = compute_max_borrow(user, hv, k)` 用 PRE-lock snapshot。
  2. `if amount > max_borrow: 400` 通过。
  3. `await loan_service.increase_debt(...)` 在锁内 accrue 后只检查 `amount > 0` 和不变量 `debt >= 0 / cash >= 0`，**不重新校验 amount 是否在新 max_borrow 内**。
  4. 两个并发 borrow 请求都看到同一份 PRE-lock max_borrow=500，各自 amount=400 → 两个都通过预检 → 都进入 service → 两个都成功，最终 debt=800 实际超过 max_borrow=500。
- **影响**：
  - 用户能在并发请求下借超 max_borrow（违反杠杆 k 限制）。
  - 配合 Task 3 已发现的 `_holdings_value` 高估问题，可被攻击者用浏览器多 tab 同时点「借款」放大效果。
  - 不是 P0（钱不被无中生有，debt 同步增加，cash 守恒）；但是是 P1 资金风控偏差（可绕过最大杠杆）。
- **下调到 P2 的理由**：实际复现需要并发 + 用户主动操作；攻击收益是「多借一些钱」而不是「免费拿钱」（仍要付利息）；nginx 限速 `/loan` 一定程度收敛并发面（虽然 CLAUDE.md 限速段没列 loan，需要确认）。
- **修复建议**：
  ```python
  # 锁内重新校验
  async def increase_debt(..., max_borrow_check: Optional[Decimal] = None):
      ...
      if max_borrow_check is not None:
          # 锁内重算 max_borrow，amount > max_borrow 时 raise LoanServiceError
          ...
  ```
  或在 service 层增加 `max_active_debt_check` 参数，由 handler 传 `compute_max_borrow(user_locked, hv_locked, k)` 锁内重算。
- **状态**：未修，留 P1 阶段总结与 phase-2 实施

##### [P2-LOAN-04] ACCRUE 长闲置账户无利息上限
- **位置**：`backend/app/services/loan_service.py:15-27`
- **类别**：业务边界 / DoS / 经济安全
- **复现**：
  1. 用户借款 1000，立即停用账户（is_active=false 或长时间不登录、sweep 又因故停跑）。
  2. `last_accrued_at` 是借款那天；6 个月后某次 buy/sell/repay/force_loan 触发 accrue。
  3. `elapsed_sec = 86400 * 180`，`factor = 1 + 0.01 * 180 = 19`（按线性 elapsed 公式），`debt = 1000 * 19 = 19000`。
  4. 单次跳变 18000；用户没有任何提前预警机制。
- **影响**：
  - 单纯审计上不算「跑负」（debt 涨是合理逻辑），但产品体验灾难——用户回来发现欠 19 倍。
  - 配合 [P2-LOAN-08]（无强平）形成「越欠越多 + 不会强平」的债务永续循环。
  - 审计角度：debt 数值若涨过 `Decimal(16, 6)` 上限 ~9.9e9，会在 quantize 时溢出，触发 `decimal.InvalidOperation` → 500，导致后续 buy/sell/repay 全死。理论攻击面：恶意用户借大额后离线 → 制造系统级 500。
- **修复建议**：
  - accrue 内对 `elapsed_sec` 设上限（如 7 天，超出按 7 天累计），`max_elapsed_sec = 7 * 86400` 之类。
  - 或借款时强制定期归还机制 / 到期日。
  - 或 sweep 主动告警「单用户 debt > 阈值」。
- **状态**：未修，建议 phase-2 实施

##### [P2-LOAN-05] daily_rate 运行时调整会回溯计算整个 elapsed 区间
- **位置**：`backend/app/services/loan_service.py:25` + `api/v1/site_config.py:62-85`
- **类别**：业务逻辑 / 时序一致性
- **复现**：
  1. 用户欠 1000，`last_accrued_at = T0`，daily_rate=0.01。
  2. 一周后（T0+7d）admin 通过 `PUT /site-config/loan_daily_rate` 改成 0.001。
  3. 改完立即触发 sweep（或用户买/卖/还款触发 accrue）：`elapsed_sec = 86400*7`，但 `daily_rate` 取的是新值 0.001，`factor = 1 + 0.001*7 = 1.007`，应有 7% 利息只算到 0.7%。
  4. 反之亦然——admin 调高利率会让用户「补缴」过去 elapsed 的差额。
- **影响**：
  - 不会让 cash/debt 跑负（数学上 factor > 0）。
  - 但破坏「按当时利率结息」语义；用户体验上是「我借的时候说好 1%，怎么突然变 0.5%」（往下还好）或「突然变 5%」（投诉）。
  - 实操上 admin 几乎不会高频改 daily_rate，攻击面很窄。
- **修复建议**：
  - 调整 daily_rate 之前先全量 sweep（把 last_accrued_at 推到「当前 rate 已生效之前」的 now）。
  - 或在 site_config handler 改 daily_rate 时主动调用 `await loan_sweep.run_sweep_once()`。
- **状态**：未修，建议 phase-2 实施

##### [P2-LOAN-07] sweep 没有分布式锁，多实例会重复结息
- **位置**：`backend/app/services/loan_sweep.py:69-82`
- **类别**：分布式 / 部署
- **复现**：
  1. 部署改成多实例（k8s replica > 1）。
  2. 每个实例都启动自己的 APScheduler，每个间隔都跑一次 `run_sweep_once()`。
  3. 两个实例同时进入循环 → 同一用户被两次 `with_for_update`（其中一个等锁）→ 第一个把 last_accrued_at 推到 now → 第二个 accrue 时 elapsed≈0 → 实质 no-op。**所以幸运地不会双扣。**
- **影响**：
  - 当前实质幂等（依赖 with_for_update + last_accrued_at 推进语义），但是**隐式**幂等。
  - 任何对 accrue 公式的改动（比如改成「按 tick 数累计」而非「按 elapsed 累计」）都会把这个隐式属性破坏，造成多实例下双扣。
  - 当前部署是单实例（docker-compose），所以本期不影响。
- **修复建议**：
  - 显式分布式锁（Redis SETNX 或 PG advisory lock）保护 sweep 入口。
  - 或转移到外部 cron（k8s CronJob / systemd timer）+ 单实例确保。
- **状态**：未修，phase-2 多实例部署前必修

##### [P2-LOAN-08] 无强平 / 无坏账清理机制（产品缺口）
- **位置**：（缺失）`loan_sweep.py` 没有 force-liquidate 路径
- **类别**：业务功能 / 经济安全
- **复现**：
  1. 用户借满杠杆 → 行情走反 → `cash + holdings_value < debt`（净值为负）。
  2. 用户不主动 repay，sweep 持续累息 → debt 永远涨。
  3. 系统永不强平、不通知、不冻结。
- **影响**：
  - 不是 P0（没有钱凭空生成）。
  - 是产品缺口：坏账永久存在；max_borrow 公式靠 `cash + holdings_value` 算的「净资产」，不影响坏账存量。
  - 配合 [P2-LOAN-04] 长闲置 + 配合 Task 3 `_holdings_value` 高估，是债务永续 + 借款额度被高估的组合。
- **修复建议**：
  - 设计 phase-2 强平模块：净资产线触发自动卖出持仓还债，清盘后还不够则记坏账（DB 流水）。
  - **必须**先把 `_holdings_value` 修正为清算口径（带卖出滑点+手续费），否则强平依据是错的。
- **状态**：产品决策待定，建议 phase-2 立项

##### [P3-LOAN-02] `test_repay_exceeds_cash_400` 是 stale test（60847ad 后已不再 400）
- **位置**：`backend/tests/test_loan_api.py:145-149`
- **类别**：测试覆盖 / 回归保护
- **复现**：
  - 该测试 setup `cash=30, debt=200, last_accrued=None`，POST `/loan/repay {"amount": "100"}`，期望 `r.status_code == 400`。
  - 当前 handler 代码（60847ad 后）：`if user.cash <= 0 and user.debt > 0: 400` —— `user.cash=30 > 0` → 不早 reject。`decrease_debt` 内 accrue（last_accrued=None → no-op），`effective = min(100, 200, 30) = 30`，正常返回 200。
  - **该测试现在应该 fail**（除非 pytest 实际跑过未发现，需要本地复现）。
- **影响**：
  - 测试名暗示业务规则「repay 超出 cash 应 400」已不再适用——业务规则现在是「静默封顶到 cash，返回 200 + effective 字段」，这是 60847ad 主动改变的。
  - 旧测试还在断言旧行为，要么未实际运行，要么修复时漏更新。
  - 不影响生产代码。
- **修复建议**：
  - 验证测试是否在 CI 跑过（建议 `pytest backend/tests/test_loan_api.py::test_repay_exceeds_cash_400 -v`）。
  - 改为：断言 `r.status_code == 200 and r.json()["effective"] <= "30" and Decimal(r.json()["cash"]) == 0`。
  - 同时补一条新测试：`test_repay_caps_to_cash_when_amount_exceeds`（场景同 walk-through）。
- **状态**：本任务只读，记录待 phase-2 修

##### [P3-LOAN-03] accrue 用线性 elapsed 而非真复利公式
- **位置**：`backend/app/services/loan_service.py:25`
- **类别**：业务公式 / 长期偏差
- **复现**：
  - 公式：`factor = 1 + daily_rate * elapsed_sec / 86400`（线性 elapsed）。
  - 真复利：`factor = (1 + daily_rate)^(elapsed_sec/86400)` 或连续复利 `exp(daily_rate * elapsed_sec/86400)`。
  - 当 sweep 间隔短（60s）时，每次 elapsed_sec=60，单次 factor≈1.0000069；按 sweep 复利 1440 次/天，等效日利率 = `1.0000069^1440 ≈ 1.01005`，与目标 0.01 几乎一致——**因为 sweep 高频 + 线性单步近似**。
  - 当 sweep 跑不动（例如间隔变 1h 或漏跑 6h），单次 elapsed_sec=21600，单次 factor=1.0025，按 4 次/d 复利 → 等效日利率 1.01 (没变)。所以**线性 elapsed 公式在 sweep 频次任意时都给出近似日利率 = 设定值**——这是一个数学巧合（小数项展开）。
- **影响**：
  - 短间隔下精度足够；长间隔（如 [P2-LOAN-04] 6 个月没 accrue 一次）下 `factor = 1 + 0.01*180 = 19`，远高于真复利 `1.01^180 ≈ 6.0`——**会让长闲置账户被多收数倍利息**。
  - 这同时是用户层面的不一致（有的用户被 sweep 每 60s accrue 一次走低偏差路径，长期闲置用户走高偏差路径）。
- **修复建议**：
  - 改用 `Decimal((1 + daily_rate) ** Decimal(elapsed_sec/86400))`（需要 mpmath / decimal pow）。
  - 或在 [P2-LOAN-04] 修复时顺便用 elapsed_sec 上限 7d 隐式约束最大单次偏差。
- **状态**：未修，与 [P2-LOAN-04] 联动

##### [P3-LOAN-04] 缺乏 LoanRecord / 资金流水审计表
- **位置**：`backend/app/models/base.py`（缺失 LoanRecord 模型）
- **类别**：可观测性 / 合规
- **复现**：
  - 当前 borrow / repay / accrue / sweep / force_loan / forgive_debt 都只在 `logger.info` 输出文本日志（结构化字段在前缀）。
  - 没有任何持久化的 loan_record 表：用户视角无法看历史借还（前端 Loan.vue 只展示当前 quota）；admin 视角排查纠纷只能 grep 应用日志。
- **影响**：
  - 真发生纠纷（用户说「我没借 100」），admin 只能看应用日志（默认 7d 留存），无法做对账。
  - 不是安全洞，是合规 / 可观测性缺口。
- **修复建议**：
  - phase-2 加 `LoanRecord(id, user_id, type[borrow/repay/accrue/forgive/force], amount, debt_after, cash_after, daily_rate, reason, created_at)` 表；6 个写入入口都补流水。
  - 配合 admin 后台查询页。
- **状态**：未修，phase-2 立项

##### [P3-LOAN-09] sweep 无连续失败告警
- **位置**：`backend/app/services/loan_sweep.py:62-66`
- **类别**：可观测性
- **复现**：sweep tick 抛异常时只 `logger.exception`；连续多次失败（DB 故障、site_config 表丢失、code bug）无任何主动告警。
- **影响**：sweep 长期不跑 → 累积漏跑利息 → 长闲置账户突发跳变（[P2-LOAN-04] 触发条件之一）。
- **修复建议**：连续 N 次失败时主动 push 告警（webhook / 邮件）；或 admin 后台展示 sweep 健康状态卡片（last_success_at, last_failure_count）。
- **状态**：未修，phase-2 立项

#### 留给后续阶段的线索

- **强平 / 坏账模块**（[P2-LOAN-08]）属于产品 + 安全双重缺口，phase-2 必须立项；先决条件是修 `_holdings_value`（Task 3 P2）使其用清算口径估值。
- **borrow TOCTOU**（[P2-LOAN-01]）是本次 Task 4 找到的最有意义的并发安全问题，phase-2 修服务层「锁内重算 max_borrow」即可。
- **rate 调整回溯**（[P2-LOAN-05]）和 **sweep 多实例**（[P2-LOAN-07]）都是部署/运维层的隐患；当前单实例部署 + admin 不会频繁改 rate，本期不影响生产，但要写进运维 runbook。
- **LoanRecord 审计表**（[P3-LOAN-04]）建议合并到 phase-2 「资金流水」专题，与已有的 Position transactions 一起做一致性。
- **stale test**（[P3-LOAN-02]）属技术债，下次有人改 loan API 时一并清理。
- 长闲置 + 线性 elapsed 复利公式（[P2-LOAN-04] + [P3-LOAN-03]）联动：单次 fix（elapsed_sec 上限）能同时缓解两个问题。


### 兑换码资金流

**审计日期**：2026-05-09
**审计文件**：`backend/app/services/redemption.py` + `backend/app/api/v1/{redemption,admin_redemption}.py` + `backend/app/models/redemption.py` + `backend/app/schemas/redemption.py` + `backend/tests/test_redemption_{service,api}.py` + `deploy/nginx.conf`
**前置 fix 复盘**：
- `697730d` 技术债收尾 merge（14 commits）：涵盖 RedemptionTransaction 审计表、CSV 导入上限、单用户单批次购买 5 次上限
- `bed3553` feat(redemption): RedemptionTransaction 同事务写入，含 batch/partner 快照
- `11f5b1e` feat(redemption): 库存低位 / 售罄告警（纯前端 banner，无码库操作）

---

#### 码生成

| 检查项 | 结果 | 证据 |
|---|---|---|
| 字符集与长度 / 熵 | N/A — 码由外部合作方提供，通过 CSV 导入；系统不自动生成码 | `services/redemption.py:26-55` parse_csv_codes；`models/redemption.py:104` max_length=128 |
| 使用 secrets 模块 | N/A — 无系统内生成路径 | 全文无 `secrets.` / `random.` 调用 |
| DB UNIQUE 约束 | **存在** `uq_redemption_code_string` 全局唯一约束 | `models/redemption.py:98` UniqueConstraint("code_string") |
| 重试逻辑 | N/A — 码由外部提供，导入时 duplicate 直接 skip | `services/redemption.py:211-217` import_codes_commit |
| 码长度下限 | **无**：parse_csv_codes 仅验证上限（128），未验证下限；1-char 码合法 | `services/redemption.py:48` len(ln) > _MAX_CODE_LEN |

**说明**：系统采用"合作方提供 CSV，管理员导入"模型，不自动生成码。熵完全取决于合作方的码质量，系统层面无法保证。这是架构决策，**不属于系统安全缺陷**，但合作方应使用高熵码。

---

#### 兑换路径

| 检查项 | 结果 | 证据 |
|---|---|---|
| 单次兑换 state 原子性 | **安全**：user 行 FOR UPDATE 锁 + code 行 SKIP LOCKED 锁，两者在同一事务 | `services/redemption.py:87,115` |
| 同码并发双重兑换 | **不能**：SKIP LOCKED 语义保证同一 code 行只有一个事务能持有锁；第二个事务锁不到 code 则返回 SOLD_OUT | `services/redemption.py:109-120` |
| RedemptionTransaction 同事务写入 | **是**：session.add(RedemptionTransaction(...)) 在同一 session 内，调用方 await db.commit() 统一提交 | `services/redemption.py:134-143`；`api/v1/redemption.py:96` |
| 金额来源 | **安全**：amount = batch.unit_price（服务层读 DB，非客户端输入）| `services/redemption.py:141`；PurchaseRequest 仅含 batch_id |
| 批次状态校验 | **存在**：status != ACTIVE 时 raise PurchaseError | `services/redemption.py:93-94` |
| 批次状态锁 | **缺失（P3）**：batch 以 session.get 读取（无 FOR UPDATE）；admin 在极短窗口 archive 批次不会阻止正在执行的购买事务 | `services/redemption.py:90`（无 with_for_update） |
| 单用户单批次上限 | **存在**，应用层计数 _PER_USER_PER_BATCH_LIMIT=5；user FOR UPDATE 锁序列化同用户请求，计数安全 | `services/redemption.py:99-107`；user 锁见 line 87 |
| DB 层单用户上限约束 | **缺失（P2）**：仅应用层 count 校验，DB 无 (batch_id, bought_by_user_id) 计数约束；若绕过 API 层直接写 DB 可突破上限 | `models/redemption.py`：无该 CheckConstraint |
| 限速（/redemption/purchase）| **偏宽松（P2）**：命中 nginx `api_general` 区（20 r/s burst=40），无专属 purchase 限速；market buy/sell 有独立 `api_trade` 区 | `deploy/nginx.conf:23,99-101` |
| Replay（同 batch_id 重复提交）| **安全**：每次购买返回不同 code_string（从可用池取一个）；第 6 次请求触发 PER_USER_LIMIT_REACHED | `services/redemption.py:106-107` |
| import 可写入 active 批次 | **允许**：import_commit 无批次状态前置校验；admin 可向 active 批次追加新码 | `services/redemption.py:212-222`（仅查重，不检 status） |

**并发竞态详细分析**

场景：用户 A 同时发出 N 次 POST /redemption/purchase

```
Txn-1: SELECT user FOR UPDATE  → 拿锁
Txn-2: SELECT user FOR UPDATE  → 阻塞，等待
Txn-1: count owned = 4 (< 5)  → 通过
Txn-1: SELECT code SKIP LOCKED → 拿到 code-X
Txn-1: update cash, code, insert audit → commit
Txn-2: 继续  count owned = 5 (>= 5)  → PER_USER_LIMIT_REACHED
```

结论：同用户并发购买 **不能** 绕过 per-user 限制（user FOR UPDATE 序列化）。

场景：不同用户 A、B 并发购买同批次最后一个码

```
Txn-A: lock user-A → count owned-A = 0
Txn-B: lock user-B → count owned-B = 0
Txn-A: SKIP LOCKED → 拿到 code-X
Txn-B: SKIP LOCKED → 无可用码 (code-X 被锁) → SOLD_OUT
```

结论：不同用户并发 **不会双重兑换**（SKIP LOCKED 语义）。测试见 `tests/test_redemption_service.py:147-167`（注：SQLite skip，PG 验证）。

---

#### admin 批次管理

| 检查项 | 结果 | 证据 |
|---|---|---|
| 所有 admin endpoint 都有 auth gate | **是**：每个 handler 签名都含 `admin: User = Depends(current_superuser)` | `api/v1/admin_redemption.py:37,47,62,94,104,126,163,181` |
| 批次创建 unit_price 下限 | **存在**：Field(gt=Decimal("0")) | `schemas/redemption.py:97` |
| 批次创建 unit_price 上限 | **缺失（P3）**：无 lt/le 约束；管理员可设置任意大金额（如 1e15）批次；无总资金注入上限 | `schemas/redemption.py:97` |
| 批次撤销（archive）| **存在**：status 可设为 archived，阻止新购买；已购码保持 SOLD 状态不变 | `api/v1/admin_redemption.py:137-146` |
| ARCHIVED → ACTIVE 回滚 | **允许**：状态机无单向限制；ARCHIVED 可重新设为 ACTIVE；已有码重新可购 | `api/v1/admin_redemption.py:138-140` |
| ACTIVE 价格修改防护 | **存在**：unit_price 修改在 active 批次被 400 拒绝 | `api/v1/admin_redemption.py:134-135` |
| CSV 导入上限 | **存在**：256 KB / 5000 行双重硬上限 | `api/v1/admin_redemption.py:29,152-156` |
| 库存低位告警 | **仅前端 banner**：threshold=5，纯 UI 提醒，不阻止 archived 操作或新购买 | `11f5b1e` 仅改前端 `RedemptionBatches.vue` |
| sqladmin 中 RedemptionCode 是否注册 | **未注册**：admin.py 仅注册 User/Market/Outcome/Position/Transaction，RedemptionCode 未挂 ModelView | `app/core/admin.py:107-111` |

**注**：sqladmin 中虽未挂 RedemptionCode（code_string 不暴露），但 `models/redemption.py:91-94` 的注释明确警告了未来若注册必须排除 code_string 字段。

---

#### 枚举攻击面

| 检查项 | 结果 | 证据 |
|---|---|---|
| 有无"查询码状态"接口 | **无**：用户端无 "check code_string validity" endpoint | 全端点列表见 `api/v1/redemption.py` @router.* |
| 购买错误区分码状态 | **购买端无码级别枚举**：错误区分的是 batch 状态（SOLD_OUT/BATCH_NOT_ACTIVE），不区分具体某个码已用/未用/不存在 | `api/v1/redemption.py:85-95` |
| batch_detail 侧信道 | **轻微（P3）**：`GET /batches/{batch_id}` 返回 404 "批次不存在" vs 404 "批次不可用"；后者表明该 batch_id 存在但状态非 ACTIVE 或 partner inactive，可被已登录用户用于枚举 batch_id 合法性 | `api/v1/redemption.py:57,60` |
| 需要认证 | **是**：所有 user 端点均 Depends(current_active_user)，匿名请求被拒绝 | `api/v1/redemption.py` 全 handler |

---

#### RedemptionTransaction 审计完整性

| 检查项 | 结果 | 证据 |
|---|---|---|
| 每次成功购买都写入 | **是**：session.add(RedemptionTransaction(...)) 在 purchase_code 返回前执行，与 cash 扣款同事务 | `services/redemption.py:134-143` |
| 与 cash 更新同一事务 | **是**：user.cash 修改、code.status 修改、RedemptionTransaction.add 均在同一 AsyncSession，统一由 `await db.commit()` 提交 | `services/redemption.py:123-143`；`api/v1/redemption.py:96` |
| audit 表 immutable | **是（应用层）**：代码库中无 RedemptionTransaction 的 UPDATE/DELETE 路径；无 admin endpoint 修改或删除审计行 | 全 grep 仅 services/redemption.py:134 有 add 操作 |
| 有无 DELETE 路径 | **无**：RedemptionTransaction 仅被 session.add，未见 delete 调用 | `grep -rn "RedemptionTransaction" backend/app/` |
| 索引覆盖 | **是**：user_id(index)、code_id(index)、batch_id(index)、partner_id(index)、timestamp(index) 均建索引 | `models/redemption.py:71-81` |
| 审计覆盖范围 | **仅购买**：覆盖 purchase_code 成功路径；mark-used 操作不涉及资金，未记录（合理） | `api/v1/redemption.py:164-177` |
| 失败事务审计行 | **不写入**（正确）：PurchaseError 触发 rollback，RedemptionTransaction 随之回滚 | `api/v1/redemption.py:84` await db.rollback() |

---

**发现**：

#### [P2-REDC-01] 兑换购买接口无专属限速，落入 api_general（20 r/s burst=40）

- **位置**：`deploy/nginx.conf:99-101`；`api/v1/redemption.py:75-110`
- **类别**：限速 / 资金消耗
- **复现**：POST /api/v1/redemption/purchase burst 40 次，均不被 429；相比之下 market buy/sell 有专属 api_trade（10 r/s burst=20）
- **影响**：高并发下同一用户可快速消耗现金（受 per-user 上限 5 缓解），但跨批次购买不受 5 次限制；攻击者账号可在 2 秒内完成所有批次购买
- **修复建议**：在 nginx.conf 添加 `location /api/v1/redemption/purchase` 专属限速区，建议 3-5 r/s burst=10
- **状态**：未修复

#### [P2-REDC-02] per-user 上限仅应用层校验，无 DB 级别守护

- **位置**：`services/redemption.py:99-107`；`models/redemption.py`（无 CheckConstraint）
- **类别**：上限绕过（需 DB 直接写权限或服务层 bug）
- **复现**：若攻击者获得 DB 写权限，或未来新增不经 purchase_code 的代码路径（如 admin 礼包功能），可突破每批次 5 次限制
- **影响**：资金异常注入；规则绕过
- **修复建议**：在 RedemptionCode 上添加 DB 层 partial unique index 或 check constraint 作为守护层；或添加 DB 触发器（当前无迁移机制，实施需谨慎）
- **状态**：未修复；当前 user FOR UPDATE 序列化保证应用层安全，但无 DB 层硬约束

#### [P3-REDC-03] batch_detail 侧信道：404 消息区分 "不存在" vs "不可用"

- **位置**：`api/v1/redemption.py:57,60`
- **类别**：信息泄露 / 枚举辅助
- **复现**：已登录用户 GET /api/v1/redemption/batches/999 → "批次不存在"；GET .../batches/1（archived 批次）→ "批次不可用"；后者泄露 batch_id=1 存在
- **影响**：低危；需已登录；可帮助攻击者枚举有效 batch_id 范围，但现有架构中批次 ID 无保密性要求
- **修复建议**：统一返回 404 "批次不存在"，不区分存在性
- **状态**：未修复

#### [P3-REDC-04] BatchCreate 无 unit_price 上限约束

- **位置**：`schemas/redemption.py:97`（仅 gt=Decimal("0")）
- **类别**：管理员操作加固
- **复现**：superuser POST /api/v1/admin/redemption/batches {"unit_price": "9999999999"} → 合法创建
- **影响**：误操作 admin 可创建极高面值批次；无总资金注入上限。当前 admin 为可信角色，影响有限
- **修复建议**：添加 Field(le=Decimal("10000")) 或类似业务合理上限；或在 admin 日志中记录
- **状态**：未修复

#### [P3-REDC-05] 批次状态机无单向约束，ARCHIVED 可重新 ACTIVE

- **位置**：`api/v1/admin_redemption.py:138-140`
- **类别**：管理员操作加固 / 审计一致性
- **复现**：PATCH /api/v1/admin/redemption/batches/{id} {"status": "archived"} 后再 {"status": "active"} → 均成功；已购 SOLD 码不变，剩余 AVAILABLE 码重新可购
- **影响**：intentional 撤销后被意外/恶意复活；历史 RedemptionTransaction 与再次激活的批次上下文割裂
- **修复建议**：若业务无复活需求，添加状态转换白名单（draft→active，active→archived 为合法方向，archived→* 禁止）
- **状态**：未修复

#### [P3-REDC-06] CSV 导入不校验码长度下限，1-char 码合法

- **位置**：`services/redemption.py:48`（仅 len(ln) > 128 判 invalid）
- **类别**：输入验证加固
- **影响**：admin 可导入极短码（如 "A"）；配合合作方网站可能导致暴力猜测该码；系统层面低危（合作方责任）
- **修复建议**：parse_csv_codes 增加 `len(ln) < MIN_CODE_LEN`（建议 8）归入 invalid
- **状态**：未修复

---

**留给后续阶段的线索**：
- `[P2-REDC-01]` 兑换购买限速是 Task 8（admin gate 覆盖矩阵）中 nginx 限速矩阵的一部分，建议统一在 Task 8 中做全端点限速复核
- `[P3-REDC-05]` 批次状态机约束与业务需求相关，建议在 phase-2 「合作方合规」专题中作为功能需求确认后再实施
- RedemptionTransaction 目前仅覆盖购买，若 phase-2 引入「管理员礼包（admin grant）」功能，必须同样写入审计行
- sqladmin 未注册 RedemptionCode 是当前的保护，但注释已提醒未来若注册必须排除 code_string 字段——建议在 `core/admin.py` 添加显式注释或文档，防止后续维护者忽视

### SSO / Casdoor / Token
（Task 6 填写）

### 首位 admin 自动晋升竞态
（Task 7 填写）

### admin gate 覆盖矩阵
（Task 8 填写）

### IDOR / 横向越权矩阵
（Task 9 填写）

### models/base.py 无迁移机制风险
（Task 10 填写）

### 静态工具 triage 结果
（Task 11 填写）

## 不在范围（已识别但本阶段不审）

- （边审边记录）

## 阶段统计

（最后填写：P0/P1/P2/P3 各几条）
