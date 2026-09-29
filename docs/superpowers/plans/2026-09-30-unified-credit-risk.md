# LMSR + 多 FX 统一信贷风险与固定强平 Implementation Plan（工作包版）

> **For agentic workers:** 本计划刻意粗化为 **8 个可独立交付的工作包（WP1–WP8）**，每个包由一个 worker 负责、独立合并、独立验收；包内只保留必要步骤与验收测试，不把单个测试拆成任务。WP 之间用 §5 的波次表协调并行/串行与文件所有权。每个 WP 合并前必须在证据目录留 `wp-N-report.md`（命令、原始输出、结论）。不部署、不推送、不跑写生产库的测试、不读密钥。

**Goal:** 把"仅 LMSR、按 margin 比率、逐市场尽力而为"的借贷/强平，改成 `单一 User.debt + 统一利率 + 跨产品抵押` 的账户级信用体系：用每个产品自己的真实清算算法（LMSR 组内滚动 q、FX 按 pair AMM）算风控净值 `E`，只保留初始/维持两条门槛；强平固定为"定时扫描 + 每账户每轮一个动作 + 现金优先还债 + 清算价值降序选组 + 10% 批次（E≤0 全卖）"。

**Architecture:** 只新增执行正确性所需部件：只读统一估值服务（纯数学 + 批量快照）、两条门槛的统一风险服务、按品种读写门闩、`LiquidationRun/Action` 幂等与恢复记录。LMSR 继续走每市场 writer 串行，FX 继续走每 pair 行锁串行；不引入全局经济锁。展示净值用 MTM，风控与强平只用真实清算净值。事件触发 / dirty / outbox 不在本期。

**Tech Stack:** Python 3.13 / FastAPI / SQLModel + SQLAlchemy(async) / Alembic / PostgreSQL(生产) + SQLite(单测) / Decimal(16,6) / APScheduler / Vue 3 + TypeScript（仅展示接线）/ pytest / k6（既有 `loadtest/`）。

**Spec（唯一有效设计）:** [`docs/superpowers/specs/2026-09-29-unified-credit-risk-design.md`](../specs/2026-09-29-unified-credit-risk-design.md)
**明确排除:** [`docs/superpowers/specs/2026-09-30-event-driven-liquidation-deferred.md`](../specs/2026-09-30-event-driven-liquidation-deferred.md)
**配套:** [`docs/fx.md`](../../fx.md)、[`docs/holdings-value-semantics.md`](../../holdings-value-semantics.md)、[`docs/migrations.md`](../../migrations.md)、[`docs/season-reset-2026-09-27.md`](../../season-reset-2026-09-27.md)、[`CLAUDE.md`](../../../CLAUDE.md)。

**分支:** `ralph/2026-09-30-unified-credit-risk`。`git add <path>`，不 `-A`，不 push。
**证据目录:** `.superpowers/sdd/2026-09-30-unified-credit-risk/`（`wp-N-report.md`、`perf/`、`rollout/`）。

---

## 0. 全局约束（不变式，任何 WP 不得偏离）

1. **单一债务、统一利率**：`User.cash` / `User.debt` / `User.debt_last_accrued_at` 是现金与总债务唯一权威；不新增 `CreditLiability`、分来源负债、还款队列、债务投影、多利率。
2. **两种净值，边界不得混用**：`display_equity = cash + MTM_lmsr + MTM_fx − D_effective`（MTM 含 HALT/paused 资产）；`liquidation_equity(E) = cash + ΣL_group − D_effective`（每 LMSR market / FX pair 用各自真实清算算法；不可执行资产 `L=0`）。借款额度、下单准入、强平触发/停止只用 `E`。
3. **真实组合清算**：LMSR 把同一 market 全部 outcome 放在**同一滚动 q 副本**上按 `outcome_id` 升序模拟卖出；FX 按 pair 用当前储备 `quote_sell`；禁止"各持仓独立清算相加"或"边际价×数量"替代。
4. **固定强平**：仅定时扫描（`liquidation_sweep_interval_sec` 默认 600，5–7200）；run-now 复用同一入口；现金优先还债；按 `(-L_group, product, group_id)` 选组；每账户每轮一个批次（`liquidation_partial_pct` 默认 10%，向上量化并封顶；`E<=0` 选中组全卖）；提交后本轮结束，下一轮扫描再继续。
5. **无事件驱动**：交易、行情、利息、新闻、暂停恢复、注撤资都不启动/续接强平；不建 dirty 表、outbox、实时重估、事件工作队列。
6. **无全局经济锁**：保留 LMSR 每市场 writer 串行与 FX 每 pair 行锁串行；不同市场/不同 pair 必须能并发提交。共享抵押品用按品种门闩 + 用户经济版本复检，不用全站互斥。
7. **性能契约**：无债且本操作不新增债务时走快路径（目标品种独占门闩 + user 锁，不展开全组合）；交易不按持仓用户数 fan-out；FX 成交响应不等 24h volume 聚合；扫描按 user_id 稳定分页（20/页）但每轮遍历**全部** `debt>0` 用户，最多 3 个执行 worker；排队不占 DB 连接、不持用户锁；改造前后性能对照是验收项。
8. **FX 执行器事务化**：报价/成交核心是"调用方事务内函数"，不 commit、不发 SSE；提交后发布走后台。
9. **迁移与回退**：迁移全部 additive（旧镜像可在升级后的库运行）；幂等、崩溃恢复、审计、赛季重置、PostgreSQL 并发测试是验收项。
10. **命令红线**：不部署、不推送、不改 `.env*` / `deploy/` / `.github/workflows/` / `docker-compose.yml` / 受保护 core 文件；不在有数据的库跑 `init_db.py`；不 `DROP` / `TRUNCATE`；不读密钥。（`backend/init_db.py` 的授权范围见 F10。）

---

## 1. 冻结决策（已由用户确认，实现直接执行，不再询问）

- **F1 单一债务/统一利率**：见 §0.1；所有产品共用 `loan_service` 的借还、计息与 `ledger_service` 账本。
- **F2 MTM 展示 / E 风控**：见 §0.2。资产页、排行榜主数字用 `display_equity`，另显示"清算净值"与保证金状态；贷款额度与强平只用 `E`。
- **F3 固定强平算法**：见 §0.4。每账户每轮最多一个卖出批次；无最优清算搜索、无候选收益排名、无紧急保证金率、无强平罚金。
- **F4 无事件触发/无 dirty/outbox/无全局锁**：见 §0.5、§0.6。
- **F5 清算费率（统一规则）**：强平按该产品**普通卖出费率**收费，不额外收费。LMSR 取 `site_config.sell_fee_rate`（`loan_migrate.py:52`，默认 `"0"`），FX 取 per-pair `sell_fee_rate`。理由是 spec §3.1 的统一规则要求估值与执行同费率口径；**不把"现状估值扣费、执行免费"描述为数学错误**（保守估值本身合法）。非零费率上线前必须公示，但**代码不硬编码任何缓冲期**；公示文案与时长是发布参数。
- **F6 维持率 = 0.04**：`credit_maintenance_ratio = 0.04`，`0.04 < 1/19 ≈ 0.05263`，满足 20x（`R_initial = 1/(20-1)`）；**分母是含息债务 `D_effective`**，它不是价格跌幅、也不构成无穿仓保证。迁移期若现有 `liquidation_hard_threshold` 有效（`0 < hard < R_initial(当次 credit_leverage)`）可先沿用它；**20x 启用前统一设为 0.04 并写审计**。测试 fixture 可直接用 0.04。
- **F7 迁移不放松授信**：迁移时 `credit_leverage = loan_leverage_k + 1`（与旧 `compute_max_borrow` 精确等价），保持或收紧授信；**20x 只在 PG 校准与影子期通过后由运营显式、审计地启用**。
- **F8 首期最多 3 个 `trading` FX pair**：`admin_fx.py` 解除"仅一个 pair"限制但强制 `count(status='trading') <= 3`；更多 pair 需单独运营批准。**上限只是管理端运营限制，数据模型与估值接口不得写死为三个品种**（新增 pair 不需要改 schema 或估值代码）。
- **F9 paused / reduce_only 双轴语义（不得靠迁移隐式改变）**：`reduce_only` 是显式管理端选项，`paused` 默认仍是**全停**：
  - `trading` + `reduce_only=false`：正常开平仓与系统干预。
  - `trading` + `reduce_only=true`：只允许卖出/强平，拒绝开仓与系统干预；`L` 按可执行报价计算。
  - `paused` + `reduce_only=false`：**保留旧语义的全停状态**，不成交、不系统干预、`L=0` 且阻塞该组。
  - `paused` + `reduce_only=true`：管理端显式选择的"只许减仓"，允许用户卖出与定时强平，不许买入/系统干预，`L` 按可执行报价计算。
  - 真正 halted、不可执行或无有效报价（`quote_sell` 失败或产出为 0）：`L=0` 并阻塞（不清仓、不删除持仓）；`draft`/`closed` 等同不可执行。
  - 迁移只新增 `reduce_only`（默认 `false`），**不得把已存在的 `paused` pair 无提示地变成可卖**；只减仓必须由管理端显式开启并留审计。
