# 当前市场行情内存迁移实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task，沿用用户已选择的子代理执行与独立审阅方式。步骤以 checkbox 跟踪；本轮只修订计划，不启动业务实现。

**Goal:** 让当前行情默认复用已提交内存状态，减少多人读取和 SSE 广播引起的重复数据库调用。

**Architecture:** 复用 PricingRegistry 管理唯一共享市场读快照。FX 数据库事务提交后、释放现有市场 GATE 前同步更新；LMSR 接入已有 writer 的提交应用生命周期。实际资金交易保持现有数据库路径，只读实例保持读库。

**Tech Stack:** 现有 Python、FastAPI、SQLAlchemy async、asyncpg、PostgreSQL、asyncio；不加依赖或迁移。

**Spec:** [设计与时序](../specs/2026-10-18-committed-market-reads-design.md)。方向已在对话确认；文档中的接口名为拟定实现名。

## 高压 profile 后的读码修订

本次只读代码、修订计划，不实施业务修改。复用已有已确认路线，不重新设计资金或交易权威。

- [高压证据](../../../output/fx-stress-profile-20261018/report.md)：SQLAlchemy 和调度为主要 CPU 类别；同市场 GATE 持续繁忙；公共读事务确有阻写实例。内存化首先减少查询/装载，第一阶段仍保留历史共享锁，不能承诺一次解决写锁等待或全部 SSE CPU。
- `PricingRegistry._entries` 现为版本比较记录，`refresh_groups` 每次仍读库，适合作为唯一快照存储改造点。
- 补入旧清单遗漏的 `api/v1/fx_stream.py` 首帧：它先拿 FOR SHARE，再调用 get_public_snapshot；只修改后者会反转 GATE/行锁顺序，必须成组修改。结合高压 GATE 队列，推荐保持原数据库行锁，用 pool_version 核对后 peek 内存，版本不符则同 session 读库，不新增公共行情 GATE 排队。
- 管理费率入参可多于 8dp；快照必须等于数据库实际值，不能只复制未经归一化的 ORM 入参。LMSR liquidity_b 列则确为 float，直接保留原转换即可，不增加精度镜像。
- Task 5 已完成并提交为 `71b8a83`，A/B 证据提交 `d88a939`；单测 2 passed、PG 5 passed。后续性能基线应包含此项，不再默认重复三组 A/B/C。
- 新建市场由现有容量/管理锁覆盖；不在获取新 ID 后持事务追加 GATE。已有市场严格按 GATE→数据库锁；创建冷读缺失不能覆盖随后安装的新状态。

## 本次读码得到的入口清单

| 当前代码入口 | 本次实施方式 |
| --- | --- |
| `credit/trading_context.py` 的 context/versions/prices；`user_trading.py` SSE 初始价格 | 继续走 Registry，切换其实现即可；账户 before/basis/after 和依赖授权保留 |
| `trading_prices.py:PricingPublisher.run` | 暖读不取 GATE；每市场读取一次已提交输入、每轮转换一次定价字典，再按订阅者过滤 |
| `fx/trading.py:quote` | 只替换市场输入，沿用纯市场 AMM 算法和错误映射 |
| `api/v1/fx.py:snapshot` → `get_public_snapshot` | 普通读入口复制当前市场状态；成交量/历史仍沿原路径 |
| `fx/market_data.py:publish_pair_frame` | 原 FOR SHARE 改读 pool_version，版本相符 peek 内存，不符则原 session 读 Pair；不取 GATE |
| `api/v1/fx_stream.py:gen` 首帧 | 与后台广播同样核对版本；订阅/anchor 顺序保留，yield 在行锁外 |
| net/short/borrow quote、风险估值及实际执行 | 暂不迁移，仍有用户资金/金库/准入语义 |

FX 写入修改点已核查：trading 的现货/强平 ledger、shorts 的 open/cover ledger、liquidity 的 fund/withdraw，以及 admin_fx 的创建/patch/archive/delete。实际提交由 Task 2 表列的外层持有者完成。PvE executor 走 execute_trade 的外层提交；触发单只复用 in-session 原语，由 orders.attempt_order 独立提交，必须另接这个事务边界。

## 全局约束

