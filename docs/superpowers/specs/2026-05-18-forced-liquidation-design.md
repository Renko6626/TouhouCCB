# 强制平仓机制设计稿

**日期**：2026-05-18
**作用域**：backend（核心服务 + scheduler + admin + 公共展示接口）+ 前端（margin 警告 banner + 翻车现场墙）

## 1. 动机

现有 loan 机制允许 `loan_leverage_k=1.0` 杠杆借款，但**没有任何被动止损**：用户借完钱买仓位、市场反向跑、净值掉到负数也没人管，借款一直挂着累利息。需要一个类似现实期货 broker 的 margin call + 强平机制：

- **保护 LP（系统）**：用户净值跌穿后再不止损，债务最终落到 LP 头上
- **保护用户**：强行止损 < 让用户继续加仓越亏越深
- **教育意义**：站点是研究学习模拟性质，把强平记录公示首页给所有人看，警示别加高杠杆

## 2. 触发判定

记号：`NW = cash - debt + holdings_value`，`margin_ratio = NW / debt`（debt=0 时无意义）。

| 状态 | 条件 | 行为 |
|---|---|---|
| healthy | `margin_ratio ≥ 0.5` 或 `debt = 0` | 正常 |
| **soft（warning）** | `0.2 ≤ margin_ratio < 0.5` | UI banner 警告 + structlog `margin_call` 事件；**不动仓** |
| **hard（liquidate）** | `margin_ratio < 0.2` | 立即全平 + 最大还债 |

阈值 0.2 / 0.5 都从 `site_config` 读取，运行时可调。

`holdings_value` 复用 `app/services/wealth.py:compute_users_holdings_value`（含卖出滑点 + fee 的 LMSR 清算价），跟 `/user/summary` 同口径——单一真值源避免分裂。

## 3. 平仓算法

```python
async def liquidate_user(
    session, user, *, daily_rate, trigger_source,
) -> LiquidationEvent:
    """前提：调用者已 SELECT FOR UPDATE user 行，已在事务上下文中。"""
    # 0. pre-state snapshot
    pre_cash, pre_debt = user.cash, user.debt
    pre_hv = await compute_holdings_value(session, [user.id])[user.id]
    pre_nw = pre_cash - pre_debt + pre_hv
    pre_margin = pre_nw / pre_debt if pre_debt > 0 else None

    # 1. 拉所有 amount>0 position（含 outcome.market），按 id ASC 顺序
    positions = await session.execute(
        select(Position)
        .options(selectinload(Position.outcome).selectinload(Outcome.market))
        .where(Position.user_id == user.id, Position.amount > 0)
        .order_by(Position.id.asc())
        .with_for_update()
    ).scalars().all()

    total_proceeds = Decimal(0)
    sold_count = 0
    for pos in positions:
        if pos.outcome.market.status != MarketStatus.TRADING:
            continue   # 已结算的让 resolve flow 处理 payout

        # 复用 market.py SELL 内部数学（不调 HTTP / 不走 risk guard / 不算 slippage）
        all_outcomes = await _lock_outcomes_for_market(session, pos.outcome.market_id)
        idx = next(i for i, o in enumerate(all_outcomes) if o.id == pos.outcome_id)

        old_q = [float(o.total_shares) for o in all_outcomes]
        new_q = list(old_q); new_q[idx] -= float(pos.amount)
        old_cost, _ = calculate_lmsr_with_prices(old_q, float(market.liquidity_b))
        new_cost, new_prices = calculate_lmsr_with_prices(new_q, float(market.liquidity_b))

        proceeds = quantize_cost(old_cost - new_cost)
        if proceeds < 0:   # 防御：极端 q 状态
            logger.error("liquidation_negative_proceeds",
                         user_id=user.id, position_id=pos.id, proceeds=str(proceeds))
            continue

        user.cash += proceeds
        all_outcomes[idx].total_shares -= pos.amount
        await session.delete(pos)

        session.add(Transaction(
            user_id=user.id, outcome_id=pos.outcome_id,
            type=TransactionType.LIQUIDATE,
            shares=pos.amount, gross=proceeds, fee=ZERO,
            price=quantize_price(proceeds / pos.amount) if pos.amount > 0 else ZERO,
            pre_market_price=quantize_price(old_prices[idx]),
            post_market_price=quantize_price(new_prices[idx]),
            cost=-proceeds,
            market_prices_post=list(new_prices),
        ))

        # SSE 推送，让其他 client 实时看到价格变化
        await BROKER.publish(market.id, "trade", {
            "trade": {
                "id": tx.id, "type": "sell",
                "outcome_id": pos.outcome_id,
                "username": user.username,
                "shares": float(pos.amount), "price": float(price),
                "gross": float(proceeds), "fee": 0.0,
                "post_market_price": float(post_mp),
                "market_prices_post": [float(p) for p in new_prices],
                "timestamp": now_iso(),
            }
        })

        total_proceeds += proceeds
        sold_count += 1

    # 2. 最大化还债
    rate = daily_rate
    repay_amount = min(user.cash, user.debt)
    repaid = Decimal(0)
    if repay_amount > 0:
        _, repaid = await loan_service.decrease_debt(
            session, user.id, repay_amount,
            consume_cash=True, daily_rate=rate,
        )

    user.last_liquidated_at = datetime.now(timezone.utc)

    # 3. 写 event 行（公示用）
    event = LiquidationEvent(
        user_id=user.id,
        triggered_at=datetime.now(timezone.utc),
        pre_cash=pre_cash, pre_debt=pre_debt,
        pre_holdings_value=pre_hv, pre_net_worth=pre_nw,
        pre_margin_ratio=pre_margin,
        sold_positions_count=sold_count,
        total_proceeds=total_proceeds,
        repaid_amount=repaid,
        remaining_debt=user.debt,
        post_cash=user.cash,
        trigger_source=trigger_source,
    )
    session.add(event)
    return event
```

