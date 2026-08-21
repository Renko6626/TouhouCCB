# 最终全分支评审 — 单写者内存状态机 阶段 0 + 阶段 1

- 范围：`2900aaf..98ee1ac`（10 commits，分支 `perf/2026-08-21-single-writer`）
- 权威：`docs/superpowers/specs/2026-08-21-single-writer-design.md` § 4 / § 5.1 前半 / § 7.5 / § 8 阶段 0-1
- 评审方式：全分支 diff 逐 hunk + 新老路径逐字段对照（读了 diff 之外的 `market.py` 老路径全文、`liquidation_service.liquidate_user`、`candle_writer`、`realtime`、`stream`、`market_locks`、`core/admin`）。**未跑测试**（实现方已提供 405 passed 证据）。

**结论：FINDINGS**（0 Critical / 4 Important / 13 Minor）。核心资金与一致性不变式全部成立，没有发现双花、q 分裂、内存先于 commit 变更、同批 candle 二次 flush 这类硬伤。4 条 Important 都是「flag 翻开之前必须处理」而不是「代码错了」。

---

## 一、绑定约束逐条核对（全部通过）

| 约束 | 结论 | 证据 |
|---|---|---|
| API 契约零变更（buy/sell/resolve/close/resume + SSE） | ✅ | `writer_ops.py:157-183 / 287-309` 与 `market.py:656-681 / 829-854` 逐字段对照一致（`shares`/`cost`/`new_cash`/`message` 四字段、`trade` payload 十一字段、量化位数 `0.01`、message 文案 `f"成功买入 {shares_d:f} 张 {label}（均价≈{avg_price}）"`）；`op_close/op_resume` 返回 `{"message": ...}` 与 `market.py:258/1165` 一致；`op_resolve` 返回同一个 `SettleResult`；`market_status` publish payload 一致 |
| 内存 q 变更在 DB commit 之后 | ✅ | `_consume` 只在 `await op(...)` 返回后 apply（`market_writer.py:200-212`）；op 内 `async with session.begin()` 退出即 commit，op 内没有任何 commit 之后才 raise 的分支 |
| 回写值 = 镜像 6dp 量化结果（不动点） | ✅ | `new_q_dec[idx] = quantize_cost(q_dec[idx] ± shares_d)`，镜像 `UPDATE ... SET total_shares = new_q_dec[idx]` 绝对值 SET，内存 `st.q_dec = new_q_dec`、`st.q = [float(x) for x in q_dec]`。逐笔恒等，重启从镜像读回精确（`test_writer_buy.py:74` 断言 `o.total_shares == st.q_dec[0]`） |
| DB 失败 → 内存零变更 | ✅ | 业务 `HTTPException` 走 `except HTTPException` 分支，不 apply；非预期异常走 `reload_state` 从镜像重读。`test_writer_buy.py:88-102` / `test_market_writer_loop.py:126-152` 有真断言 |
| `market_locks.py` 一行不动 | ✅ | 不在 diffstat 内 |
| user 锁 / position 锁在新路径保留 | ✅ | `op_buy:97/104`、`op_sell:245/246`、`op_resolve:409-419/460-462`、`op_liquidate_market:546/547-555` 全部保留 `FOR UPDATE` |
| 老路径 flag off 行为零变化 | ✅ | 老路径唯一实质改动是滑点校验提取（`trade_checks.py` 与原 `market.py:563-580 / 736-752` 逐 token 等价，含 `quantize(Decimal("0.000001"))` 位置与错误文案）；其余是 `if WRITER.enabled:` 早返回（关时零成本）。`DEFAULT_SLIPPAGE_BPS` / `HARDCAP_SLIPPAGE_BPS` 全仓 grep 无悬挂引用 |
| `single_writer_enabled` 默认 false、启动读一次 | ✅ | `loan_migrate.py` 加 `("single_writer_enabled", "false", "bool")`；`main.py:78-84` lifespan 内读一次；无任何热重读路径 |
| 资金语义 6dp / 8dp，镜像绝对值 SET | ✅ | `quantize_cost` / `quantize_price` 用法与老路径同位置同参数 |
| flusher pop-then-upsert / 失败回炉 / 绝不同批两次 | ✅ | `candle_flusher.py:57-73`：先 `batch = self._pending; self._pending = {}` 再落库；失败按「batch 是较早方」merge 回。`test_candle_flusher.py:66-96` 显式断言二次 flush 是 no-op 且 volume 不涨 |
| 强平 per-market 独立提交 / HALT 跳过 / noop 不写 event / 汇总一条 | ✅ | `liquidate_user_split` 三阶段；`op_liquidate_market:535-537` 非 TRADING 直接空结果；`liquidation_service.py` 阶段 C `if sold_count == 0 and repaid == ZERO: return None`；`test_writer_liquidation.py` 三个用例分别断言 `len(events) == 1`、HALT 跳过、noop 不写 |
| 阶段 0：publish 只序列化一次，snapshot/ping 仍 per-connection | ✅ | `realtime.py:132` 打包一次（且在锁外，无 await，publish 之间的投递顺序仍是原子的，未引入乱序）；`stream.py:181-182` 直接转发；snapshot/ping 仍各自 `sse_pack`。`test_realtime_broker.py` 用 `blobs[0] is blobs[1] is blobs[2]` identity 断言，是真断言不是恒真 |
| 阶段 2-5 不越界 | ✅ | `realtime.QUEUE_MAXSIZE` 仍 2000（§5.2 是阶段 2）；`_QUOTE_CACHE` 未动（阶段 5）；无 tick 帧；前端零改动 |