- 不修改 AMM、资金账本、风控准入、幂等键、市场版本冲突处理和数据库持久化语义。
- 不引入 Redis、新表、跨进程同步、用户资金缓存或新的写队列；不复制一套 FX writer。
- 保留接口字段、授权、Decimal 精度、minimum_group_stamps、缺失市场及历史覆盖语义。
- 行情暖读不再查询当前市场字段；snapshot 中的成交量/历史 SQL 和账户检查不算违反该目标。
- 所有 GATE 在数据库事务/行锁之前整批获取，不重入、不补拿。推送和网络发送在锁外执行。
- 新增永久测试仅覆盖迁移带来的真实故障，优先扩展现有测试；不为每个包装层复制完整场景。
- 当前工作区已有未提交融资优化和其他用户改动；实施前列清迁移基线，不能 reset、stash 或广泛暂存。
- 本机临时数据库验证，不触碰生产；提交、推送及部署按当时用户授权执行，不在计划阶段操作。

## 审阅重点

| 重点 | 必须验证的行为 | 所属任务 |
| --- | --- | --- |
| 数据库已提交、内存尚未安装 | context 不宣称旧价格覆盖新账户；失败后拒绝旧快照 | 1、2 |
| 回滚、净交易多腿、幂等重放 | 只安装已提交最终状态，重放不倒退 | 2 |
| 创建、修改、注撤资、归档、删除 | 读取端及时看到状态变更；重启恢复一致 | 2 |
| LMSR 状态重载、已结算市场、精度 | 不返回 unavailable 状态，不丢 q_dec/b 精度 | 3 |
| 公共 SSE 价格与历史覆盖 | 首帧/广播在原共享锁下核对版本，既不补拿 GATE，也不使用旧内存冒充已锁行 | 4 |

## 文件职责

| 文件 | 拟修改职责 |
| --- | --- |
| 新增 `backend/app/services/market_read_state.py` | 只定义不可变普通数据快照及从已加载数据复制的转换函数；不拥有第二个缓存 |
| `backend/app/services/trading_prices.py` | 快照存储、冷加载、安装/失效、版本和共用读取；publisher 数据转换复用 |
| `backend/app/main.py` | reset/预热/调度器启动顺序及关闭清理 |
| `backend/app/services/fx/trading.py`、`shorts.py`、`net_trading.py`、`borrow_buy.py` | 提交前捕获最终 FX 状态，事务成功后安装 |
| `backend/app/services/fx/orders.py` | 触发单自有事务提交后、GATE 内安装最终状态 |
| `backend/app/services/fx/liquidity.py`、`backend/app/api/v1/admin_fx.py` | 注撤资与市场管理提交后的快照维护 |
| `backend/app/services/credit/execution.py` | 强平外层事务提交后、GATE 内安装 |
| `backend/app/services/market_writer.py`、`writer_ops.py`、`backend/app/api/v1/market.py` | LMSR 提交应用、注册、重载、创建/管理生命周期的读快照同步 |
| `backend/app/services/credit/trading_context.py` | 市场快照读取与用户依赖查询合并 |
| `backend/app/services/fx/market_data.py` | 公共推送复用市场快照，保留历史一致性部分 |
| `backend/app/api/v1/fx_stream.py` | 公共 SSE 首帧版本核对与显式快照传参；消费者循环本次不改 |
| `backend/app/api/v1/fx.py`、`user_trading.py` | 核查调用入口，必要时适配，不更改响应协议 |

不默认修改 `account_events.py` 的账户通知语义；FX 市场快照安装必须先于现有 `publish_accounts`。LMSR 保留原通知位置，通过共享 GATE 保证后续账户补读顺序。机器人和触发单都复用同一 Registry，触发单在自身提交边界安装；不在调度器中再维护一份快照。

## Task 1：建立共享快照入口与生命周期

**Files:** 新增 `services/market_read_state.py`；修改 `services/trading_prices.py`、`main.py`；新增聚焦测试 `tests/test_market_read_state.py`。

**Interfaces（拟定）:**

