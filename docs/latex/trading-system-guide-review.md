# 唯一 writer 版本审阅状态（2026-10-18）

当前指南已更新至 `6c76757` 的唯一 writer / 已提交快照模型，PDF12页。整体及限定修正审阅已批准，路径索引与旧GATE注释已修正。两次XeLaTeX编译和页面截图检查记录见 [修正报告](../../output/fx-single-writer-ab-20261018/validation/final-fix-report.md)，[最终审阅](../../output/fx-single-writer-ab-20261018/code-review.md)与[性能报告](../../output/fx-single-writer-ab-20261018/report.md)另存。

下文保留迁移前审阅作为历史记录，涉及GATE、LMSR通知先于apply及旧读取等待的描述已被本次迁移替代，不能当作当前实现说明。当前普通读不等待writer；精确公共历史组合仍可等待数据库，FX金融计算继续以事务内数据库状态为准。

---

# 交易系统指南事实核对（2026-10-18 当前工作区；只读，未运行测试）

## 主要请求与边界

1. **纯 FX 报价与带账户报价分开讲。** `fx.py:create_quote:101` 不认证，`trading.py:quote:250` 只做 AMM 数学；`_read_pair_state:198` 先 peek 已提交内存，owner 可复用且无现存事务时才冷读 registry，否则用调用方 DB。普通 snapshot `get_public_snapshot:856` 同样读市场输入，但另有成交量/历史元数据读取，并非整个接口零 SQL。`net_trading.py:_read_quote_base:199/quote_net_trade:216` 仍在 DB 联读 Pair/User/Wallet/Treasury 加空头数据并模拟账户；`borrow_buy.py:quote_borrow_buy:99` 仍读 Pair、依赖、用户、规则和钱包；`shorts.py:quote_short:1734` 亦为带账户、风控的只读建议报价。都不能承诺正式交易会采用预览价格。

2. **FX 真实 HTTP 交易认证不是独立短 session。** `fx.py:trade_net:272/create_trade:109` 等仍依赖 `current_active_user`，`core/users.py:get_current_user:63` 使用请求 session 读取 User；`net_trading.py:execute_net_trade:378` 在等待 GATE 前 rollback 结束认证/发现的事务（392–395）。`trading.py:execute_trade:637–646` 普通现货先目标独占 GATE 乐观尝试；有债才退出发现完整依赖再尝试。净交易显式借款会提前发现依赖，其余可先目标尝试；`borrow_buy.py:60–70`、`shorts.py:_execute_player_short_write:1406/1432` 先发现完整依赖。不可把所有路径都画成一开始无条件发现。

3. **FX 正式结算：** `net_trading.py:execute_net_trade_in_session:260` 目标 Pair 独占 GATE 下按 Pair→User→wallet/shorts/treasury 取得数据库锁；先查幂等回执，再核经济版本、完整依赖和规则，锁内重发现/模拟风控（276–309），多腿在同一外层事务。核心只执行、不 commit；包装捕获账户 hints，`market_read_state.py:commit_fx_changes:105` flush→捕获不可变市场字段→DB commit→无 await 安装 registry，再释放 GATE。`trading.py:execute_trade_in_session:346/371` 同样 Pair→User→Wallet→Treasury。

4. **通知有真实差异：** 净交易 `net_trading.py:403–409` 和融资 `borrow_buy.py:81–88` 在 GATE 内 commit+install 后发私人 hint 和 market-data hint，出 GATE 后 enqueue 公共发布；现货 `trading.py:661–681` 私人 hint 在 GATE 内，market-data hint 在出 GATE 后，且额外 refresh trade 才 enqueue；空头 `shorts.py:1450–1478` 私人 hint 在 GATE 内，market-data hint/public enqueue 在外；条件单 `orders.py:165–171` 出 GATE 后才发两种 hint 和 enqueue。不要把所有通知时序画成完全相同。