**锁顺序**：user → positions → 每个 market 的 outcomes（跟 market.py SELL 一致，杜绝 deadlock）。

**原子性**：整个 user 一个事务，全 commit 或全 rollback。其中某个 position 卖出抛异常 → 整个 user 这次 rollback，下次 sweep 重试。

**SSE publish 跟 DB 事务的关系**：参照 `market.py:578` 现有 buy/sell 路径，publish 在 `managed_transaction` 上下文管理器**退出后**（也即 commit 完成后）调用。这意味着：
- 如果 publish 自身失败（broker 内部错误），DB 已经一致，不会回滚
- 如果 DB commit 失败，publish 不会触发
- 中间崩溃极小窗口（commit 成功但 publish 前进程死）会丢一次推送——客户端下次重连 snapshot 时拉到正确状态自愈

**实现细节**：算法 §3 的伪代码把 publish 写在 for 循环内是为了表达"每次卖出对应一个推送"，实际编码时把 publish 调用收集到列表，事务 commit 后批量执行。

**复用 market.py 内部锁辅助**（`_lock_user` / `_lock_outcomes_for_market`）：当前是 module-private（`_` 前缀）。实施时把它们提取到 `services/market_locks.py`（小重构），让 `liquidation_service.py` 共用——避免重复实现且确保锁顺序一致。

## 4. 触发途径

### 4.1 定时 sweep（主路径）

`backend/app/services/liquidation_sweep.py` 模仿 `loan_sweep.py`：

```python
async def run_liquidation_sweep_once() -> dict:
    """返回 {triggered_count, soft_warning_count, sweep_duration_ms, errors}"""
    if not await site_config.get_bool("liquidation_enabled"):
        return {"skipped": "disabled"}

    hard_thr = await site_config.get_decimal("liquidation_hard_threshold")
    soft_thr = await site_config.get_decimal("liquidation_soft_threshold")
    rate = await site_config.get_decimal("loan_daily_rate")

    user_ids = (await session.execute(select(User.id).where(User.debt > 0))).scalars().all()

    triggered, warned, errors = 0, 0, 0
    for uid in user_ids:
        # 防爆 loop：30 min 内已扫过的"资不抵债 stuck"用户跳过
        if _recently_attempted.get(uid, 0) + 1800 > monotonic():
            continue
        try:
            async with new_session() as db:
                async with db.begin():
                    user = await _lock_user(db, uid)
                    if user.debt <= 0:
                        continue
                    hv = (await compute_users_holdings_value(db, [uid])).get(uid, Decimal(0))
                    nw = user.cash - user.debt + hv
                    margin = nw / user.debt

                    if margin < hard_thr:
                        ev = await liquidation_service.liquidate_user(
                            db, user, daily_rate=rate,
                            trigger_source="scheduler",
                        )
                        triggered += 1
                        if ev.sold_positions_count == 0 and ev.repaid_amount == 0:
                            _recently_attempted[uid] = monotonic()
                    elif margin < soft_thr:
                        warned += 1
                        logger.warning("margin_call_soft_threshold",
                                       user_id=uid, margin_ratio=float(margin))
        except Exception:
            errors += 1
            logger.exception("liquidation_sweep_user_error", user_id=uid)

    return {"triggered_count": triggered, "soft_warning_count": warned,
            "errors": errors, ...}
```