- `FxReadState`：frozen dataclass，包含 FX 定价八字段和 `FxPairPublic` 实际需要的元数据；Decimal 保留数据库精度。
- `LmsrReadState`：frozen dataclass，包含 b 的原 float/decimal_string 表示、tuple outcome_ids/q_dec、status、closes_at；不新增 b 镜像字段。
- `PricingRecord`：不可变 state、stamp、observed_at；只在安装时计算定价 hash/修订号。
- `REGISTRY.install_committed(state: FxReadState | LmsrReadState) -> None`：同步、无 IO；已有市场调用者持写 GATE，启动预热/新建市场按设计中的例外处理。
- `REGISTRY.invalidate(key: GroupKey) -> None`：同步移除可用快照；不能把无效状态伪装成数据库不存在。
- `REGISTRY.read_groups(keys: Sequence[GroupKey], *, require_coverage: bool = True) -> list[FxPricingGroup | LmsrPricingGroup]`：async；账户相关默认整批取共享 GATE。Publisher 传 False，暖态直接复制，缺失时才按冷加载规则取 GATE；不得用该宽松读取宣称账户覆盖。
- `REGISTRY.read_fx(pair_id: int) -> FxReadState`：async，供纯行情函数使用；暖读不取 GATE，冷读按规则加载，不存在时保持原 404。
- `REGISTRY.peek_fx(pair_id: int) -> FxReadState | None`：同步、不查库、不取锁；检查当前进程暖读资格后返回不可变已提交值。公共行锁路径只用此接口。
- `REGISTRY.configure(*, memory_products: frozenset[str]) -> None`：启动时设置已完成写入覆盖的产品；自建 Registry 默认空集合，现有数据库测试保持原语义。是否允许暖读还取决于当前写所有权，不新增外部配置开关。
- `REGISTRY.refresh_groups(keys)` 暂保留兼容代理，业务语义和注释改为读取已提交状态；之后无需为了改名批量清理调用方。
- `REGISTRY.warm_fx() -> None`：启动时批量加载 FX；LMSR 由 Task 3 接入。

- [ ] 扩展聚焦测试：相同输入 stamp 不变、输入变更 revision 上升、公开元数据修改不污染定价 hash、返回对象不能修改共享状态；两次暖读不调用 session factory。
- [ ] 实现普通数据复制、Registry 安装及数据库回退。内存只存数据，不存 session/ORM；DTO 转换遵守当前字符串精度及 observed_at/server_time 分工。
- [ ] 为冷加载增加“共享 GATE → 单键加载锁 → 新 session”的顺序；安装与写操作不竞争异步 Registry 锁。多键一次获取 GATE；已持 GATE 的内部调用用明确的锁内复制函数，不重入公共 read 方法。
- [ ] 聚焦测试区分两类语义：持有写 GATE 时，普通行情暖读可立即返回上一份完整状态，而账户覆盖读取等待安装后通过；两者都不执行暖态市场 SQL。不要机械地给所有读取加共享 GATE。
- [ ] 未完成 Task 2 前 memory_products 为空；完成 FX 写入覆盖后先设为 `frozenset({'fx'})`，Task 3 完成再加入 lmsr。reset/预热在经济写任务启动前，移除 lifespan 末尾重复 reset；不能提前缓存未覆盖写入的产品。
- [ ] 所有权丢失和只读模式绕过暖态缓存；数据库不存在不永久负缓存；错误仅使涉及市场不可用。
- [ ] 验证：`cd backend && venv/bin/python -m pytest -q tests/test_market_read_state.py tests/test_trading_context.py`。原直接 SQL 修改测试明确使用读库模式或失效后重载，不削弱字段和版本断言。

**交付:** 可以独立验证的快照组件；生产读取尚未切换，写入覆盖完整后再启用。

## Task 2：接通所有 FX 提交入口，再启用 FX 暖读

**Files:** Task 1 的快照模块与 Registry；`fx/trading.py`、`shorts.py`、`net_trading.py`、`borrow_buy.py`、`liquidity.py`、`orders.py`；`credit/execution.py`；`api/v1/admin_fx.py`；现有对应 FX 测试。

**Interfaces（拟定）:**