死锁复核：新路径的锁序是 `user → position`（buy/sell/liquidate）与 `position(+outcome) → user`（resolve，照抄老路径）。两者方向相反，但 resolve 只锁本市场的 position/outcome，buy/liquidate 只等本用户在**别的**市场的 position，不构成环。同市场的 resolve 与 buy 由同一个 consumer 串行，天然互斥。**无死锁**，但 spec § 4.5 「所有事务都按 user → position 单向拿锁」这句话在 `op_resolve` 上不成立，建议在 spec 或 `op_resolve` docstring 里补一句「resolve 是 position→user 的例外，靠 per-market 串行 + 无跨市场重叠避免环」。

测试真实性抽查：未发现恒真/空断言。ledger 里记录的计划笔误（`assert ... or True`、自引用 import、walrus 笔误）在实现里都已修掉；`test_writer_e2e.py` 的两个用例（writer 开/关）是完整实现而非留白，且用「candle 是否立刻落库」区分新老路径，路由证据充分。

---

## 二、Findings

### Important

#### IMP-1 · consumer 会执行调用方已放弃的命令，且执行时刻无上界
**文件**：`backend/app/services/market_writer.py:194-200`（配合 `:184-188`）

`submit()` 超时后 `asyncio.wait_for` 会 cancel `fut`，但**命令仍留在队列里**。consumer 之后取出它时不检查 `fut` 状态，照常执行完整 DB 事务，最后靠 `if not fut.done()` 把结果丢掉。

失败场景：DB 卡顿 20 s，市场 M 队列里积了 200 条 buy。所有调用方在 10 s 时收到 503「结果未知，请刷新确认」。DB 恢复后 consumer 把 200 条全部执行掉——用户 A 在 T 时刻按当时价格提交的 `accept_any_slippage=True` 平仓单（`TradingView.vue` 与 quant bot 的默认平仓姿势），可能在 T+40 s 按已经被前面 199 笔推走的价格成交。`accept_any_slippage` 关掉了 bps 护栏，`max_cost` 前端不传，于是这笔迟到成交**没有任何价格保护**。老路径没有这个形态：请求超时 = 连接断 = 事务被 DB 侧回滚。

spec § 4.3 只承诺了「命令可能仍在 DB 里执行」，没有承诺「命令可能几十秒后才开始执行」。

修法（一行，低风险）：consumer 取出后先判活
```python
cmd, fut = await q.get()
if fut.done():          # 调用方已超时/断连，放弃执行
    continue
```
更稳的话在 cmd 上打 `enqueued_at`，出队时超过 `SUBMIT_TIMEOUT` 直接丢弃。

---

#### IMP-2 · SQLAdmin 仍可直接改 `Market.status` / `liquidity_b` / `closes_at` / `Outcome.total_shares`，writer 永远看不到
**文件**：`backend/app/core/admin.py:73-84`（`MarketAdmin` / `OutcomeAdmin`，SQLAdmin `can_edit` 默认 True）+ `backend/app/services/market_writer.py`（内存只在 `start()` / `register_market()` / `reload_state()` 三处刷新）

flag 打开后 `MarketState.b` / `closes_at` / `status` / `q_dec` 全是启动时的快照，除了「非预期异常自愈」外没有任何再读 DB 的时机。而 SQLAdmin 是这些字段唯一的手工修改入口（代码里没有 market 编辑端点）。