- **F10 `init_db.py` 授权**：已授权**只补** `app.models.{credit,fx,bot}` 的 metadata import（`# noqa: F401`），修复已核实的"空库缺 FX/bot 表"缺陷（`import init_db` 只有 21 张表 vs `import app.main` 27 张）；不得改其清库/示例数据逻辑。
- **F11 公开页不展示强平 run 详情**：`/loan/recent-liquidations` 保持既有公开摘要字段（`LiquidationEvent` 既有列 + 新增可空 `product`），不暴露 `liquidation_run/action` 内部字段。账户端可看 `risk_status`/`credit_frozen`，管理端可看 run/action。
- **F12 实现细节冻结**：LMSR 最小卖出单位 = 1 股（`ROUND_CEILING`，封顶持仓），FX = 0.000001 外币（`ROUND_CEILING`，封顶余额）；组排序 `(-L, product, group_id)`，`"fx" < "lmsr"`；`LiquidationEvent` 每"实际卖仓或还债的回合"一条；新增内容白名单 = `User.economic_version`、`User.credit_frozen`、`liquidation_run`、`liquidation_action`、`services/credit/` 包、按品种门闩、版本化估值缓存、配置键、审计类型、`liquidation_events.run_id/product`、`fx_pair.reduce_only`。

---

## 2. 现状基线（改动前先复读对应文件）

| 关注点 | 现状 | 差距 |
| --- | --- | --- |
| 债务权威 | `User.cash/debt/debt_last_accrued_at` + `loan_service` + `ledger_service` | 保留；加 `pending_debt` 纯函数与 `economic_version` |
| 借款额度 | `compute_max_borrow = max(0, k×(cash−debt+hv)−debt)`（`loan_service.py:220`），`k ≤ 10`（`api/v1/site_config.py:69`） | 改成 `max(0, E/R_initial − D)`；等价映射 `k = 1/R_initial` |
| LMSR 估值 | `compute_users_holdings_value` 逐 position 在未变化 q 上独立全卖相加（`wealth.py:101-127`）；另有 MTM | 新增按 market 滚动 q 的组报价 |
| FX 估值 | `compute_fx_mtm`（`fx/valuation.py`，仅展示） | 新增按 pair `quote_sell` 净回收 |
| 触发 | `margin = (cash−debt+hv)/debt` 对比 `liquidation_hard_threshold` 0.2 / `soft` 0.5（`liquidation_sweep.py:105`） | 改成 `D>0 且 E < R_maintenance×D`；停止 `D==0 或 E >= R_initial×D` |
| 强平执行 | `liquidate_user`（单事务全平，`fee=ZERO`）/ `liquidate_user_split`（逐 market 各自事务 + 阶段 C，`liquidation_service.py:370-502`）/ `op_liquidate_market`（`writer_ops.py:606-730`） | 改成组级一次提交；legacy 保留到开关切换完成 |
| FX 执行器 | `execute_trade` 自 commit + publish（`fx/trading.py:208-211`）；`publish_trade` 拉 24h volume（`fx/market_data.py:123-139`、`trading.py:242-247`） | 拆事务内函数；发布移出响应路径 |
| 锁序 | FX `pair→user→wallet→treasury`（`fx/trading.py:127,165,187`）；LMSR writer 内 user→position，outcome 绝对 SET | 保留；门闩在最外层 |
| 命名空间 | broker topic 裸 int（`realtime.py:60`）；FX 用 pair_id，LMSR 用 market_id | 改成带产品前缀的 topic / 限流键 |
| 单 FX pair | `admin_fx.py:168,195` 强制唯一 trading pair | 改 F8（≤3） |
| 扫描 | 一次性全量候选、无分页、30 分钟"卡水下"冷却、`Semaphore(3)` | 分页 20、全候选、去无差别冷却、记录阻塞原因 |
| 经济写入口 | `writer_ops.py`（buy/sell/resolve/liquidate）、`api/v1/market.py` legacy（639/831/1059）、`fx/trading.py`、`loan_service.py`、`admin_user_service.py`（adjust_cash:61 / batch:264 / amnesty:338 / force_loan / forgive_debt）、`redemption.py:129`、`danmuku.py:102`、`pve/service.py:99,138,238`、`fx/scheduler.py` fund/withdraw（**未 bump `pool_version`**） | 全部接入版本自增与风险检查 |
| 赛季重置 | `scripts/season_reset.py` 的 `CLEAR_ORDER` / `FX_CLEAR_ORDER` / `PRESERVED_AUDIT_TYPES` | 新表/新审计类型纳入，replay 自检不失败 |

---

## 3. 目标模块与冻结接口（并行 worker 的契约，不得改名）

### 3.1 新包 `backend/app/services/credit/`

```text
credit/
  __init__.py
  keys.py        # GroupKey / 排序 / 命名空间（纯函数）
  thresholds.py  # R_initial / R_maintenance 派生与比较（纯函数）
  version.py     # bump_economic_version(user)
  lmsr_quote.py  # LMSR 组清算报价（纯数学）
  fx_quote.py    # FX pair 清算报价（复用 fx.amm.quote_sell）
  valuation.py   # 批量快照 + MTM / E / 分组明细（只读）
  gates.py       # 按品种读写门闩（asyncio，进程内）
  risk.py        # 交易后风险检查、借款额度、消费检查
  runs.py        # LiquidationRun / LiquidationAction 持久化与幂等
  execution.py   # 每账户每轮编排
  sweep.py       # 定时扫描（分页、并发上限、指标）
  ownership.py   # 单写实例所有权保护
```

`liquidation_sweep.py` 保留公开名 `run_liquidation_sweep_once` / `start_scheduler` / `stop_scheduler` / `reschedule`，内部改调 `credit.sweep`；消费方 `main.py` lifespan、`api/v1/admin_liquidation.py`、`api/v1/site_config.py` 不改签名。

### 3.2 接口契约（签名级）