**Lifespan 集成**：`app/main.py` 在 `start_scheduler()` 旁加 `liquidation_sweep.start_scheduler()` + 对应 stop。`conftest.py` 的 `_disable_scheduler` mock 也加 patch。

### 4.2 Admin 手动触发

```python
@admin_router.post("/admin/liquidation/run-now")
async def admin_run_liquidation_sweep(admin: User = Depends(current_superuser)):
    """立即跑一次完整 sweep，跟 scheduler 同逻辑，不绕过阈值。"""
    result = await liquidation_sweep.run_liquidation_sweep_once()
    logger.info("ADMIN_RUN_LIQUIDATION_SWEEP admin_id=%s result=%s",
                admin.id, result)
    return result
```

**不暴露**单用户强平 endpoint——admin 没有 override 阈值的能力。

## 5. 数据模型

### 5.1 新 enum 值

```python
class TransactionType(str, Enum):
    BUY = "buy"
    SELL = "sell"
    SETTLE = "settle"
    SETTLE_LOSE = "settle_lose"
    LIQUIDATE = "liquidate"   # 新增
```

### 5.2 User 加列

```python
class User(SQLModel, table=True):
    # ...existing fields...
    last_liquidated_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True),
    )
```

### 5.3 新表 `LiquidationEvent`

```python
class LiquidationEvent(SQLModel, table=True):
    __tablename__ = "liquidation_events"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    triggered_at: datetime = Field(sa_column=Column(DateTime(timezone=True)))

    pre_cash: Decimal = Field(sa_type=Numeric(16, 6))
    pre_debt: Decimal = Field(sa_type=Numeric(16, 6))
    pre_holdings_value: Decimal = Field(sa_type=Numeric(16, 6))
    pre_net_worth: Decimal = Field(sa_type=Numeric(16, 6))
    pre_margin_ratio: Decimal | None = Field(sa_type=Numeric(10, 6), nullable=True)

    sold_positions_count: int
    total_proceeds: Decimal = Field(sa_type=Numeric(16, 6))
    repaid_amount: Decimal = Field(sa_type=Numeric(16, 6))
    remaining_debt: Decimal = Field(sa_type=Numeric(16, 6))
    post_cash: Decimal = Field(sa_type=Numeric(16, 6))

    trigger_source: str  # "scheduler" | "admin_manual"
```

### 5.4 Alembic migration

```bash
cd backend && alembic revision --autogenerate -m "add_liquidation_event_table_and_user_last_liquidated"
```

应该自动生成：
- `ALTER TABLE users ADD COLUMN last_liquidated_at TIMESTAMPTZ NULL`
- `CREATE TABLE liquidation_events (...) ` + index on `user_id`、`triggered_at`

### 5.5 site_config 新增 4 key

`backend/app/services/loan_migrate.py` 的 `DEFAULTS` 追加：

```python
("liquidation_enabled", "false", "bool"),   # ← 默认关，灰度开启
("liquidation_sweep_interval_sec", "600", "int"),     # 10 min
("liquidation_hard_threshold", "0.2", "decimal"),
("liquidation_soft_threshold", "0.5", "decimal"),
```

`backend/app/api/v1/site_config.py` 的 `ALLOWED_KEYS` 同步加。

## 6. API

### 6.1 `/user/summary` 扩字段

```python
return {
    # ...existing...
    "margin_ratio": net_worth / debt if debt > 0 else None,
    "margin_status": "healthy" | "warning" | "danger",
    "last_liquidated_at": user.last_liquidated_at,
}
```

`margin_status` 由后端按 site_config 阈值分类，前端不重算（避免缓存不一致）。

