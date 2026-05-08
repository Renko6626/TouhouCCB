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
（Task 3 填写）

### 贷款 / 复利 / 还款
（Task 4 填写）

### 兑换码资金流
（Task 5 填写）

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
