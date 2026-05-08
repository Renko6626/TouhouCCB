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
（Task 2 填写）

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