5. **LMSR 多一层已有 writer 队列：** `api/v1/market.py:_release_request_connection:531` 在 submit 前关闭请求 session。`market_writer.py:submit:278` 每市场有界队列；不允许持任意 GATE 等 writer。consumer `_execute_gated:347` 先 GATE 再 op，必要时释放后发现信贷依赖。`writer_ops.py:op_buy:174` 等使用 `account_events.py:notifying_transaction:103`：DB commit 后即 publish_accounts，之后 op 返回，writer `_apply_outcome:387–410` 才更新 q/price/状态/candle 并安装 registry，仍在 GATE 内。因此 LMSR account_changed 可早于内存 apply；覆盖账户读必须等待同一 GATE。tick feed 位于 consumer 的 gated 执行返回之后（market_writer.py:313–329）。

6. **账户覆盖读不是简单的缓存读：** `user_trading.py:trading_user_id:17` 才是认证独立短 session；`credit/trading_context.py:read_trading_context:78` 先独立 session 读 before版本、账户、规则，关闭，再 registry.refresh_groups→共享 GATE，最后短 session 重读 after版本，最多3次。`read_trading_versions:94` 关闭账户/依赖 session 后共享 GATE；prices:103 先验证许可依赖、关闭 session 后共享 GATE。`trading_prices.py:_read_records:147` require_coverage 默认 true，暖命中仍共享 GATE；纯公开行情 require_coverage=false 可直接返回。`credit/gates.py:_may_jump_queue:150` 与排队键相交不得插队，后到共享读会排在写者后。当前高写入排队回退未修复。

7. **公共 SSE 与私人 SSE 两套职责：** `fx_stream.py:stream:56` 先 subscribe/anchor 后读首帧；`trading.py:read_public_pair_state:210` DB SELECT pool_version FOR SHARE，匹配时内存 peek，不匹配则同锁 session fresh Pair，不额外获取 GATE；首帧 quote/tail/cursor在同已提交前缀，生成后 commit释放 session再长连接。SQLAlchemyError 只读降级保留报价但 history_ready=false（fx_stream.py:80–100）。后台广播 `market_data.py:publish_pair_frame:199/246` 同样 pool_version guard，保留增量事件/历史版本失配失效，不是零 SQL。私人 `user_trading.py:trading_stream:75` 订阅在权威重读之前，sync含版本/规则/组价格，15秒短 session检查准入/规则/依赖；无长期 DB session。`account_events.py:enqueue:49` 只发经济版本提示；前端需要重读权威账户。共享 `trading_prices.py:PricingPublisher.run:235` 按所有订阅依赖的组集合每秒或唤醒一次读取（require_coverage=false），一次序列化/分发，不随订阅数重复市场读取。

8. **条件单/强平/管理：** 条件单 `orders.py:attempt_order:82/127–171` Pair GATE→Pair/User/Order锁→复用净交易/空头内核→资金及订单成功状态同事务 commit+install；外层提交负责人统一发通知。创建/撤单仅订单状态，不等同成交。强平 `credit/execution.py:execute_user:787` 先发现/估值，整批 GATE→Pair/User→锁内重验；`_liquidation_transaction:770` begin退出即 DB commit，然后 install FX 和账户 hint；FX卖出/回补外围发布，LMSR需释放 GATE/session后 submit writer（900–910）。管理既有 pair `admin_fx.py:_pair_update_gate:76` 先 close请求 session→目标独占 GATE，`update_pair:314` 锁内重读+bump版本→commit+install；注资/撤资 `liquidity.py:fund_pair:29/withdraw_pair:75` 同样目标独占、Pair/Treasury锁和commit+install。创建新 pair `admin_fx.py:create_pair:277–309` 是新键 commit/install无该键 GATE的明确例外。

## 权威与恢复

- 账户现金/金债、持仓/成本、空头债、成交回执、账本/审计、池与版本在 DB 持久化。FX写结算仍锁 DB Pair，不从 registry 扣池。`market_read_state.py:FxReadState:10` 是不可变已提交市场输入，非ORM/session；非整个账户缓存。
- LMSR `market_writer.py` 开头明确运行时 q 为真源，DB Outcome.total_shares 为每笔 commit 内同步镜像；当前 op 先算新q并提交DB，commit成功后 consumer apply运行态，重启/异常从DB镜像恢复。文案应区分“正常运行时定价源”与“持久化恢复依据”，不宜一句“所有内存永远非权威”覆盖 writer。
- `trading_prices.py:_can_reuse:49` 仅已配置产品+owner+writes_enabled+非read-only可复用；非owner/read-only仍走DB，不能依赖另进程及时更新它的内存。`_record_locked:116` 冷态在整批共享 GATE 后单键 loading lock+短session加载，`main.py:176–179` 启动reset/configure/warm_fx。
- 回滚未安装；`commit_fx_changes:109–115` commit抛错会 invalidate，因可能落库但确认丢失，不承诺未成交；安装失败 invalidate+记录日志，后续冷读。幂等重放返回原回执并rollback释放锁，无第二次结算。LMSR submit超时/异常也明确结果未知，`market_writer.py:296/337` 不把超时解释成交易失败；异常从DB镜像reload。