- `stage_fx_pair(session: AsyncSession, pair: FxPair) -> None`：在实际修改点登记本事务已加载的 Pair；记录绑定当前最外层事务，同一 pair 保留最终对象，不执行 SQL、不安装。与账户通知 hint 不同，描述捕获失败不能吞异常当作空集合继续提交并永久保留旧快照。
- `capture_fx_changes(session: AsyncSession) -> tuple[FxReadState, ...]`：在必要 flush 完成后、commit 前物化并取走当前事务的状态；沿用 `capture_accounts` 的事务归属约束，回滚遗留描述不可在下一事务发布。
- `install_fx_changes(changes: tuple[FxReadState, ...]) -> None`：commit 后同步安装；异常时先 invalidate 涉及键再记录提交后故障。删除入口在提交后显式 invalidate，不发布已删除状态。

登记与捕获只服务已确认的市场快照，不扩展为通用事件系统或 ORM 全局 after_commit 钩子。

| 修改来源 | 实际提交持有者 | 安装位置 |
| --- | --- | --- |
| 现货，包括机器人 | `fx/trading.execute_trade` | commit 后、GATE 内、账户通知前 |
| 净交易，多腿/融资 | `fx/net_trading.execute_net_trade` | 同上，只捕获最终 Pair |
| 借款买入兼容入口 | `fx/borrow_buy.execute_borrow_buy` | 同上 |
| 开空、平空 | `fx/shorts._execute_player_short_write` | 同上 |
| 触发单买卖、融资、平仓 | `fx/orders.attempt_order` | 其 commit 后、当前 GATE 内，早于锁外 publish_accounts |
| 强平卖出、强平平空 | `credit/execution.execute_user` 的局部事务上下文 | 上下文内部 commit 后、账户通知前、GATE 内 |
| 注资、撤资 | `fx/liquidity` 的 impl | commit 后，`refresh` 等任何 await 之前 |
| 创建、修改、归档、删除 | `api/v1/admin_fx` | 成功提交后、管理市场 GATE 释放前 |

- [ ] 在已有真实业务测试中补足最小场景：交易成功后读到数据库同值；净交易跨零多腿安装最终值；失败回滚保留旧值；旧幂等键重放不覆盖随后成交。
- [ ] 在 ledger 实际修改 Pair 的位置登记；外层事务拥有者捕获并安装。不在 `_in_session` 原语内提交或提前更新全局状态。保持现有用户锁、市场锁、风险重试次序。
- [ ] 强平处将原 `notifying_transaction` 调用替换为 `credit/execution.py` 内的局部 `_liquidation_transaction(session)`：沿用 `session.begin()`，退出事务前捕获账户和 FX 描述，成功提交后先安装 FX，再执行原 `publish_accounts`。必须保留事务内部提前 return 时的提交/通知行为；不能仅在原上下文之后加安装语句，因为原 helper 已经发出账户通知，且提前 return 会跳过该语句。不扩大通用账户事件模块或注册全局 ORM 钩子。
- [ ] 对照全仓库所有 FX 当前状态赋值、批量 update 和 Pair 删除，确认无遗漏；逐项记录实际调用链。PvE 走 execute_trade，触发单走 execute_fx_instruction_in_session 后由 attempt_order 提交；不能只搜索 reserve 赋值而漏掉实际提交者。
- [ ] 复用 `tests/test_fx_trigger_orders.py` 与 `tests/pg/test_pg_fx_trigger_orders.py` 的成功/失败/重放用例，断言成功后共享快照等于数据库最终状态；失败及已成功订单再次扫描不覆盖随后交易的新快照，不复制整套资金断言。
- [ ] 覆盖管理生命周期测试：费率、暂停/reduce_only、名称、注撤资、归档、删除与新建；创建事务采用现有容量锁和市场登记顺序，保证响应前安装。
- [ ] 管理创建/修改的必要 refresh 前移到 flush 后、commit 前，之后才捕获快照；费用8dp、储备6dp字符串与重启后读库一致。现有 PG 用例补一个 `0.123456785` 入参场景，独立断言提交后内存费率等于数据库实际返回值，避免复制舍入算法。其余交易不新增 refresh。
- [ ] 创建时沿用容量/管理锁，commit 后无 await 安装新键；冷加载若查到 None 不得把它写回覆盖已有条目。扩展聚焦测试控制一次“旧缺失查询返回晚于创建安装”的次序，确认新条目仍可读；不新增永久 tombstone 或 ID 预分配。
- [ ] 处理 commit 异常/取消导致结果不确定时的失效。数据库已经成功提交的后续错误不可用 rollback 冒充撤销；不引入新的交易重试或补偿流程。
- [ ] 在 `tests/pg/test_pg_trading_context.py` 增加真实 PostgreSQL 时序用例：暂停在 commit 完成/安装之前，新 context 等待 GATE；安装并释放后返回匹配新账户的价格。另用可控旧冷读与新写竞争验证不倒退。
- [ ] 全部入口接好后启用写实例 FX 暖读；故意丢弃 pricing wake 后，下一周期仍从新快照发现变化。
- [ ] 验证现有 `test_fx_trading_service.py`、`test_fx_trading_in_session.py`、`test_fx_net_trading.py`、`test_fx_short_trading.py`、`test_fx_short_liquidation.py`、`test_fx_liquidation_service.py`、`test_fx_trigger_orders.py`、`test_admin_fx.py`、`test_admin_fx_multi_pair.py`。PG 测试使用 Task 6 的临时 test 库。