### 6.2 `/admin/liquidation/run-now` POST

见 §4.2。

### 6.3 `/recent-liquidations` GET（公开，匿名可访问）

```python
@router.get("/recent-liquidations")
async def recent_liquidations(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_session),
):
    stmt = (
        select(LiquidationEvent, User.username)
        .join(User, User.id == LiquidationEvent.user_id)
        .order_by(LiquidationEvent.triggered_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [{
        "id": ev.id,
        "username": username,
        "triggered_at": ev.triggered_at.isoformat(),
        "pre_debt": float(ev.pre_debt),
        "pre_net_worth": float(ev.pre_net_worth),
        "pre_margin_ratio": float(ev.pre_margin_ratio) if ev.pre_margin_ratio else None,
        "total_proceeds": float(ev.total_proceeds),
        "repaid_amount": float(ev.repaid_amount),
        "remaining_debt": float(ev.remaining_debt),
        "fully_liquidated": ev.remaining_debt == 0,
    } for ev, username in rows]
```

放在 `loan.py` 路由组下；不加 auth（同 `/market/recent-trades` 思路）。

## 7. 前端

### 7.1 Margin warning banner

`Home.vue` + `pages/market/TradingView.vue` 头部加 `<MarginCallBanner>` 组件：
- 数据源：`/user/summary.margin_status`
- 显示规则：
  - `healthy`：不显示
  - `warning`（margin 0.2~0.5）：黄色 banner "中重仓警报"，给出当前 margin_ratio + 建议补仓或卖仓
  - `danger`（< 0.2）：红色 banner "即将被强平，最后机会"
- 视觉沿 `docs/style.md`：工业风、粗边框、不可关闭

### 7.2 TradePanel buy 按钮抑制

`components/market/TradePanel.vue`：`margin_status="danger"` 时 buy 按钮 disabled + tooltip 提示。sell 仍可用（让用户自救）。

### 7.3 Portfolio 历史强平行渲染

`pages/user/Portfolio.vue` 的 transactions 表：type=`liquidate` 行用红底 + 文案"强制平仓"，跟 buy/sell 区分。

### 7.4 首页"翻车现场"卡

`Home.vue` 新增 `<RecentLiquidationsPanel>` 组件，从 `/recent-liquidations` 拉数据：

```
┌─ 翻车现场（教育警示） ──────────────────────────┐
│ ⚠️ 高杠杆有风险。净值/借款 < 0.2 即被强平。      │
├──────────────────────────────────────────┤
│ 张三  5min ago                             │
│   原借 500，净值 -30                       │
│   平掉 392 还了 392，剩 138 未还 ☠️        │
│                                          │
│ 李四  1h ago                              │
│   原借 200，净值 30                        │
│   平掉 280 还了 200，剩 80 cash ✅          │
│                       [查看更多]            │
└──────────────────────────────────────────┘
```

每行一句话风格，剩余债务 > 0 用红色"资不抵债 ☠️"tag；全清掉的用绿色"幸存 ✅"tag。

## 8. 错误处理 / 边界

| 场景 | 处理 |
|---|---|
| user.debt=0 在拿锁期被还清 | `if debt <= 0: continue` 跳过 |
| outcome 已结算 | 跳过该 position，让 resolve flow 处理 payout |
| LMSR proceeds < 0（极端 q）| 跳过 + log error，不卡死整个 user |
| 全平后 cash 仍 < debt | 剩余 debt 保留，30min 内不重复扫描该 user（防爆 loop）|
| 用户/quant bot 同时下单 | `FOR UPDATE` 排队，sweep 最坏延一轮 |
| Sweep 中途单 user 卡住 | 单 user 单事务超时跳过，不雪崩整个 sweep |
| Scheduler 跟 loan_sweep 抢锁 | 两边都是 per-user 短事务，不互锁 |
| sweep 跑 60s+ 未完 | `max_instances=1` 防重叠 |
| Admin run-now 与 scheduler tick 同时 | `max_instances=1` 自动序列化 |
| `compute_holdings_value` 慢查 | 用现成 batch 函数，已优化；单 user 慢则跳过 |

**Stuck underwater 防爆**：模块级 `_recently_attempted: dict[int, float]` 记录每 user 上次"扫到但没产生进展（卖 0 个 + 还 0 元）"的 monotonic 时间，30 min 内不重扫。30min 后自然过期（内存清掉这条目）重新尝试——可能用户主动还了一些或市场动了。