```python
# credit/keys.py
Product = Literal["lmsr", "fx"]
@dataclass(frozen=True, order=True)
class GroupKey:
    product: Product
    group_id: int
def symbol_namespace(product: Product, group_id: int) -> str          # "lmsr:{id}" / "fx:{id}"
def sort_groups_by_liquidation(groups: Sequence[GroupLiquidation]) -> list[GroupLiquidation]

# credit/thresholds.py
@dataclass(frozen=True)
class RiskThresholds:
    leverage: Decimal
    r_initial: Decimal
    r_maintenance: Decimal
    def triggered(self, equity: Decimal, debt: Decimal) -> bool       # debt > 0 且 equity < r_maintenance*debt
    def recovered(self, equity: Decimal, debt: Decimal) -> bool       # debt == 0 或 equity >= r_initial*debt
    def max_borrow(self, equity: Decimal, debt: Decimal) -> Decimal   # max(0, equity/r_initial - debt)，6dp 向下
def derive_thresholds(leverage: Decimal, maintenance: Decimal) -> RiskThresholds   # prec=28
def validate_thresholds(leverage: Decimal, maintenance: Decimal) -> None           # 非法抛 ValueError

# credit/version.py
def bump_economic_version(user: User) -> int      # 就地 +1 并返回；调用方负责 commit

# credit/lmsr_quote.py
@dataclass(frozen=True)
class OutcomeSnapshot:
    outcome_id: int
    total_shares: Decimal
    status: str
    closes_at: datetime | None
@dataclass(frozen=True)
class LmsrLeg:
    outcome_id: int
    amount: Decimal
    gross: Decimal
    fee: Decimal
    net: Decimal
@dataclass(frozen=True)
class LmsrGroupQuote:
    market_id: int
    mode: Literal["partial", "full"]
    legs: tuple[LmsrLeg, ...]
    gross: Decimal
    fee: Decimal
    net: Decimal
    blocked_reason: str | None
def quote_lmsr_group(
    outcomes: Sequence[OutcomeSnapshot], positions: Mapping[int, Decimal], *,
    market_id: int, b: float, fee_rate: Decimal,
    mode: Literal["partial", "full"], partial_pct: Decimal,
    unit: Decimal = Decimal("1"),
) -> LmsrGroupQuote

# credit/fx_quote.py
@dataclass(frozen=True)
class FxPairSnapshot:
    pair_id: int
    status: str            # draft|trading|paused|closed
    reduce_only: bool
    gold_reserve: Decimal
    foreign_reserve: Decimal
    sell_fee_rate: Decimal
@dataclass(frozen=True)
class FxGroupQuote:
    pair_id: int
    mode: Literal["partial", "full"]
    foreign_in: Decimal
    fee_foreign: Decimal
    gold_out: Decimal
    post_gold_reserve: Decimal
    post_foreign_reserve: Decimal
    blocked_reason: str | None
def quote_fx_group(
    pair: FxPairSnapshot, *, foreign_amount: Decimal,
    mode: Literal["partial", "full"], partial_pct: Decimal,
    unit: Decimal = Decimal("0.000001"),
) -> FxGroupQuote

# credit/valuation.py（只读；不写库，不推进 debt_last_accrued_at）
@dataclass(frozen=True)
class GroupLiquidation:
    key: GroupKey
    value: Decimal
    executable: bool
    blocked_reason: str | None
@dataclass(frozen=True)
class AccountValuation:
    user_id: int
    cash: Decimal
    debt_persisted: Decimal
    debt_effective: Decimal
    mtm_lmsr: Decimal
    mtm_fx: Decimal
    display_equity: Decimal
    liquidation_equity: Decimal
    groups: tuple[GroupLiquidation, ...]
    economic_version: int
async def value_users_batch(session: AsyncSession, user_ids: Sequence[int], *, daily_rate: Decimal) -> dict[int, AccountValuation]
async def value_user_detailed(session: AsyncSession, user_id: int, *, daily_rate: Decimal, lock: bool = False) -> AccountValuation

# credit/gates.py
class SymbolGateSet:
    def hold(self, *, exclusive: Sequence[GroupKey], shared: Sequence[GroupKey]) -> AbstractAsyncContextManager[None]
    def held_keys(self) -> frozenset[GroupKey]
    # 单批全序：重复键合并为独占 → 按 (product, group_id) 升序 acquire → 逆序释放
    # 禁止分批增量获取；禁止持闩等待另一个 writer 命令

# credit/risk.py（调用方已持门闩）
@dataclass(frozen=True)
class DependencySet:
    economic_version: int
    cash: Decimal
    debt: Decimal
    debt_last_accrued_at: datetime | None
    groups: tuple[GroupKey, ...]
    holdings: Mapping[GroupKey, Mapping[int, Decimal]]
@dataclass(frozen=True)
class PostTradeState:
    cash: Decimal
    debt: Decimal
    lmsr_q: Mapping[int, tuple[Decimal, ...]]
    fx_reserves: Mapping[int, tuple[Decimal, Decimal]]
    fee_rates: Mapping[GroupKey, Decimal]
@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str | None      # insufficient_initial_margin|credit_frozen|frozen_by_operator|version_conflict
    equity_after: Decimal
    debt_after: Decimal
    max_borrow: Decimal | None
async def discover_dependencies(session: AsyncSession, user_id: int) -> DependencySet
async def check_new_risk(
    session: AsyncSession, *, user: User, deps: DependencySet, post: PostTradeState,
    thresholds: RiskThresholds, partial_pct: Decimal, now: datetime,
) -> RiskDecision
async def check_cash_spend(
    session: AsyncSession, *, user: User, deps: DependencySet, spend: Decimal,
    thresholds: RiskThresholds, partial_pct: Decimal, now: datetime,
) -> RiskDecision

# credit/runs.py
async def get_or_create_active_run(session: AsyncSession, *, user_id: int, trigger_source: str, now: datetime) -> LiquidationRun
async def record_action(
    session: AsyncSession, *, run: LiquidationRun, round_no: int, kind: str,
    product: str | None, group_id: int | None, mode: str | None,
    requested: dict | None, executed: dict | None,
    proceeds: Decimal, fee: Decimal, fee_currency: str | None, repaid: Decimal,
    debt_after: Decimal, cash_after: Decimal, economic_version_after: int,
    blocked_reason: str | None,
) -> LiquidationAction
async def close_run(session: AsyncSession, *, run: LiquidationRun, status: str, now: datetime) -> None

# credit/execution.py
@dataclass(frozen=True)
class RoundResult:
    outcome: Literal["no_action", "repaid", "sold", "recovered", "insolvent", "blocked"]
    product: str | None
    group_id: int | None
    proceeds: Decimal
    repaid: Decimal
    fee: Decimal
    blocked_reason: str | None
async def liquidate_account_round(*, user_id: int, trigger_source: str, gates: SymbolGateSet) -> RoundResult

# credit/sweep.py
async def run_sweep_once(trigger_source: str) -> dict[str, int | str]
```

### 3.3 新模型 `backend/app/models/credit.py`

```python
class LiquidationRun(SQLModel, table=True):
    __tablename__ = "liquidation_run"
    __table_args__ = (
        Index("uq_liquidation_run_active_user", "user_id", unique=True,
              postgresql_where=text("status = 'active'"), sqlite_where=text("status = 'active'")),
        Index("ix_liquidation_run_status_updated", "status", "updated_at"),
    )
    # id / user_id(FK user.id) / status(active|recovered|insolvent|blocked|stopped)
    # trigger_source / started_at / updated_at / next_round(1) / rounds(0)
    # last_group_product / last_group_id / last_blocked_reason / closed_at
    # pre_cash / pre_debt / pre_liquidation_equity / total_proceeds / total_repaid / total_fee

class LiquidationAction(SQLModel, table=True):
    __tablename__ = "liquidation_action"
    __table_args__ = (
        UniqueConstraint("run_id", "round_no", name="uq_liquidation_action_run_round"),
        Index("ix_liquidation_action_user_created", "user_id", "created_at"),
    )
    # id / run_id(FK liquidation_run.id) / user_id(FK user.id) / round_no
    # kind(sell_group|repay_cash|repay_only|blocked|stopped) / product / group_id / mode
    # requested(JSON) / executed(JSON) / proceeds / fee / fee_currency("gold"|"foreign")
    # repaid / debt_after / cash_after / economic_version_after / blocked_reason / created_at
```

`User` 新增：`economic_version`（int，`server_default "0"`）、`credit_frozen`（bool，`server_default "false"`）。
`LiquidationEvent` 新增可空列：`run_id`（FK `liquidation_run.id`，`ondelete="SET NULL"`）、`product`（`max_length=8`）。
`FxPair` 新增：`reduce_only`（bool，`server_default "false"`）。