**交付:** FX 完整写入覆盖，context/prices/versions 的 FX 市场字段默认走内存；交易执行仍读取数据库。

## Task 3：让 LMSR 价格读取复用已有 writer 状态

**Files:** `market_writer.py`、`writer_ops.py`、`api/v1/market.py`、`trading_prices.py`；现有 `test_market_writer_state.py`、`test_market_writer_loop.py`、`test_writer_admin_ops.py`、`test_trading_context.py`。

**Interfaces:** 复用 Task 1 的 `LmsrReadState`、`install_committed`、`invalidate`、`read_groups`；不返回可变 MarketState。

- [ ] b 沿用当前 float→decimal_string 表示，q_dec 保留 Decimal 6dp；在 `_apply_outcome` 成功完成后、GATE 内安装完整只读投影，不重新查询 Outcome，不增加额外 b 镜像。
- [ ] 保留 LMSR op 内现有 `notifying_transaction` 位置；账户通知可能先入队，但由 Task 1 的共享 GATE 保证 context/prices 补读等到 `_apply_outcome` 和读快照安装完成。用现有 writer 测试覆盖该次序，不为通知顺序扩展账户事件模块。
- [ ] `_install`、register/reload、创建与管理路径使用相同安装约定；writer unavailable 时先使读快照失效。重载失败不能直接使用旧 q 冒充健康状态。
- [ ] 检查 settled 市场及 writer 未加载市场，保留数据库冷加载；确认其后续管理写入如何失效。不得把无 writer state 当 missing。
- [ ] 扩展现有测试验证买卖/状态变更、重载、q_dec/b 精度、旧重放不倒退；只读实例继续读库。验证命令：`cd backend && venv/bin/python -m pytest -q tests/test_market_writer_state.py tests/test_market_writer_loop.py tests/test_writer_admin_ops.py tests/test_trading_context.py`。

**交付:** FX 与 LMSR 共用读入口，正常价格轮询不再重新查询 LMSR Market/Outcome；writer 的交易权威与队列不变。

## Task 4：切换行情入口和 SSE，保留历史协议

**Files:** `fx/trading.py`、`fx/market_data.py`、`api/v1/fx_stream.py`、`trading_prices.py`，必要时 `api/v1/fx.py`、`user_trading.py`；现有 `test_fx_market_data.py`、`test_fx_market_data_integration.py`、`test_trading_context.py`。

**Interfaces（拟定）:** `get_public_snapshot(db, pair_id, *, pair_state: FxReadState | None = None)` 保留原响应；`quote` 通过 `REGISTRY.read_fx` 获取纯市场输入；publisher 使用 `read_groups`。

新增 `fx/trading.py:read_public_pair_state(db: AsyncSession, pair_id: int) -> FxReadState`（async）：用原共享锁查询 pool_version；相符时使用 peek_fx，不符/无缓存时用当前 session 的 `_pair` 复制普通数据。此辅助只提供本次响应输入，不安装缓存，不获取 GATE；缺失保持原 404。