失败场景：
- 管理员在 admin 面板把某市场 `liquidity_b` 从 100 改成 200 → writer 继续用 b=100 定价并扣款，而 `/market/quote`、`/api/v1/chart`、`services/wealth.py`（强平判定与排行榜）读 DB 用 b=200。**同一笔持仓在成交侧和估值侧用两个 b**，滑点/保证金判定全乱，且不会报错。
- 管理员改 `Outcome.total_shares` 修数 → 下一笔成交的镜像 `UPDATE ... SET total_shares = <memory 值>` 把这次修数**静默覆盖**（绝对值 SET，不是增量）。
- 管理员在面板改 `status` → writer 内存不变，HALT 的市场照常成交 / TRADING 的市场一直拒单。

老架构下这些编辑是即时生效的，所以这不是「本来就有的坑」，是本次架构变更**新引入**的运维陷阱。

处置（二选一，翻 flag 前必做）：`MarketAdmin` / `OutcomeAdmin` 上 `can_edit = False`（`admin.py` 是 CLAUDE.md 红线文件，需用户授权）；或在 spec § 9 风险表 + 运维文档明写「flag 开启后禁止在 admin 面板改市场/选项字段，改完必须重启后端」。

---

#### IMP-3 · sweep 的 writer 分支把「保证金已恢复」的判定与实际强平拆成了两个事务，判定窗口显著变宽，且该分支零测试覆盖
**文件**：`backend/app/services/liquidation_sweep.py:91-116`

老路径：`lock_user` → `user_has_halt_holdings` 守卫 → 重算 `margin_now` → **在同一个事务、同一把 user 锁里**调 `liquidate_user` 卖仓。判定与执行之间窗口为零。

新路径：守卫事务 `async with session.begin()` 在 `:109` 结束就**释放了 user 锁**，然后才调 `liquidate_user_split`；后者阶段 A 重新 lock user，但**只重查 `user.debt > 0`，不重查 halt 持仓、不重算 margin**，随后阶段 B 逐市场排队等 writer。

失败场景：用户 U 保证金掉到 hard 线下被 sweep 选中，守卫通过；此刻 U 在前端手动卖掉一半仓位把保证金拉回安全线（或者往里充值/还债）。sweep 释放锁后 `liquidate_user_split` 阶段 A 只看到 `debt > 0` 就继续，阶段 B 把 U 剩下的仓位全部按 `mode` 强平掉。用户已经自救成功却仍被强平——这是直接的用户资金损害，且 `LiquidationEvent.pre_margin_ratio` 记的是阶段 A 的快照，事后看审计记录还挺"合理"。窗口长度 = 一次 DB 往返 + 每个市场的 writer 排队延迟（拥堵时可到秒级）。

同时 ledger 已把「sweep 的 `WRITER.enabled` 分支无专项集成测试」列为 deferred。整个分支在 pytest 里**从未被执行过**（全套测试都跑在 flag off；`test_writer_liquidation.py` 直接调 `liquidate_user_split`，绕开 sweep）。

修法：在 `liquidate_user_split` 阶段 A 的那把 user 锁里补上 `user_has_halt_holdings` + `margin_now >= hard_thr` 复检（把 `hard_thr` 作为参数传进去），恢复即 return None；并补一个 monkeypatch `WRITER.enabled=True` 的 sweep 集成测试。

---

#### IMP-4 · 停机顺序：writer/flusher 先于 liquidation scheduler 停止
**文件**：`backend/app/main.py:88-96`

```python
await _writer.stop()
await _flusher.stop()
await stop_bot_detection_scheduler()
await stop_liquidation_scheduler()   # ← 最后才停
```

失败场景：停机瞬间正好有一轮 sweep 在跑。
- 若它还没到 `if WRITER.enabled` 判断 → 读到 `False` → **走老路径**，用 market/outcome 行锁直接改 `total_shares`（此时内存已清，不会分裂，但停机窗口里跑的是本该被 flag 关掉的代码路径）。
- 若它已经在 `liquidate_user_split` 阶段 B → 每次 `WRITER.submit` 因 `_queues` 已清而抛 400，被 `except HTTPException` 吞掉记 warning，然后**阶段 C 照样执行还债并写一条 `sold_positions_count=0` 的 `LiquidationEvent`**。公示墙上会出现一条「强平了 0 个仓位」的假记录，`_recently_attempted` 不会被设置（`ev` 非 None），下一轮重启后立刻再强平一次。

修法：把三个 `stop_*_scheduler()` 移到 `_writer.stop()` 之前（注释里「启动顺序 loan → liquidation → bot_detection，停止时反序」的意图本来就是这个）。一行顺序调整，零风险。