### 3.4 配置键

| key | 值 | 说明 |
| --- | --- | --- |
| `unified_credit_enabled` | `false` | 统一引擎总开关；翻转需重启 |
| `credit_new_risk_frozen` | `false` | 切换窗口"停增险"闸；热生效 |
| `credit_leverage` | 迁移时 `loan_leverage_k + 1` | 范围 `(1, 20]`；20x 需 F7 的显式启用 |
| `credit_maintenance_ratio` | 迁移期沿用有效 hard 阈值；20x 前设为 `0.04` | 必须 `< R_initial`（F6） |
| `credit_risk_retry_limit` | `3` | 版本冲突重试上限 |

复用不改语义：`liquidation_enabled`(false)、`liquidation_sweep_interval_sec`(600, 5–7200)、`liquidation_partial_pct`(0.10)、`loan_daily_rate`、`sell_fee_rate`、`loan_enabled`。

### 3.5 新增审计事件类型

`liquidation_run_start`、`liquidation_action`、`liquidation_blocked`、`liquidation_run_close`、`credit_freeze_set`。必须加入 `AUDIT_EVENT_TYPES`；`audit_replay.fold` 对未知类型不校验增量但会读 `user_after`，因此新事件的 `user_after` 必须是提交后的权威快照；资金影响仍由同事务的 `trade_liquidate` / `liquidation_repay` / `fx_trade` 承担。

---

## 4. Review Focus（每个 WP 审查按此优先）

- 过期抵押放贷、只重验当前交易品种、用旧快照放行 → 直接判不通过。
- 重复卖仓 / 重复还债 / 重复启动 run → 唯一键与 run 行锁必须挡住；崩溃后重扫不重复执行。
- 锁序循环、持闩等待另一个 writer、按品种门闩分批获取 → 直接判不通过。
- 组清算数学：滚动 q、`outcome_id` 升序、费用 6dp、`net == gross - fee`、暂停资产 `L=0` 但 MTM 保留。
- 交易事务内按持仓用户数 fan-out、写 dirty/outbox、等待 24h volume → 判不通过。
- 估值缓存新鲜度只能由版本决定，不得依赖 TTL 猜测。
- 同一时刻只能有一个强平执行者卖仓（legacy 与新编排不得并存）。
- 经济写入口不得有"没 bump 版本 / 没做风险检查"的旁路（含 admin service、PvE、赛季重置、legacy market 路径）。
- 公开响应不得出现 `liquidation_run/action` 内部字段（F11）。

---

## 5. 工作包总览

| WP | 交付物 | 文件所有权（独占） | 依赖 | 可并行于 | 合并门槛（摘要） |
| --- | --- | --- | --- | --- | --- |
| **WP1** 基座 | schema/迁移、`init_db` 注册、配置与校验、flags、审计/赛季重置、`credit/{keys,thresholds,version}.py`、`pending_debt`、PG 基座、性能基线 | `models/{credit,base,fx,audit}.py`、`alembic/versions/*`、`init_db.py`、`main.py`、`services/loan_migrate.py`、`api/v1/site_config.py`、`scripts/season_reset.py`、`services/credit/{keys,thresholds,version,flags}.py`、`services/loan_service.py`、`pytest.ini`、`tests/pg/*` | — | 无（第一合并） | 迁移双库幂等；开关 false 零行为变化；冻结接口落地 |
| **WP2** 估值 | `lmsr_quote`、`fx_quote`、`valuation`、影子 CLI | `services/credit/{lmsr_quote,fx_quote,valuation}.py`、`scripts/credit_shadow_report.py` | WP1 | WP3a、WP4a | 滚动 q 对照；MTM/E 分离；≤6 SELECT/100 用户；无运行时引用 |
| **WP3** 风险件 | 门闩、风险检查 + 版本缓存、runs、单写所有权 | `services/credit/{gates,risk,runs,ownership}.py`、`tests/pg/test_pg_writer_ownership.py` | WP1（risk 另需 WP2） | WP2、WP4a | 门闩 20 次稳定无死锁；风险决策单测；run 幂等；PG 所有权 |
| **WP4** FX 执行器 | 事务内拆分、后台发布、FX 强平卖出、reduce_only/paused 语义 | `services/fx/{trading,market_data,publisher}.py` | WP1（强平卖出另需 WP2） | WP2、WP3a | 玩家行为对等；发布不阻塞响应；分币种守恒；锁序 |
| **WP5** LMSR 强平 | 组级 op、F5 费率、legacy 开关化 | `services/writer_ops.py`、`services/market_writer.py`、`services/liquidation_service.py` | WP1、WP2、WP3 | WP4、WP8a | 零费率新旧逐字段一致；非零费率 `cash = net`；同事务还债；幂等 |
| **WP6** 准入接入 | 全部经济写入口的门闩/版本/风险检查 | `services/writer_ops.py`、`services/market_writer.py`、`services/fx/trading.py`、`api/v1/market.py`、`api/v1/loan.py`、`services/{admin_user_service,redemption,danmuku,loan_service}.py`、`services/pve/service.py`、`services/fx/scheduler.py`、`scripts/season_reset.py` | WP2、WP3、WP4、WP5 | WP8a | 无债 SQL 计数不增；跨产品抵押不可复用；版本全入口覆盖；`pytest -x` 全绿 |
| **WP7** 编排与扫描 | `execution`、`sweep`、facade、run-now | `services/credit/{execution,sweep}.py`、`services/liquidation_sweep.py`、`api/v1/admin_liquidation.py` | WP3、WP4、WP5、WP6 | 无（关键路径） | 每轮一动作；分页全覆盖；≤3 worker；PG 并发/崩溃恢复；扫描 P95 < 周期 |
| **WP8** 多 FX/展示/性能/切换 | 命名空间、≤3 pair、`reduce_only` 管理面、账户与公开展示、性能对照、runbook | `services/realtime.py`、`services/fx/market_data.py`、`services/tick_broadcaster.py`、`api/v1/{fx_stream,stream,admin_fx,fx,user,loan}.py`、`schemas/fx.py`、`thccb-frontend/src/**`、`loadtest/scenarios/credit_mixed.js`、`docs/**` | WP4、WP6、WP7（8a 仅需 WP4） | 无（尾包） | 同号隔离；≤3 pair；F9 语义；公开页无 run 详情；性能门槛；回退演练 |

### 5.1 波次（并行/串行）

```text
Wave 0:  WP1                                   （串行门：schema/配置/接口冻结）
Wave 1:  WP2 ∥ WP3a(gates,ownership) ∥ WP4a(split,publisher)
Wave 2:  WP3b(risk,runs) ∥ WP4b(FX 强平卖出)    （均需 WP2；WP3b 另需 WP3a）
Wave 3:  (WP5 → WP6) ∥ WP8a(namespace,≤3 pair) （WP5/WP6 必须串行；WP8a 在 WP4 完整交接后开始）
Wave 4:  WP7                                   （需 WP3b+WP4b+WP5+WP6）
Wave 5:  WP8b(展示,perf,runbook)               （需 WP7）
```

- **必须串行**：WP1 → 其余；`writer_ops.py` / `market_writer.py` WP5 → WP6；`fx/trading.py` WP4 → WP6；`services/fx/market_data.py` WP4 → WP8；`scripts/season_reset.py` WP1 → WP6；`api/v1/loan.py` WP6 → WP8；WP7 → WP8b。
- **可并行**：Wave 1 的三个包；Wave 2 的两个包；Wave 3 的 WP5→WP6 链与 WP8a（两条并行线，writer 文件链严格串行）。
- **禁止并行**：任何两个 WP 同时改 `alembic/versions/`（本计划只允许 1 个 revision，归 WP1）；任何两个 WP 同时改同一文件（按上表串行）。

---

## 6. 工作包详情

### WP1 — 基座：schema、配置、审计与冻结原语

