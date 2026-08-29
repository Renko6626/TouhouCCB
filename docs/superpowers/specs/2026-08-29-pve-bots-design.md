# PvE 模式：机器人账户池设计（2026-08-29）

## 1. 背景与目标

市场需要活跃度与对手盘。引入一批由系统维护的机器人小号在市场中真实交易：

- **造流动性/活跃度**：让价格动起来，玩家随时有对手盘（`liquidity` 等做市型模板是主力）。
- **当"怪"给玩家打**：机器人是玩家的博弈对象，从机器人身上赢钱是玩法的一部分。

定位决定风格：**多数机器人是"散户思维"**（追涨杀跌、跟风、抄底被套……有缺陷的人类），少数跑量化逻辑。

对玩家**半明牌**：官方公告市场中存在机器人，不逐一标注；部分机器人采用统一、有辨识度的命名（"NPC 军团"款），另一部分低调混在人群中。

经济上机器人与真人**无差别**：真实 User 行、真实资金（ledger 记账）、真实走 LMSR 成交。**进排行榜**（把 NPC 从榜上打下去是玩法）。亏光即死（停止交易），管理员可通过 ledger 注资手动复活。

规模：**中编制 50~150 个**。人格按模板+随机扰动生成，批量调度。

## 2. 非目标（YAGNI）

- 不做机器人决策流水的持久化表——内存环形缓冲即可（先例：`history_ring.py`），重启即弃。
- 不在 `bot_profile` 里冗余盈亏/累计注资——从 `LedgerEntry` 与持仓计算，不搞第二份账。
- 不做引擎状态持久化——重启后机器人"集体失忆"重新开始；量化模板从真实持仓 reconcile 重建（同 quant 思路）。
- 不做图形化模板调参界面——一期管理页直接编辑 params JSON 表单。
- 不改造 `thccb-quant` 为多账号框架（token 养不动、走公网限速）；只移植其策略逻辑作参考。
- 不新建资金通道——初始注资、复活全部走现有 ledger 调账流程。

## 3. 架构总览

**后端进程内 PvE 引擎 + 回环 HTTP 下单**（已比较过独立 worker 进程与扩展 thccb-quant 两个方案，均否决）：

- 引擎位于 `backend/app/services/pve/`，以 lifespan 注册的第 4 个后台调度器运行（与 loan / liquidation / bot_detection 三个现有调度器同构）。后端为单 uvicorn worker，无多 worker 重复调度问题。
- 机器人下单对 `127.0.0.1:8004` 自己的 `/api/v1/market/{buy,sell,quote}` 发**回环 HTTP** 请求，token 由后端为机器人 User 内部签发 JWT。**不动 `market.py` 买卖热路径一行代码**——滑点检查、ledger、audit、SSE 广播、K 线与真人完全同路径。
- 账户池：新表 `bot_profile` + `User.is_bot` 列；管理端 `admin_pve.py` + 前端管理页。

选型理由：完全复用现有调度器模式与真人交易路径，不碰红线文件，改动集中于新增文件 + `main.py` 挂调度器 + alembic 迁移；引擎与 Web 同进程的风险由批量调度与全局护栏兜底。

## 4. 数据模型

### 4.1 `User.is_bot`（新列）

`bool`，默认 `false`，加索引。`models/base.py` 属高敏感文件且动 schema，**必须走 `alembic revision --autogenerate` 流程**。

用途：`bot_detection` 扫描排除、管理端筛选、以及任何需要"只看真人"的查询低成本过滤。

排行榜与财富统计是否包含机器人做成**两个独立的 site_config 开关**（管理页可切），默认均**包含**（与"机器人参与排名是玩法"的定位一致）。排行榜查询在 `market.py`（高敏感文件的只读低风险区），实现时单独说明改动。

### 4.2 `bot_profile`（新表）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | PK | |
| `user_id` | FK → user.id，unique | 一机器人一行 |
| `template` | str | 人格模板名（`liquidity` / `grid` / `hodler` / 二期散户系……） |
| `params` | JSON | 覆盖模板默认值的个体参数，生成时随机扰动后落库 |
| `status` | str | `active` / `paused`（管理员暂停）/ `dead`（亏光自动置） |
| `market_scope` | JSON, nullable | `null`=全市场；`[market_id…]`=只在指定市场活动（暖场场景） |
| `created_at` / `last_trade_at` | datetime(tz) | |

机器人 User 行：`casdoor_id=NULL`、`email=NULL`、`is_bot=true`，用户名由生成器产出（辨识度款/低调款两种命名风格）。

新增 ORM 关系（如有）遵守项目守则：默认 `lazy="raise_on_sql"`，查询显式 `select`/`selectinload`。