## 性能措辞

唯一直接证据 `output/fx-market-memory-ab-20261018/report.md`：固定readheavy目标Pair输入1367→0，总SQL8925→7570，应用CPU39.75%→36.78%；不是所有SQL为0，也非已证实容量提高。stress成交后context P99 384→7135ms，after共享GATE等待P99 6919.66ms。锁内work P99约0.045ms（调用方提供的分布数据）与等待P99不属于同一个请求，不能逐项相加成时间轴；建议时间轴用“排队约秒级，拿到后取快照很短”并标统计口径。两版stress均因原成功成交P99保护停止，未跑burst；尚未部署。GATE既有（9/30），本次账户覆盖读接入共享GATE（10/18），不能写成今天新建交易串行化队列。候选拆分/弱化覆盖读取机制尚未实现，指南应保留为待决策而非现状。

# 成稿独立审阅（trading-system-guide.tex，2026-10-18）

结论：主要交易/账户时间轴、LMSR通知先于apply、读写权威、未部署和当前排队回退均与代码相符。P99明确为不同分布且未相加。未发现需要改变业务的事项；建议修正以下具体表达后交付。

1. **中：公共SSE图应真正画成互斥分支。** tex:272–273 当前仍在从上往下时间轴先画“版本相同复用”，再画“版本不同读Pair”，虽然279行解释二选一，图本身易误读为必经两步。用alt边框/虚线分隔标“二选一”，或同一高度并列两框汇合；回源条件写“无快照或版本不同”，与 trading.py:read_public_pair_state:220–223 的 `state is not None and state.pool_version == version` 一致。

2. **中：限定“新建市场例外”为“新建FX市场”。** tex:361附近“新建市场...提交并同步安装...不会...补拿新市场GATE”只有FX创建适用（admin_fx.py:create_pair:277–309）。LMSR api/v1/market.py:create_market:241–245 先释放创建session，再 register_market；market_writer.py:register_market:192–207 明确取新市场独占GATE再新session加载/安装。建议段首改“新建FX市场有一个例外”，末尾加一句“LMSR创建则在提交并关闭请求session后，另取GATE注册writer”。

3. **低但有事实歧义：限定as_of是行情组字段。** tex:258“as_of表示行情被观察或安装的时点”应改为“行情组内的as_of表示快照被观察或安装的时点；context顶层as_of与server_time是响应时钟”。trading_prices.py:_groups:166–172 用 record.observed_at，而 credit/trading_context.py:read_trading_context:91 的顶层as_of=now_string()。

4. **低：FX图说明持续限定净交易主线，补一句已存在的现货差异。** tex:171从“以净交易入口为主线”变成“本图是普通FX外层执行器的主线”，但图中“账户提示；行情唤醒→释放GATE”并非普通现货的实际顺序。建议仍写“本图对应净交易入口”，并补“现货与独立空头的行情runtime唤醒在释放GATE后；净交易、融资的该唤醒在GATE内，公共发布队列均在外”。依据 trading.py:661–681、shorts.py:1450–1478、net_trading.py:403–409、borrow_buy.py:81–88。条件单例外已写对。

## 四项修正最终复核

已仅复核当前 tex 四项改动：172行净交易通知差异、258行组内/顶层as_of、272–276行SSE互斥分支与无快照回源、368行FX创建例外与LMSR注册顺序，均已准确解决原finding。最终 verdict：通过，无待修正项。本次未扩大审阅、未运行服务或测试；PDF编译与页面外观由主agent验证。