**目标**：冻结后续 7 个 worker 共同依赖的数据模型、配置键与纯函数契约；迁移保持 additive 与可回退。
**步骤要点**
- PG 测试基座：`pytest.ini` 注册 `pg` marker + `addopts` 加 `-m "not pg"`；`tests/pg/` 独立库、每测试 `drop_all/create_all`、方言断言、`SELECT user FOR UPDATE` 冒烟；记录起库命令（不改 `docker-compose.yml`）。
- 性能与行为基线：同机 PG + `loadtest/` 既有场景 + 100 用户种子，≥5 轮预热，记录 buy P50/P99、quote P99、吞吐、错误率、事件循环延迟、连接池等待；记录 `tests/test_liquidation_sweep_perf.py -s` 中位耗时；存 `perf/baseline-report.md`。
- 模型与迁移：§3.3 全部字段；`alembic revision --autogenerate` 后人工 review；`batch_alter_table`（SQLite）；部分唯一索引给 `postgresql_where` + `sqlite_where`；`downgrade()` 删新表/列并注明"丢 run/action 运行记录，不影响资金"。
- `init_db.py`：按 F10 只补 3 行 metadata import，并复现"`import init_db` 表数 21 → 27"作为证据。
- 配置与校验：§3.4 键；`credit_leverage` 迁移种子 `loan_leverage_k + 1`（>19 拒绝并 CRITICAL）；`credit_maintenance_ratio` 迁移期沿用有效 `liquidation_hard_threshold`，否则不写；`unified_credit_enabled=true` 时缺 maintenance、`maintenance >= r_initial`、`leverage > 20` 均拒绝启用；关掉开关后管理端拒绝独立编辑 `loan_leverage_k`；全部写 `config_set` 审计（`source="credit_migration"`）。
- flags：进程级只读缓存 + 启动加载 + 测试覆写；不引入锁；只读实例显式关闭全部写调度器（本 WP 只加钩子，行为由 WP3 所有权件消费）。
- 审计与赛季重置：§3.5 类型；`CLEAR_ORDER` 中 `LiquidationAction` 先于 `LiquidationRun`；replay 自检覆盖。
- 纯函数：§3.2 的 `keys` / `thresholds` / `version`，以及 `loan_service.pending_debt`（`accrue_interest` 改为调用它，读写同源）。

**包内验收**
- `test_credit_keys` / `test_credit_thresholds`（含 100 组随机 `legacy_equivalence`：`credit_leverage=k+1` 时旧 `compute_max_borrow` == 新 `max_borrow`） / `test_credit_config` / `test_credit_migration` / `test_credit_audit` / 更新后的 `test_season_reset*`。
- 命令：`cd backend && python -m py_compile $(find app -name '*.py') && python -c "import app.main"`；`pytest -x`；`alembic upgrade head && alembic downgrade -1 && alembic upgrade head`（隔离 SQLite 与 PG 测试库）；`TEST_PG_DATABASE_URL=... python -m pytest -q -m pg tests/pg/`。

**合并门槛**：迁移双库幂等；开关 false 时**零运行时行为变化**；§3 冻结接口与键名落地，后续 WP 不得再改 schema/键名（若必须改，回到 WP1 走一次 additive revision）。

---

### WP2 — 统一清算估值与影子对账

**目标**：为风控提供"各产品真实组合 LCV + 展示 MTM + E"的唯一权威实现。
**步骤要点**
- `lmsr_quote`：滚动 q 聚合（同一 market 全部 outcome，`outcome_id` 升序），`gross = quantize_cost(cost_before − cost_after)`，`fee = quantize_cost(gross × fee_rate)`，`net = gross − fee`；`partial` 用 `min(amount, ceil_to_unit(amount × partial_pct, 1))`；`gross < 0` 的腿 `negative_proceeds` 跳过不删；`market_is_open` 为假 → `blocked_reason="market_not_open"` 且 `net=0`；纯函数不查库。
- `fx_quote`：`quote_sell` 聚合；10% 产出量化到 0 时回退全量，全量失败 → `quote_failed`；按 F9 双轴判定可执行性——`trading`（无论 `reduce_only`）与 `paused + reduce_only=true` 按可执行报价算 `L`；`paused + reduce_only=false`、`draft`、`closed`、无有效报价 → `blocked_reason` 且 `L=0`。
- `valuation`：批量读 ≤6 SELECT/100 用户（User → Position+Outcome+Market → FxWallet+Pair → 全量 outcomes）；`debt_effective` 用 `pending_debt`，不写库；`display_equity` 与 `liquidation_equity` 分别产出且 HALT/paused 处理不同。
- 影子 CLI：只读输出旧 LCV / 新组聚合 / 差值 / 占比 / MTM / E / 是否越维持线；在隔离 PG 数据集跑一次存 `shadow-before.md` 并解释差异来源。

**包内验收**：`test_credit_lmsr_quote`（滚动 q vs 独立相加有可判定差额、顺序无关、ceil、0 费率锚）、`test_credit_fx_quote`（AMM 同值、零产出回退、状态语义、`buy_fee_rate` 不参与）、`test_credit_valuation`（两个 equity 分开断言、暂停资产 L=0 但 MTM 保留、多 pair 不合并、利息不落库、SQL 计数上界）、`test_credit_shadow_report`（无写操作）。

**合并门槛**：`python -m pytest -q tests/test_credit_*.py tests/test_wealth_mtm.py tests/test_fx_valuation.py` 全绿；`grep -rn "services.credit" backend/app | grep -v "services/credit/"` 为空（尚未被运行路径引用）。

---

### WP3 — 风险件：门闩、风险检查、run 持久化、单写所有权

**目标**：提供准入与编排两侧共用的并发/幂等/决策件。
**步骤要点**
- `gates`：单批全序获取（重复键合并独占、按 `(product, group_id)` 升序、逆序释放）；等待不占 DB 连接；`held_keys()` 供指标。
- `risk`：`discover_dependencies`（锁外批量读用户版本 + 全组合快照）；`check_new_risk` 用 `PostTradeState` 模拟后 `E_after >= r_initial × D_after`，`D_after == 0` 直接 allow；`check_cash_spend` 覆盖消费/转出；`credit_frozen` / `credit_new_risk_frozen` 拒绝增险但允许还款/减仓/清算；版本冲突有界重试（`credit_risk_retry_limit`）后安全拒绝；版本化缓存键 `(user_id, economic_version, sorted((symbol, symbol_version)))`，LMSR 用 `WRITER.get_state` 的 `(hash(q_dec), status, closes_at)`，FX 用 `pair.pool_version`，有界 LRU + 命中计数。
- `runs`：`get_or_create_active_run`（每用户一个 active）、`record_action`（`(run_id, round_no)` 唯一）、`close_run`；run 行 `FOR UPDATE`；round 只在提交时前进。
- `ownership`：PostgreSQL `pg_try_advisory_lock` 专用非池化连接；连接丢失 → `writes_enabled=False` + CRITICAL，不假装 TTL 租约；`unified_credit_enabled=true` 时启动必须持锁。

**包内验收**：门闩 200 组随机重叠并发无死锁（重跑 20 次）；风险决策（无债快路径、越线拒绝、冻结可还款、版本重试后拒绝、缓存随版本失效）；runs 幂等与回滚；PG 所有权（第二实例失败、断连后可获取）。

**合并门槛**：上述测试全绿；仍未接入运行路径（除 flags/ownership 的启动钩子）。

---

### WP4 — FX 执行器：事务内拆分、后台发布、强平卖出、状态语义