## 5. 人格模板与策略层

### 5.1 接口

`services/pve/templates/` 下每模板一类：

```python
class BotTemplate:
    name: str
    default_params: dict
    async def decide(self, bot: BotRuntime, view: MarketView) -> Action | None
```

- **`MarketView`**：引擎每轮统一拉一次的市场快照（各 outcome 现价、1h/24h 涨跌、最近成交流水），本轮被唤醒的机器人共享，避免百人各查一遍库。
- **`BotRuntime`**：引擎内存中的个体状态（成本记忆、上次动作、情绪/冷却、下次行动时间等）。
- **`Action`**：buy/sell + outcome + 金额/份额，由引擎护栏过滤后执行。

### 5.2 模板清单

**一期**（验证底座）：

| 模板 | 类型 | 行为 |
|---|---|---|
| `liquidity` | 量化 | volharvest 简化版：围绕底仓做库存平衡，是"造流动性"目标的主力 |
| `grid` | 量化 | 网格：区间内低买高卖（移植自 thccb-quant） |
| `hodler` | 散户(最简) | 认准一个 outcome 定期小额买入，几乎不卖；兼作冒烟测试 |

**二期**（散户行为化模板，底座稳定后加入）：

| 模板 | 行为 |
|---|---|
| `chaser` 追涨杀跌 | 买最近涨得凶的，跌破自身成本一定比例恐慌割肉 |
| `sheep` 跟风 | 看最近成交流水，别人买啥跟着买啥 |
| `bottom_fisher` 抄底侠 | 大跌进场接飞刀，被套死扛，扛不住割在低点 |
| `gambler` 赌徒 | 低频大注，专挑低价冷门 outcome 梭 |
| `meanrev` | 量化：均值回归（移植自 thccb-quant） |
| `degen` 杠杆赌徒 | 走借贷接口上杠杆（见 §8 杠杆口子） |

### 5.3 共享人格维度（params，生成时随机扰动）

- **下单规模**：现金比例 + 单笔上下限。
- **选市场偏好**：热门优先 vs 固定偏好，受 `market_scope` 约束。
- **决策噪声**：概率性跳过、金额抖动——避免机器人步调一致露馅。
- **注意力模型**（见下）。

### 5.4 注意力模型

- **看盘间隔 `check_interval`**：多久看一轮盘。量化模板分钟级；散户十几分钟到几小时。
- **作息窗口 `active_hours`**：每天哪些时段醒着。生成时从典型作息中抽取：上班摸鱼型（工作日白天碎片时段）、晚间型（20:00–24:00）、夜猫子、全天散漫型；窗口边界加个体随机偏移，避免整点集体上线。窗口外不参与调度。
- **行情推送 `alert_sensitivity`**：模拟"大涨大跌通知炸出吃瓜群众"——主循环发现某市场短时涨跌幅超过该机器人敏感阈值时，无视作息与看盘间隔将其提前唤醒，带 1~10 分钟随机延迟（陆续点开通知，非同秒涌入）；触发后有冷却期，且按概率响应。量化模板近似实时响应。

  副作用即目标：真人大单 → 波动 → 炸出一批机器人 → 市场热闹；全局护栏保证羊群不踩踏成雪崩。

### 5.5 引擎级安全护栏（不信任任何模板，统一强制）

- 单笔金额上限；
- 单机器人每日成交额上限；
- 全 PvE 系统每分钟总下单数上限；
- 下单前经 `/quote` 预估，滑点超阈值放弃。

模板写出 bug 的最坏结果是不交易，而非打爆市场。

### 5.6 死亡判定

总资产（cash + 持仓 **LCV 清算口径**，见 `docs/holdings-value-semantics.md`）低于阈值（引擎全局配置，默认取"不足以按该机器人参数下出一笔最小单"的水位）→ `status=dead`，移出调度；复活 = 管理员 ledger 注资 + status 拨回 `active`。

## 6. 调度与生命周期

`services/pve/scheduler.py`，lifespan 注册 `start_pve_scheduler()` / `stop_pve_scheduler()`：

1. 主循环 15~30 秒一轮；每轮开头查 site_config `pve_enabled`（急停闸），关着就睡。
2. 拉一次 `MarketView` 快照。
3. 从 `active` 机器人中筛出"到点该醒"的批次（下次行动时间由看盘间隔 + 作息 + 抖动算出，存内存；行情推送可提前唤醒）。
4. 批次内逐个 `decide()`，产出 Action 经护栏过滤后**串行**回环下单（全局每分钟上限天然限流；串行避免自家机器人互打滑点）。
5. 决策（含跳过原因）写入该机器人的内存环形缓冲。
6. 对被唤醒机器人顺便做死亡检查。