## 9. 测试

### 9.1 单元 `tests/test_liquidation_service.py`
- happy：1 user 2 positions，全平 + 部分还债，remaining_debt=0
- happy：1 user 2 positions，cash 不足，remaining_debt>0
- skip：market 已结算 → position 不平
- skip：user.debt=0 → 直接返
- LIQUIDATE Transaction 字段正确
- LiquidationEvent pre/post snapshot 正确

### 9.2 Sweep `tests/test_liquidation_sweep.py`
- 选 user：margin<hard 进入，soft 仅记 warning，healthy 跳过
- stuck cache：扫过的 30min 内不重扫
- max_instances=1：重叠 tick 不互踩
- run-now 同 scheduled-once 结果一致

### 9.3 Admin `tests/test_liquidation_admin.py`
- 非 admin → 403
- admin → 200 + 返回结果
- audit log 写入

### 9.4 Public endpoint `tests/test_liquidation_public.py`
- 匿名可调
- limit 参数有效
- order 按 triggered_at desc
- username join 正确

### 9.5 集成 `tests/test_liquidation_integration.py`
- 真 sqlite：建 user + position + market + debt → sweep 一次 → 校验 cash/debt/positions/transactions/liquidation_events 全对
- SSE：subscribe BROKER → sweep → 收到 trade event

### 9.6 Migration
- alembic 升级 + 降级测试（保 reversibility）

### 9.7 回归
- `test_loan_service.py` / `test_loan_admin.py` / `test_market_*` 全过
- backend full pytest（跳 chart/candle 慢测）

## 10. 监控 / 复盘

每次 sweep 后 structlog 写一条 INFO：
```json
{"event": "liquidation_sweep_done",
 "triggered_count": 2,
 "soft_warning_count": 1,
 "errors": 0,
 "sweep_duration_ms": 234}
```

`liquidation_events` 表本身就是 audit log——任何时候 SQL 拉数据复盘 N 月前发生过什么。

## 11. 风险 & 回滚

**部署风险**：
- alembic migration 加列加表：PG 即时操作，无锁表风险
- scheduler 自动启动后立刻扫一次——但 `liquidation_enabled` 默认 `false`（见 §5.5），sweep 函数最前面就 short-circuit return，安全。验证完后 admin 通过 `/site-config` 接口把它改 `true` 即可灰度开启
- LIQUIDATE TransactionType 是新值，老前端不识别会显示"未知"——可接受，下个前端 release 修

**回滚方案**：
- `liquidation_enabled=false` site_config 一键关停所有强平
- 单条 LiquidationEvent 已写则保留（不回滚已发生事件）
- 极端：还原 git revert，alembic downgrade —— 但 LiquidationEvent 已写的数据会丢失

## 12. 未来扩展（YAGNI 之外）

- 多档阈值（火警 0.3、地震 0.1）—— 当前两档够用
- Email/Telegram 通知 —— 当前用 banner 够
- 强平历史指标看板（每日触发数、累计未还 debt）—— 加 admin 页时再说
- LP 风险监控（被强平用户的累计未还 debt 是 LP 的潜在亏损）—— 加 admin/wealth 看板时再说

## 13. 完成准入清单

实施时按此核对：

- [ ] alembic migration 生成 + 升级 + 降级测试通过
- [ ] `liquidation_service.py` 全 unit + integration 测试过
- [ ] `liquidation_sweep.py` scheduler 测试过 + `_recently_attempted` 防爆机制过
- [ ] admin `/run-now` endpoint 测试过（含 403 / audit log）
- [ ] public `/recent-liquidations` 匿名可访问 + limit/order 测试过
- [ ] 前端 4 个组件（banner / buy-disable / liquidate-row / wall-of-shame）人肉浏览器看过
- [ ] backend full pytest（跳 chart/candle）全过
- [ ] `python -c "import app.main"` OK
- [ ] site_config 4 个新 key 默认值上线后可调
- [ ] CLAUDE.md 红线：base.py 改动经过 alembic 流程 ✓；admin.py + auth 路径无侵入 ✓
- [ ] 风险：`liquidation_enabled` 默认 `false` 上线，灰度开启