**目标**：FX 成交可在调用方事务内复用，且响应路径不退化为 24h 聚合。
**步骤要点**
- `execute_trade_in_session(db, user_id, pair_id, side, amount, min_out, idempotency_key) -> tuple[FxTradePublic, FxTrade]`：保留现有校验/幂等/锁序/记账，**不 commit、不 rollback、不发布**；幂等命中返回既有交易；`execute_trade` 保留为包装（调用 → commit 或幂等 rollback → 入队发布）。
- `publisher`：有界队列 + 单 worker，提交后入队；队列满丢弃并计数，不阻塞交易；`drain()` 供测试；lifespan 启停顺序在 FX scheduler 之后、broker 之前。
- `execute_liquidation_sell_in_session`：锁序 `pair→user→wallet→treasury`；`treasury.foreign_balance += fee_foreign`；reserves 取 quote post 值并 `pool_version += 1`；wallet 扣 `foreign_in`、按比例缩 `cost_basis`、清零置 0；`user.cash += gold_out`；写 `FxTrade(source="liquidation", idempotency_key=f"liq:{run_id}:{round_no}")` + `record_fx_trade`；不 commit、不发布。
- 状态语义（F9 双轴）：`trading + reduce_only=false` 正常；`trading + reduce_only=true` 允许卖/清算、拒绝买与系统干预；`paused + reduce_only=true` 允许清算与用户卖出、拒绝开仓与系统干预；`paused + reduce_only=false` 保持全停（不成交、`L=0`）；`draft`/`closed` 以及无有效报价 → `L=0` 且不成交。**`paused` 不得因新增列而自动变成可卖**。

**包内验收**：玩家行为对等（幂等重放、参数不一致 409、`min_out` 409、gate/pair/bot/TOS/现金/钱包错误码全等）；`execute_trade_in_session` 返回时未提交；发布不在响应路径且队列满不影响交易；FX 强平金圆券守恒涵盖池、treasury、用户现金与还债回收账户；外币守恒涵盖池、treasury、用户钱包，不能套用只适用于系统自营单的 `G+T_G`/`F+T_F` 检查；同 run/round 重放不二次卖；`do_orm_execute` 锁序断言；F9 四象限矩阵 + "存量 `paused` 迁移后仍全停（`L=0`、买入与清算都被阻塞）" + "管理端显式开 `reduce_only` 后才可减仓"。

**合并门槛**：`TMPDIR=/dev/shm python -m pytest -q --noconftest tests/test_fx_*.py` 全绿；`tests/test_fx_valuation.py::test_summary_exposes_fx_and_keeps_margin_on_lcv` 仍绿（风控切换在 WP6 且由开关控制）。

---

### WP5 — LMSR 组强平执行与费率

**目标**：把"逐 position / 逐 market"改成一次事务内的组级清算，并按 F5 收费。
**步骤要点**
- `LiquidateGroupCmd(market_id, user_id, run_id, round_no, mode, partial_pct, fee_rate, daily_rate, trigger_source)` + `op_liquidate_group`：`lock_user` → 该 market 全部持仓 `FOR UPDATE`（`Position.id ASC`）→ `quote_lmsr_group` 在 `state.q_dec` 滚动副本上按 `outcome_id` 顺序卖 → `cash += net`、outcome 绝对 SET、`LIQUIDATE`（`fee` 为腿费、`cost = -net`、`gross` 为腿 gross）→ 同事务计息 + `decrease_debt_locked` 还债 → `record_action` → 返回 `{sold_count, gross, fee, net, repaid, debt_after, cash_after}`。
- 不检查滑点、不用用户 `min_out`、不写 candle、沿用 `feed_prices`；`state.q_dec` 与 DB 镜像保持 6dp 不动点。
- legacy 路径同步费率（`liquidation_service.py:269`、`writer_ops.py` 旧 op），并在旧入口加 `flags.is_unified_enabled()` 断言（避免两个执行者同时卖仓）；旧 `op_liquidate_market` 保留到切换完成。
- 更新 D1/F5 相关断言：`tests/test_liquidation_e2e.py:221` 改为 `fee == gross × 费率`；新增非零费率用例；`grep -rn "强平不收手续费" backend/app backend/tests` 为空。

**包内验收**：一次事务卖完整组；负收益腿跳过不删；零费率下与旧 `op_liquidate_market` 逐字段一致；非零费率 `cash` 增量 = `gross − fee`；同事务还债；同 `(run_id, round_no)` 幂等；legacy 开关行为。

**合并门槛**：`python -m pytest -q tests/test_writer_liquidation.py tests/test_lmsr_liquidation_fee.py tests/test_liquidation_*.py tests/test_writer_{buy,sell}.py`；开关 false 时既有强平测试除 F5 断言外逐字段不变。

---

### WP6 — 准入接入：经济写路径全覆盖

**目标**：所有会改变现金/债务/持仓/钱包的入口都走同一套门闩 + 版本复检 + 交易后风险检查，无旁路。
**步骤要点**
- 市场/FX：`market_writer` consumer 在 `op()` 的 DB 事务**之外**获取门闩；`op_buy` / `op_sell` / `op_resolve` 加 `bump_economic_version` 与风险检查；FX `execute_trade_in_session` 内替换"有债禁买"为初始门槛检查；`api/v1/market.py` legacy 非 writer 路径（639/831/1059）同规则。
- **门闩与提交顺序（不得颠倒）**：① 锁外 `discover_dependencies` → 申请门闩（改价/改状态的品种独占，其余抵押品共享，单批全序）；② 取得门闩后才开 DB 事务（产品行锁 → User → 持仓/钱包 → treasury），版本复检，模拟 post-state，决策，commit；③ **commit 后仍持门闩**做无 IO 镜像更新（LMSR `q_dec/prices/status`；FX 只需确认事务内 bump 的 `pool_version`）；④ 镜像更新失败 → 该品种 `unavailable=True` → 释放**全部**门闩 → 仅对该品种 `reload_state()`；禁止先放门闩再更新镜像，禁止因单市场 candle flush 阻塞全站；⑤ 无债快路径只取目标品种独占门闩 + user 锁，锁后发现债务则回滚重走有债路径，重试超限安全拒绝。
- 借款/消费/管理/运维：`api/v1/loan.py`（`/borrow` 用新 `max_borrow`、`/quota` 返回两条门槛与 E）；`admin_user_service.py`（`adjust_cash`、`force_loan`、`forgive_debt`、`batch_adjust_cash`、`amnesty` 检查 + 审计）；`redemption.py`、`danmuku.py` 消费后检查（保留"有债禁止兑换"）；`pve/service.py`（bot cash 初始化/重置/发放）；`fx/scheduler.py`（`fund_pair`/`withdraw_pair` **bump `pool_version`**）；`season_reset.py`（重置后 bump 版本、新赛季不保留 run）；`loan_service.py` 的 `increase_debt` / `decrease_debt_locked` 统一 bump。
- 版本自增覆盖全部入口；交易路径不写 dirty/outbox、不展开债务用户、不触发强平。

**包内验收**：无债快路径 SQL 计数不高于 WP1 基线；`test_debt_user_buy_rechecks_post_trade_equity`；`test_two_products_cannot_reuse_collateral`；`test_self_price_impact_cannot_inflate_borrowing`；`test_cash_spend_below_initial_margin_rejected`；`test_gate_released_only_after_mirror_update`（spy 顺序）；`test_mirror_failure_isolates_symbol_and_reloads_only_it`；`test_fund_withdraw_bump_pool_version`；`test_economic_version_increments_on_every_write_path`（参数化覆盖 §2 表格全部入口）。

**合并门槛**：上述 + `pytest -q` 全绿 + `tests/test_market_locks.py`、`test_market_deadlock_fix.py` 保持绿。**若性能验收未过，先修瓶颈，禁止退回全局锁后宣称性能不变**（spec §6.2）。

---

### WP7 — 统一编排与定时扫描