**身份与登录**：机器人 User 由管理端批量创建。引擎不走 HTTP 登录——直接用后端 JWT 签发逻辑为机器人造短期 access token（仅存进程内存，权限与普通用户相同）。SSO 按 `casdoor_id` 匹配，机器人为 NULL 永不命中——**机器人账号无法被任何人登录**。

**进程重启**：内存状态全弃，见 §2 非目标。

## 7. 管理面

### 7.1 后端 `api/v1/admin_pve.py`（全部 `current_superuser`，挂 `/admin` 限速带）

| 端点 | 功能 |
|---|---|
| `GET /admin/pve/bots` | 账户池列表：用户名、模板、状态、现金、持仓市值、今日成交额、最近动作时间 |
| `POST /admin/pve/bots/generate` | 批量生成：数量 + 模板配比 + 命名风格 + 初始资金（走 ledger） |
| `PATCH /admin/pve/bots/{id}` | 个体干预：暂停/恢复、换模板、改 params、改 market_scope |
| `POST /admin/pve/bots/{id}/fund` | 注资/复活（ledger 调账 + status 拨回 active） |
| `GET /admin/pve/bots/{id}/log` | 内存决策环形缓冲（含跳过原因） |
| `GET/PUT /admin/pve/config` | 全局：`pve_enabled` 急停闸、全局每分钟下单上限、排行榜/财富统计含机器人两开关 |

### 7.2 前端

`/admin` 区新增 PvE 管理页（安全区 `pages/` + `components/`，守 `docs/style.md` 工业风设计系统）：顶部全局开关 + 总览数字（在编/存活/死亡数、今日总成交），账户池表格 + 行内操作，点开单个机器人看决策流水。模板调参一期为 params JSON 编辑表单。

## 8. 反作弊与安全边界

- **`bot_detection` 扫描排除**：行为扫描取样查询加 `User.is_bot == false`——否则机器人每 30 分钟被自己的反作弊刷屏 `bot_suspicion`。
- **anti-bot L2（活动模式 HMAC）**：引擎进程内自己算 `X-Client-Token`（密钥本在后端手里），不往 `quant_whitelist_user_ids` CSV 塞 id。
- **JWT**：机器人 token 短期、仅进程内存、仅回环使用、非超管。
- **旁路 nginx 限速的补偿**：回环直连 uvicorn 绕过 nginx 市场限速，由引擎"全局每分钟上限 + 串行下单"接管该层保护。
- **借贷（杠杆口子）**：借贷路径**不封死**——模板可以经回环调用借款接口上杠杆，借了就正常进利息结算与强平清算（被公开强平也是节目效果）。一期所有模板不借钱（`debt` 恒 0，不进 liquidation sweep）；二期 `degen` 模板启用，届时引擎护栏增加单机器人负债上限参数。
- **急停**：`pve_enabled` 每轮开头检查，关闸后最迟一个周期（≤30s）全体停手；调度器随 lifespan 优雅停机。

## 9. 测试与上线路径

**单元测试**：模板 `decide()` 对 MarketView 夹具的行为断言；注意力模型时间计算（作息窗口/看盘间隔/推送唤醒+冷却）；死亡判定；护栏拦截（超额单、超频）。

**集成测试**：pytest 起 test app，机器人经回环 HTTP 真实走一遍 buy/sell（验证 JWT 签发、anti-bot 通过、ledger/transaction 落库）；管理端 API CRUD + 权限；alembic 迁移可升可降。

**前端**：`type-check` + `lint`；管理页浏览器实测（空池、全暂停、机器人死亡等边界态）。

**上线路径**（生产站在跑）：

1. `pve_enabled` 默认 `false` 合入部署；
2. 生产手动生成 5 个机器人小编制，`market_scope` 圈定一个测试市场；
3. 观察一两天（决策日志、资金流水、性能）；
4. 逐步扩编制、放开市场范围。

**分支**：非小修补，`feat/2026-08-29-pve-bots`；涉及 schema（`User.is_bot` + `bot_profile`），合并前按护栏停下与用户确认。

## 10. 分期

- **一期（本 spec 的实施范围）**：schema 迁移、引擎与调度器、注意力模型、安全护栏、回环下单与 JWT 签发、`liquidity`/`grid`/`hodler` 三模板、管理端 API 与前端页、反作弊排除、两个统计口径开关。
- **二期**：散户行为化模板（`chaser`/`sheep`/`bottom_fisher`/`gambler`）、`meanrev`、`degen` 杠杆赌徒（含负债上限护栏）、按运营反馈迭代人格参数。