- [ ] 将纯市场 quote 和 HTTP snapshot 的 Pair 获取切到 read_fx；复用现有 AMM/报价算法与异常映射。含用户资金的 net/short/borrow quote 保持原样。
- [ ] 读库模式/失去所有权时，quote/get_public_snapshot 复用调用方传入的 db 读取 Pair，保持原事务及测试 session 语义，不擅自改用全局 session factory。已持行锁的调用方始终显式传 pair_state；Registry 的冷加载只用于未持数据库事务的普通入口。
- [ ] `publish_pair_frame` 和 `fx_stream.gen` 首帧调用 read_public_pair_state，并将 pair_state 显式传给快照构建；保留 volume、history_version、tail cursor、invalidated 和 subscribe→anchor 顺序。首帧原有“行锁不可用”回退仍保留，只从当前 session 读 Pair，不宣称历史前缀，也不补拿 GATE。
- [ ] 保持原公开字段与无订阅者不构建帧的行为；不把储备/费率随内部 DTO 泄露进公开 payload，不改 24h 精确聚合及 `/history` 共享锁。
- [ ] PricingPublisher 采用 require_coverage=False 的暖读；每市场每轮至多 model_dump 一次，按订阅者依赖过滤复用只读结果，保留原队列/背压/编码接口。公开 BROKER 已共享 bytes，不重复改造。
- [ ] 扩展现有测试验证相同市场多人读取共享输入、不同用户订阅过滤、价格更新、无变化 stamp 稳定；使用 SQL 观察确认暖态市场输入无查询，不要求历史/认证 SQL 为零。
- [ ] 在 `tests/pg/test_pg_fx_history_locking.py` 复用 PG 锁测试基座覆盖首帧/广播与交易并发：都能完成，不补拿 GATE；模拟数据库已提交但内存尚未安装，确认版本不符时返回数据库新值且不把回退结果反向安装。保持原历史覆盖断言；直接 SQL 测试显式用读库模式，内存写入测试走实际提交入口。
- [ ] 运行 `cd backend && venv/bin/python -m pytest -q tests/test_fx_market_data.py tests/test_fx_market_data_integration.py tests/test_trading_context.py`。真实 PG 保留 `tests/pg/test_pg_fx_history_locking.py`，不修改其锁等待与历史证明断言。

**交付:** 当前行情字段默认读内存，公共和私人 SSE 共用状态；历史语义保持原约定。

## Task 5：合并用户依赖身份查询，独立评估收益

2026-10-18 补充授权：用户要求此任务由子代理先行完成，与市场内存迁移计划并行；其余任务仍停留在计划阶段。A/B/C 使用各自变更构造代码对照，不要求按本文编号实际先后实施。

当前进度：已完成。`tests/test_trading_context.py` 为 2 passed；真实 PG context 为 5 passed；[A/B 报告](../../../output/fx-dependency-query-ab-20261018/report.md)确认 versions SQL 7→5、prices 5→3，交易吞吐未改善。生产改动提交 `71b8a83`，证据提交 `d88a939`，未推送或部署；实施内存化时不要重做此任务。

**Files:** `credit/trading_context.py`、现有 `tests/test_trading_context.py`、`tests/pg/test_pg_trading_context.py`。

**Interfaces:** `read_dependencies(db, user_id, current_group=None)` 返回类型和排序不变；使用带 product 标签的 `UNION ALL` 一次返回三类身份，沿用 Python 去重和排序。

- [x] 保留 LMSR Position INNER JOIN Outcome、零余额/零份额行、不同 product 同号 ID、current_group 和 missing_groups 语义；不替换成 risk.discover_dependencies。
- [x] 复用现有账户基准/缺失身份/授权用例补足缺口，验证 prices 仍拒绝不属于用户依赖的市场。context 已从 basis 复用依赖，不再重复查询。
- [x] 运行 `cd backend && venv/bin/python -m pytest -q tests/test_trading_context.py` 及 PG context 用例。确认一次依赖查询由三次变一次，不声称数据库扫描减少三分之二。

身份准入的标量化可另行局部优化；本次不复制 JWT 校验、不缓存封禁、不加入用户依赖长期缓存。

**交付:** 常见版本检查和 SSE 依赖复核少两次 SQL 往返，效果与市场内存迁移分开记录。

## Task 6：集中验证、本机性能对照及回退说明