**目标**：把固定强平算法与定时扫描落地，含 run-now 与崩溃恢复。
**步骤要点**
- `execution.liquidate_account_round`（F3 逐条）：计息 + 复检（新 run 仅在 `E < R_maintenance×D` 创建；已有 active run 以 `E >= R_initial×D` 或 `D==0` 结束）；同一事务现金优先还债并记 repay 动作，恢复即停；未恢复按 `(-L_group, product, group_id)` 选一个可执行组；`E>0` 卖 10%（F12 量化），`E<=0` 选中组全卖；卖出/费用/持仓/回款还债/run+action 同一事务；提交后重查目标，未恢复保存 run 等下一轮（不即时重排、不另起循环）；无正回收/暂停/数据异常记 `liquidation_blocked` + `last_blocked_reason`；每账户最多一个在途动作；交易/行情/利息/新闻/暂停恢复/注撤资都不启动或续接强平。
- LMSR 组分发 `WRITER.submit(LiquidateGroupCmd)`；FX 组在编排事务内调 `execute_liquidation_sell_in_session`；门闩经 `SymbolGateSet`。
- `sweep`：定时与 run-now 共用 `asyncio.Lock`（重叠返回 `sweep_in_progress`）；错过触发合并；阶段 1 无锁、按 `user_id` 分页 20 遍历全部 `debt>0`；阶段 2 候选（触发 ∪ 有 active run）交 `Semaphore(3)`；指标 `scanned_users / candidates / triggered / recovered / blocked / actions / errors / skipped_rounds / sweep_duration_ms / per_user_lock_ms`；超周期告警 + 跳过重叠轮次；删除 `_recently_attempted` 与 `_STUCK_COOLDOWN_SEC`（`liquidation_sweep.py:32`）。
- `liquidation_sweep.py` facade 保持签名；`admin_liquidation.py` 返回新指标；`/recent-liquidations` 的 `product` 字段归 WP8。

**包内验收**：`test_credit_execution`（现金优先、选组与稳定排序、10% ceil、E≤0 全卖、每轮一组、暂停组跳过后卖其他组、资不抵债冻结且保留债务、同事务回滚无部分状态、行情变动不启动清算）；`test_credit_sweep`（分页全覆盖而非只查前 20、≤3 worker、run-now 重叠跳过、漏过触发合并、已有 run 续接、无债不估值、指标齐全）；PG 并发/崩溃恢复（同用户竞争、独立市场并行计时、版本重试、提交后崩溃重启不重复卖/扣/还、writer 镜像自愈）。

**合并门槛**：focused 全绿；`TEST_PG_DATABASE_URL=... python -m pytest -q -m pg tests/pg/` 全绿；100 用户扫描 P95 < 配置周期；停服顺序（sweep 先于 writer）保持。

---

### WP8 — 多 FX、命名空间、展示、性能对照与切换

**目标**：解除单 pair 限制并隔离命名空间；完成账户/公开展示；产出性能对照与切换/回退 runbook。
**步骤要点**
- 8a 命名空间与多 pair：`realtime.py` topic 由 `int` 改 `str`（`subscribe/publish/current_seq/subscriber_count` 同步），FX 用 `symbol_namespace("fx", pair_id)`、LMSR 用 `symbol_namespace("lmsr", market_id)`，IP 限流键同步；`admin_fx.py` 删除 `:168`/`:195` 唯一限制并强制 F8（`trading` 数量 ≤3，超出 409；上限只在管理端，schema 与估值接口不写死 3）；`schemas/fx.py` 暴露 `reduce_only`（默认 false，公开白名单不变），管理端可显式把 `paused` pair 切到只减仓并留审计。
- 8b 展示：`api/v1/user.py` summary 增加 `fx_wallets: [{pair_id, currency_code, foreign_amount, mtm_gold}]`、`display_equity`、`liquidation_equity`、`debt_with_interest`、`credit_leverage`、`r_initial`、`r_maintenance`、`equity_to_debt`（`D==0` 为 `null`）、`risk_status`、`credit_frozen`、`unified_credit_enabled`；`debt==0` 走快路径（两 equity 等值）；`api/v1/fx.py` 返回全部非 draft；`api/v1/loan.py` 的 `/liquidation-policy` 返回新门槛/费率/间隔/开关（legacy 字段标 `legacy: true`）、`/recent-liquidations` 只加可空 `product`（F11，不含 run 详情）；前端按 MTM 显示主净值、另显示清算净值与两条门槛、多 pair 钱包、跨产品强平提示；写 `docs/unified-credit-risk-2026-10.md` 并补 `docs/fx.md` 章节（费率按 F5，公示文案与时长由运营在发布时确定）。
- 8c 性能与切换：同机 PG、同数据集/轨迹/连接池/负载、≥5 轮预热，跑 `unified_credit_enabled=true`，与 WP1 基线对照（无债 P95/P99 增幅 ≤5%、吞吐降幅 ≤5%、错误率无显著上升；既有 single-writer 验收目标仍保留；若基线未达标单独报告，不能自动降级目标；新路径独立报告估值计算、SQL 次数、门闩等待/持有、DB 事务、端到端时延；覆盖同账户跨品种与不同账户独立品种；100 用户 + 多 FX + LMSR + tick + PvE + 批量强平并发；完整扫描 P95 < 周期）；未达标先修瓶颈。切换 runbook：备份 → `credit_new_risk_frozen=true` → `liquidation_enabled=false` → 部署 + `alembic upgrade head` → 只读取证线上 `sell_fee_rate` → 显式设置门槛（20x 前 `credit_maintenance_ratio=0.04`）并审计 → `unified_credit_enabled=true` + run-now 灰度 → `liquidation_enabled=true` → 解冻 → 确认公示。回退演练：先冻结新增信用，保留统一估值、还款和安全减仓；已有 FX 抵押债务时不得直接恢复只处理 LMSR 的旧强平。降级迁移仅在可丢弃的隔离库测试；生产不删除 run/action 记录。若必须恢复旧代码，需停服并恢复版本匹配的完整数据库备份，明确备份后交易处置，不能仅切开关或 downgrade。

**包内验收**：同号 `market_id == pair_id` 订阅互不串流；`current_seq`/限流键隔离；3 个 pair 各自 reserves/treasury/事件独立；第 4 个 `trading` 返回 409 且迁移不因上限写死品种；F9 四象限（含存量 `paused` 仍全停、显式 `reduce_only` 后才减仓）；账户字段与 `debt==0` 快路径；公开响应无 run 详情；前端 `npm run type-check && npm run test:unit`；性能报告与 runbook 演练记录。

**合并门槛**：8a 可先合（隔离 + ≤3 pair 测试绿，`fx_enabled` 仍 false）；8b/8c 在 WP7 之后合并，性能相对门槛达标、回退演练通过。

---

## 7. 全局验收矩阵（每个 WP 合并前跑对应行）

| 层 | 命令 | 期望 |
| --- | --- | --- |
| 语法/导入 | `cd backend && python -m py_compile $(find app -name '*.py') && python -c "import app.main"` | exit 0 |
| 后端全量（SQLite） | `cd backend && pytest -x` | 全绿；既有失败事先列清单并注明非本改动引入 |
| credit 聚焦 | `python -m pytest -q tests/test_credit_*.py tests/test_lmsr_liquidation_fee.py tests/test_fx_liquidation.py` | 全绿 |
| 既有回归 | `python -m pytest -q tests/test_liquidation_*.py tests/test_loan_*.py tests/test_writer_*.py` | 全绿（F5 费率断言除外） |
| FX 聚焦 | `TMPDIR=/dev/shm python -m pytest -q --noconftest tests/test_fx_*.py` | 全绿（继承 `docs/fx.md` §11.2 环境限制） |
| PG 并发/恢复 | `TEST_PG_DATABASE_URL=... python -m pytest -q -m pg tests/pg/` | 全绿，30s 超时无 flaky |
| 迁移 | `cd backend && alembic upgrade head && alembic downgrade -1 && alembic upgrade head`（隔离库） | 幂等，回退干净 |
| 赛季重置 | `docker compose run --rm --no-deps -T backend python scripts/season_reset.py --dry-run --expected-ruleset 2026-09-27`（隔离库） | 预览含新表；零写入 |
| 性能 | `python -m pytest -q tests/test_liquidation_sweep_perf.py -s` + k6 `credit_mixed.js` | 达标报告（WP8c） |
| 前端 | `cd thccb-frontend && npm run type-check && npm run test:unit` | 全绿；UI 未实测要明说 |