---

### Minor

| # | 文件:行 | 问题 | 场景/后果 |
|---|---|---|---|
| MIN-1 | `market_writer.py:202-208` | apply 时 `st.q = [float(x) for x in new_q_dec]`（量化值），但 `st.prices` 取的是 op 用 `old_q[i] + float(shares)` 浮点直加算出来的 `new_prices`。二者的 q 向量在 ulp 级不同 → `st.prices != f(st.q)` | 阶段 1 无害（`st.prices` 全仓只写不读）。阶段 2 的 tick 帧要吐 `prices`，届时「重启恢复精确」对 prices 不成立，且 `reload_state` 前后帧会出现最后一位跳变。建议现在就统一成从 `st.q` 重新导出（删掉 `OpOutcome.new_prices` 的短路），或干脆删掉该字段 |
| MIN-2 | `market_writer.py:221-224` | `CancelledError` 分支只设 future、不 `reload_state`。若 cancel 落在 commit 之后，内存陈旧 | 只在 `stop()` 时可达（停机/`start()` 重入），停机后重启从镜像读回天然正确。但若将来把 `start()` 用作热重载，会留下陈旧 q |
| MIN-3 | `market_writer.py:151-156` | `reload_state` 的 `except` 里 `self._states[market_id].unavailable = True`；若 key 已被 `stop()` 清掉会抛 `KeyError` **穿出** `_consume` 的 except 块 → consumer task 静默死亡 | 该市场此后所有 `submit` 都排到队列里无人消费，每个请求硬等 10 s 后 503，且没有任何日志说明原因。改成 `st = self._states.get(market_id); if st: st.unavailable = True` |
| MIN-4 | `market_writer.py:116-128` | `stop()` 不 drain 队列，队列里未执行的命令的 future 既不被 set 也不被 cancel | 调用方硬等满 `SUBMIT_TIMEOUT=10 s` 才拿到 503「结果未知」，而这些命令**确定没执行**，措辞误导；且 `stop_grace_period: 8s` < 10 s，容器会先被杀，客户端连 503 都收不到。建议 stop 时把残余命令的 future 设成明确的「未执行，已取消」错误 |
| MIN-5 | `writer_ops.py:535-537` / `liquidation_service.py:336-` | writer 强平丢了老路径的两条审计日志：`liquidation_skip_non_trading_market`（每个被跳过的 position 一条）与 `liquidate_user_mode`（mode/pre_margin/thresholds） | 强平是资金敏感操作，出问题时的可观测性下降；ELK 上原有的这两个 event 会在 flag 开启后归零，仪表盘/告警可能悄悄失效 |
| MIN-6 | `market_writer.py:211-216` | `_merge_candles` 与 `BROKER.publish` 在 commit **之后**、`fut.set_result` **之前**，且都在同一个 try 里 | 这两步任一抛异常 → 调用方拿到 500「结果未知」，但交易其实已成功；同时触发一次无意义的 `reload_state`。老路径 publish 也在事务外，异常同样 500，非回归；但 writer 额外丢掉了这笔的 candle 行（要等下次重启 `_resync_recent_candles` 补） |
| MIN-7 | `writer_ops.py:198-207` | 已在 docstring 记录：「持仓不足 + 滑点/总量超限」双违规时，writer 先报滑点/总量、老路径先报持仓 | API `detail` 文案对客户端可见；quant bot 若按 detail 分支处理会看到行为变化。建议同步进 `quant/docs/sse-contract.md` 或 API 文档 |
| MIN-8 | `main.py:78-84` | `WRITER.start()` / `CANDLE_FLUSHER.start()` 没有 try/except，与紧邻的 `_resync_recent_candles`（有兜底）风格不一致 | flag 开着时任一 DB 抖动 → 整个后端起不来。更糟的是 `start()` 内部若在 `_install` 循环中途失败，已安装的市场留着运行中的 consumer task，而 `_enabled` 仍是 False → handler 全走老路径，orphan task 持着队列 |
| MIN-9 | `writer_ops.py:236-243` | `op_sell` 的滑点校验放在 DB 事务内（因为要先读 `sell_fee_rate`），spec § 4.3 要求「校验纯内存，失败直接 reject」 | `site_config` 有 60 s 缓存时不会真发 SQL，实际不占连接；缓存冷启的第一笔滑点拒单会多一次 DB 往返。可把 fee 率缓存进 `MarketState` 或在 op 外预读 |
| MIN-10 | `market_writer.py:37` | `MarketState.seq` 声明了但从不递增；spec § 4.3 第 4 步写着「推进 seq」 | 计划把它划给阶段 2，属于已知留白；当前是死字段，不影响正确性 |
| MIN-11 | `tests/test_writer_e2e.py` | E2E 没有断言 SSE `trade` 事件的形状，尽管「API 契约零变更」包含 SSE，且 writer 在 `writer_ops.py:167-182 / 297-308` 独立重建了一遍 payload | 两处 payload 构造器将来漂移（少一个字段、`fee` 类型变了）不会被任何测试抓到。当前两边形状我逐字段核对过是一致的 |
| MIN-12 | `market_writer.py:171-176` | `submit()` 只检查 `_queues` / `_states`，不检查 `self._enabled` | 当前所有调用点都先判 `WRITER.enabled`，且 `stop()` 会清空 dict，行为正确；但这个不变式是隐式的，加一行显式判断更抗改 |
| MIN-13 | `market_writer.py:158-166` | SETTLED 后 `_states` / `_market_by_outcome` / `_queues` / `_tasks` 条目永不回收 | 每个已结算市场永久留一个空转的 consumer task + 队列。量级极小（每市场一个 task），但长跑进程会线性累积 |