**Files:** 必要时复用本机压测脚本；新增结果目录 `output/fx-market-memory-ab-20261018/`；更新本计划完成项及事故记录，不改生产配置。

- [ ] 固定真实对照基线：包含先前融资优化，但不包含本迁移。核对当前脏工作区与隔离分支差异，记录实际代码 hash；禁止把旧远端版本直接当作“迁移前”。
- [ ] 实施前先为已有融资差异保留独立基线提交或完整快照，再开始修改同一个 net_trading 文件；避免将先前优化和此次内存化混进同一提交。此步骤只整理已完成工作，不扩展融资逻辑。
- [ ] 执行 Task 1–5 相关现有测试的并集，每个文件集中运行一次；需要失败定位时仅重跑相关部分。新增测试限于上述快照一致性和精度故障。
- [ ] 本机创建独立临时 PG test 库，依据 `tests/pg/README.md` 显式提供 `TEST_PG_DATABASE_URL`，用 `venv/bin/python -m pytest -q -m pg tests/pg/test_pg_trading_context.py tests/pg/test_pg_fx_history_locking.py tests/pg/test_pg_fx_trigger_orders.py`，费率归一化场景在现有 PG context 用例中覆盖。未设置 URL 而 skip 不算通过。
- [ ] 做 A/B：两版都包含已提交的依赖查询合并和相同融资优化，仅市场快照/读取迁移不同。同一机器、临时 PG、新建同种子库、单 Uvicorn worker、相同连接池配置。Task 5 的独立收益已有证据，不再默认重复 A/B/C。
- [ ] 先做读多写少场景：相同市场 100 用户、公共/私人 SSE 各 100 连接，固定低频交易；比较当前市场 SQL、总 SQL、CPU、事件循环延迟、读接口 P95/P99、在线连接数。无成交时确认成交量窗口仍按时间滚动。
- [ ] 再复用之前混合场景：60 现金/20 融资/20 空头，normal 60s、active 45s、stress 30s，stress 为所有用户合计 25 次交易尝试/s；保留原错误率 >5% 或成功交易 P99 >2s 的 30s 滚动保护。若提前停止，明确未完成阶段。
- [ ] 容量 A/B 不启用 SIGPROF/py-spy 或逐请求重度追踪；SQL 分类计数采用各组相同轻量采集。热点仍不明时另跑 profile，不将有采样扰动的吞吐当容量结果。
- [ ] 报告价格输入 SQL 是否消除、依赖 SQL 是否 3→1、CPU、成功吞吐、交易尾延迟、市场 GATE 等待、连接池峰值及公共行锁等待；正常与活跃阶段分别比较。无法证明收益时不以测试通过代替性能结果。
- [ ] 按本次高压教训记录实际 HTTP 组成、chart/history 补读、SSE body 数及停止边界；脚本参数相同不等于完成工作量相同。高压 CPU 归因另跑且只在 stress 开启，不把取消/收尾混进样本，也不把两轮吞吐差额全算 profiler 开销。
- [ ] 回退方式为回退此次读迁移代码并重启（无 schema 回退）；不为回退增加生产双读框架。只读实例、资金交易和历史路径保持可读库；停止测试进程并清理临时令牌。

**验收标准:** 正确性检查通过；暖态市场输入查询消除；多人场景不按订阅人数重复查市场或转换同一输入；报告实际性能变化与残余瓶颈。不给吞吐提升倍数或交易 P99 作无证据承诺。

## 执行顺序与检查点

1. Task 1 + Task 2 作为第一组：完整 FX 提交覆盖后再开暖读，可先评审和提交。
2. Task 3 + Task 4：统一 LMSR 读取及行情/SSE 消费路径，确认历史协议。
3. Task 5 已完成；后续两版均包含它，避免反复验证同一局部优化。
4. Task 6 集中验证和压测；按证据决定后续是否研究公共共享锁、调度开销及交易内重复 ORM 工作。

沿用用户已选择的委派方式：主 agent 按以上顺序交接明确文件所有权的实现任务，再派独立审阅与必要修复；避免多个执行者同时修改 `trading_prices.py` 和提交边界。SSE 消费者任务/断连检查优化另列后续任务，不混入本次内存化 A/B。计划本身不授权生产部署或新增架构。