---

## 8. 明确非目标

1. **事件驱动强平**：不建 dirty 索引、风险 outbox、版本条件消费、实时重估/强平续接、事件延迟指标。
2. **多来源负债**：不新增 `CreditLiability`、分来源还款队列、债务投影、多利率、按资产分拆债务。
3. **压力保证金 / 第三净值**：只有 `display_equity`(MTM) 与 `liquidation_equity`(E)。
4. **全局经济锁**：不新增覆盖全站经济写的互斥锁；保留每 market writer 与每 pair 串行；不同市场/ pair 必须能并发提交。
5. **产品专属风险分层**：不做抵押品折扣、相关性、每品种 IM/MM、债务底线、保险基金、自动核销、treasury 赔付。
6. **清算搜索**：不做最优卖出量搜索、候选动作收益排名、紧急保证金率、强平罚金。
7. **借贷产品扩展**：不做外币借贷、裸空、永续、多空双向、限价单、用户做市、真实汇率源、quant/PvE 接入 FX。
8. **数值内核重写**：复用 `lmsr.py` 与 `fx/amm.py`。
9. **运维动作**：不部署、不推送、不打开生产 `fx_enabled`、不改受保护文件（F10 授权范围除外）。
10. **测试方式**：不做自动浏览器点击/填表；前端最多截图；不跑写生产库的测试。

---

## 9. 风险

| # | 风险 | 缓解 |
| --- | --- | --- |
| R1 | 门闩死锁/饥饿 | 单批全序、禁止持闩等待、PG 并发验证（WP3/WP7）、门闩指标；失败先修瓶颈，**不得**退回全局锁 |
| R2 | E 计算拖慢热路径 | 无债快路径、版本化缓存、SQL 计数回归（WP6）、WP8c 门槛 |
| R3 | 版本重试风暴 | 有界重试 + 安全拒绝 + 计数告警；不用旧快照放行 |
| R4 | 批次跨事务崩溃窗口 | 动作与卖出/还债同事务 + `(run_id, round_no)` 唯一 + 下轮续接 + WP7 恢复测试 |
| R5 | 费率语义变化未公示 | F5 线上取证 + 公示（发布参数）+ 0/非 0 双测试 |
| R6 | 回退失败 | schema 全 additive + 旧执行者保留至切换完成 + 备份/恢复演练 |
| R7 | FX 拆分破坏幂等/SSE | 行为对等测试 + 公开白名单不变 |
| R8 | 命名空间遗漏 | 同号隔离测试 + 限流键命名空间 + 首期 ≤3 pair |
| R9 | 多副本写破坏单写前提 | WP3 所有权锁 + 启动拒绝 + 只读实例关全部写调度器 |
| R10 | 扫描超周期 | 分页批量读、≤3 worker、指标告警、跳过重叠轮次 |
| R11 | SQLite/PG 迁移不一致 | `batch_alter_table` + `postgresql_where`/`sqlite_where` + 双库迁移测试 |
| R12 | 赛季重置自检被新审计类型打破 | 权威 `user_after` + `fold` 容忍未知类型 + 重置套件覆盖新表 |
| R13 | 空库缺表（已核实的既有缺陷） | WP1 按 F10 修复并存证；未修好前禁止用空库路径初始化生产 |

---

## 10. 上线前门槛

1. 全部 WP 合并；`pytest -x` 全绿；既有失败已列清单并证明无关。
2. PostgreSQL 并发/崩溃恢复套件全绿（独立市场并行、版本重试、重复扫描、崩溃续接）。
3. WP8c 性能对照达标（无债 P95/P99 增幅 ≤5%、吞吐降幅 ≤5%、错误率无显著上升；扫描 P95 < 周期；跨账户独立品种未被全局串行）。
4. 影子对账按发布参数完成，所有差异可归因，无未解释偏差。
5. `credit_leverage` / `credit_maintenance_ratio` 已显式设定并审计（20x 前 `=0.04`）；`R_maintenance < R_initial` 由管理端与启用校验双重强制。
6. F5 线上 `sell_fee_rate` 已只读取证；非零时公示已按运营确定的文案与时长发布。
7. 切换 runbook 演练通过；生产回退以版本匹配的完整数据库备份为准，迁移降级只在隔离库演练；备份校验和已记录。
8. 单写实例所有权生效；只读副本显式关闭全部写调度器。
9. 赛季重置 dry-run 覆盖新表与新审计类型，自检通过。
10. FX `trading` pair 数 ≤3（更多需单独批准）；生产 `fx_enabled` 仍按独立发布决策。
11. `init_db.py` 的 metadata 注册已修复并复现表数 21 → 27；否则禁止空库路径起服。

---

## 11. Subagent 分工建议

| 角色 | 负责 | 说明 |
| --- | --- | --- |
| Worker A | WP1 | 串行门；A 未合并前其他 worker 只允许做只读调研与本地草稿 |
| Worker B | WP2 | Wave 1 起；只写 `credit/{lmsr_quote,fx_quote,valuation}.py` 与影子脚本 |
| Worker C | WP3（先 gates+ownership，后 risk+runs） | Wave 1 做 gates/ownership；WP2 合并后补 risk/runs |
| Worker D | WP4 | Wave 1 做拆分/发布；WP2 合并后补 FX 强平卖出 |
| Worker E | WP5 → WP6 | 同一 worker 串行两个包，避免 `writer_ops.py`/`market_writer.py` 交接成本 |
| Worker F | WP7 | 关键路径；需 E 的 WP6 合并后才开工 |
| Worker G | WP8a → WP8b/c | 命名空间与多 pair 可在 Wave 3 开工；展示/性能/runbook 在 WP7 后 |
| 主 agent（集成者） | 合并顺序、Review Focus、性能门槛判定、上线前门槛与切换 runbook 复核 | 不并行写业务文件；负责每包合并前的 `pytest -x` 与证据抽查 |

- 同一时刻最多 3 个 worker 并行（Wave 1 为 3 个，Wave 2/3 最多 2 个，另留主 agent 集成）；**同一文件同一时刻只有一个 worker**（§5.1 串行表）。
- 每个 worker 的交付必须包含 `wp-N-report.md`：改动文件、命令、原始输出、未决技术问题；主 agent 抽查后再合并。
- 若某 worker 提前空闲，可预读下一个 Wave 的接口契约并写测试骨架，但不得改不属于自己的文件。

---

## 12. 实现期间可能浮现的技术问题（只需在发生时记录并解决，不是当前未决决策）

1. **门闩依赖集合过大**：若 WP8c 显示共享抵押品竞争使无债/独立市场延迟超门槛，可优化批量读取、缓存和持锁时间；不能按价值阈值忽略仍计入抵押的品种锁，也**不得**退回全局锁；按 spec §6.2 先修瓶颈。
2. **估值缓存命中率不足**：PvE/tick 高频写同一批市场时缓存可能频繁失效；若 WP8c 要求更多优化，再评估按 (market, q) 复用组报价而不是放大 TTL。
3. **`audit_replay` 对新事件类型的容忍边界**：若赛季重置自检在 `liquidation_action` 与后续 `trade_liquidate` 交错时出现快照不一致，需在 WP1 的 replay 规则里显式声明这些类型的快照语义。
4. **FX `paused + reduce_only` 与未来三态需求**：F9 用 `status + bool` 表达；若运营后续要区分"暂停开仓/暂停全部/停牌"三态，再评估引入独立状态值（本期不做）。
5. **3 个 pair 下的 SSE/限流容量**：`MAX_SUBSCRIBERS_PER_MARKET` 与 IP 限流按 pair 独立计数，实际并发订阅数需在 WP8 观察；若触顶，调整的是每 pair 上限参数而非命名空间方案。
6. **`fund_pair`/`withdraw_pair` bump 版本后的缓存抖动**：注撤资会立即失效该 pair 的缓存；若运维批量注资造成抖动，评估合并注资或延后失效，但不改变"版本决定新鲜度"的原则。