---

## 三、Ledger deferred minors 的 triage

ledger 里明确 deferred 的只有一条，另有两条「实现方测试侧修正」需要确认：

| ledger 条目 | 我的裁决 | 理由 |
|---|---|---|
| **Task 9 minor (deferred)**：sweep 的 `WRITER.enabled` 分支无专项集成测试 | **不 block 合分支；block 翻 prod flag**。且它把 IMP-3 这个真 bug 藏住了 | 这段是全分支唯一「只在生产 flag 开启时才执行、pytest 从不触及」的代码。它不是转录，是手写的新编排（守卫事务 + 释放锁 + 调编排器），语义上就已经引入了 IMP-3 的判定窗口。补一个 `monkeypatch.setattr(WRITER, "_enabled", True)` 的 sweep 集成测试（断言：margin 恢复的用户不被强平、HALT 持仓用户被 skip、noop 用户进 cooldown）是翻 flag 的前置条件 |
| **Task 6/7 备注 (1)**：`_fresh_db` 与 conftest `setup_db` 重复 drop 会洗掉 seed 配置，简化为仅 `WRITER.stop()` | **接受，不 block** | `tests/test_writer_buy.py:18-27` 的注释把原因写清楚了（parity 测试要走 `verify_anti_bot` 读 `activity_mode_enabled`）。conftest 的 autouse `setup_db` 先执行且已 drop+create+seed，隔离性不降 |
| **Task 6/7 备注 (2)**：sell 持仓不足测试加第二买家隔离；`op_sell` docstring 记录次序行为差 | **接受，不 block**（已登记为 MIN-7 的文档跟进） | 这是对「持仓 ≤ 总量」不变式的正确认识，测试改得比计划更严谨；行为差本身已在 docstring 说明 |
| **Task 3 备注**：`Market.status` str-enum 比较无害 | **接受** | `MarketState.status != MarketStatus.TRADING` 在 str-Enum 下正确；`test_writer_admin_ops.py` 的 close/resume/resolve 用例实测覆盖 |
| **Task 4 备注**：flusher `start/stop` loop 无专测 | **不 block** | `flush_once` 的三条关键语义（写入+清空、二次 flush no-op、失败回炉且不 double-count）都有真断言；`_loop` 只是 `sleep + flush_once` 的包装，`stop()` 的最终 flush 由 `main.py` 接线 + E2E 的 `flush_once` 间接覆盖 |

---

## 四、合并前建议的处置顺序

1. **合分支前（代码，都很小）**：IMP-4（一行顺序调整）、MIN-3（`.get()` 防 KeyError）、IMP-1（consumer 出队判 `fut.done()`）。这三条改动都在 5 行以内、各自可独立回滚，且都有明确的错误场景。
2. **翻 prod flag 前（必做）**：IMP-3（阶段 A 补 margin/halt 复检 + sweep 集成测试）、IMP-2（`can_edit=False` 或运维文档 + 重启约定）。
3. **可延后到阶段 2 一起做**：MIN-1（prices 口径统一，阶段 2 要吐 tick 帧时必须先解决）、MIN-11（SSE 形状断言）、MIN-5（强平日志补回）。
4. 其余 Minor 按需。

k6 验收（spec § 2 验收表）仍是人工步骤，未在本次范围内，交用户在 Postgres 环境执行。
