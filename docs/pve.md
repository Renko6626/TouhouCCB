# PvE 机器人：写一个新的行为模板

面向「要给机器人加新算法」的你。架构背景见
`docs/superpowers/specs/2026-08-29-pve-bots-design.md`，前半部分讲 LMSR 模板，FX 管理和操作流程见文末。

## TL;DR

在 `backend/app/services/pve/templates.py` 里加一个类，**写完即注册完**（不用改注册表、
不用改引擎、不用改管理端——管理页的模板下拉自动出现它）：

```python
class MyChaserTemplate(BotTemplate):
    """追涨杀跌：买最近涨得凶的，跌破自己成本一定比例恐慌割肉。
    （教学示例——真实的追涨杀跌人格请直接用 believer 家族的 chaser 预设）"""

    name = "my_chaser"                   # 唯一名；重名会在 import 时直接报错
    default_params = {
        "lookback_min": 30,              # 动量观察窗
        "momentum_threshold": 0.05,      # 涨幅超过它才追
        "panic_drawdown": 0.15,          # 现价低于成本 15% → 割肉
        "buy_cny_min": 5.0,
        "buy_cny_max": 20.0,
        # 注意力（可选，不写用 attention.ATTENTION_DEFAULTS）
        "check_interval_sec": 1800,
        "active_preset": "evening",
    }

    def decide(self, bot: BotState, view: MarketView) -> Optional[Action]:
        p, rng = bot.params, bot.rng
        # 1) 先看手里的仓：跌破成本线就恐慌
        for ov in bot.eligible_outcomes(view):
            held, cost = float(bot.holding(ov.outcome_id)), bot.avg_cost(ov.outcome_id)
            if held > 1 and cost and ov.price < cost * (1 - p["panic_drawdown"]):
                return Action("sell", ov.outcome_id, q_shares(held), "chaser 恐慌割肉")
        # 2) 找最近涨得最凶的追
        hot = max(bot.eligible_outcomes(view),
                  key=lambda ov: view.window_change(ov.outcome_id, p["lookback_min"]),
                  default=None)
        if hot is None or view.window_change(hot.outcome_id, p["lookback_min"]) < p["momentum_threshold"]:
            return None
        cny = min(rng.uniform(p["buy_cny_min"], p["buy_cny_max"]), float(bot.cash) * 0.9)
        if cny < 1:
            return None
        return Action("buy", hot.outcome_id, q_shares(cny / max(hot.price, 0.01)), "chaser 追涨")
```

配一个单测（夹具直接 import，别复制粘贴）：

```python
# tests/test_pve_my_chaser.py
from app.services.pve.templates import MyChaserTemplate
from tests.pve_helpers import make_bot, make_trade, make_view

def test_my_chaser_panic_sells_below_cost():
    bot = make_bot(MyChaserTemplate, holdings={11: (20, 10)})   # 20 份成本 10 → 均价 0.5
    a = MyChaserTemplate().decide(bot, make_view(price_a=0.35)) # 跌 30% > panic 15%
    assert a is not None and a.side == "sell"
```

跑 `pytest tests/test_pve_my_chaser.py -x`，绿了就完事。上线后在 `/admin/pve` 生成即可。

## decide() 合同

- **同步纯函数**：不碰 DB、不发请求、不 sleep。所有 IO 由 engine 承担。
- **输入只有两个**：`BotState`（这个机器人）+ `MarketView`（本轮共享市场快照）。
- **返回 `Action` 或 `None`**：None = 看了盘不动（正常且常见，散户人味的一部分）。
- **抛异常是安全的**：engine 捕获、记进该机器人的决策日志、照常重排下次唤醒——
  但最好别依赖这一点。
- **随机必须用 `bot.rng`**（按 profile_id 固定种子），别用全局 `random`——
  同一个机器人重放同样输入会得到同样决策，可调试性靠这个。

## 输入：BotState

| 成员 | 说明 |
|---|---|
| `bot.cash` | `Decimal`，唤醒时从 DB 现读的现金 |
| `bot.holding(oid)` | 该 outcome 持仓份额（`Decimal`，无仓=0） |
| `bot.avg_cost(oid)` | 该仓位均价成本（float），无仓返回 `None`——止盈/割肉判断用 |
| `bot.eligible_outcomes(view)` | market_scope 过滤后的可交易 outcome 列表 |
| `bot.params` | 模板默认值 ← 注意力默认值 ← 落库个体参数，三层合并后的 dict |
| `bot.memory` | 模板私有 dict，进程存活期内跨 tick 保留（重启/换模板即清空）——网格线、主场、情绪状态放这 |
| `bot.rng` | `random.Random`，个体固定种子 |

持仓/现金**每次唤醒都从 DB 现读**，不要自己在 memory 里记账本——会漂移。

## 输入：MarketView

只包含 `status=trading` 的市场；`trades` 为近 60 分钟成交（时间降序，上限 600 笔）。

| 成员 | 说明 |
|---|---|
| `view.outcomes[oid]` | `OutcomeView(outcome_id, market_id, label, price)`，price 为 LMSR 瞬时价 |
| `view.window_change(oid, minutes)` | 现价 − 窗口起点价（窗口内无成交=0）——动量/抄底信号 |
| `view.net_flow(oid, minutes)` | 窗口内 Σbuy − Σsell 份额，>0=人群在买——跟风信号 |
| `view.trade_count(oid, minutes)` | 窗口内成交笔数——活跃度 |
| `view.max_abs_change(minutes, oids)` | 涨跌幅最大的 outcome——引擎的行情推送也用它 |
| `view.trades` | 原始 `TradeBrief` 列表，以上不够用时自己算 |

## 输出：Action 与引擎护栏

`Action(side, outcome_id, shares, reason)`——shares 用 `q_shares(浮点)` 构造（2dp），
reason 写人话，它会原样出现在管理页决策日志里。

**你不用操心的**（engine 统一强制，模板写出 bug 最多是不交易）：
全局每分钟下单上限、单笔金额上限、单机器人日成交额上限、滑点超限放弃、
真实下单与滑点保护、死亡判定。

**你要粗略操心的**：买入前自己看下 `bot.cash` 够不够（护栏兜底会拦，但会在日志里
留一条「现金不足」的 error 味道记录）；卖出别超过 `bot.holding()`。

## 人格参数与注意力

`default_params` 里除了模板私有键，还可以放注意力键（`attention.ATTENTION_DEFAULTS`
是兜底）：`check_interval_sec`（看盘间隔）、`active_preset`（作息：
`always/worker/evening/owl/loose`）、`alert_threshold`/`alert_prob`/`alert_cooldown_sec`
（行情推送唤醒）。量化型给 `always`+分钟级间隔；散户给小时级+具体作息。

**时间涨落**（防「每 N 分钟冲进来一批单」的机器节律，模板作者不用管，引擎统一做）：
看盘间隔除常规 ×U(0.6,1.6) 抖动外带重尾（5% 沉迷刷盘 ×0.15~0.35、10% 忙别的去了
×2~4）；全局还有一个 OU 慢波「活跃度潮汐」（site_config `pve_activity_wave`，0=关，
默认 0.7）——活跃期全体看盘变勤、冷清期变懒，成交到达率自然起伏；积压削峰的每 tick
放行数也在 cap/2~cap 间随机。当前活跃度可在管理页引擎快照（`engine.activity`）观测。

**生成时扰动**（`service.spawn_params`）：数值参数自动 ×U(0.75,1.3)，散户随机抽作息、
注意力参数重新采样——同模板的个体天然不同。语义非"强度"的键（`outcome_id`、
`price_low/high`、`active_preset`）不扰动。全量参数落库 `bot_profile.params`，
管理页可看可改，改了下一 tick 生效。

## believer 家族：信念驱动散户（二期）

散户人格不是一堆独立脚本，而是 `BelieverTemplate` 这一个模型的参数点位：每个机器人
内心维护一份主观概率（「我觉得它会赢」），交易动机 = 长线信念 edge（belief − price）
与短线动量 edge（trend_coef × window_change）按 `w_swing` 加权混合。信念每次看盘被
三样东西演化：从众项（`herd_coef`，price 模式跟价格信 / flow 模式跟 `net_flow` 人群信、
负值=逆势党）、观点冲击（`shock_prob/scale`，外生噪声源）、风向注入（见下）。
短线退出（止盈落袋 / 割肉）按 `w_swing` 概率执行——波段客勤快、信仰党拿到结算。

已注册预设（薄子类，只改参数分布中心）：`fan` 铁杆粉 / `swinger` 波段客 /
`chaser` 追涨杀跌 / `sheep` 跟风羊 / `bottom_fisher` 抄底侠 / `believer` 通用中间型。
加新人格优先考虑「加一个参数点位」而不是新写 decide()——没有固定行为阈值，
玩家无法用指纹试探识别。测试见 `tests/test_pve_believer.py`。

### 风向注入（运营造事件行情）

site_config 键 `pve_sentiment`（`/admin/pve` 全局配置里直接编辑），格式：

```json
{"tilts": {"42": 0.15}, "expires_at": "2026-08-30T12:00:00+00:00"}
```

tilt 单位是价格空间（可为负），believer 系把 `sentiment_gain × tilt` 加进对应
outcome 的长线 edge——相当于给整群散户吹一阵「42 号利好」的风；`expires_at`
可省略（一直生效），到期/删除键/格式写错都安全失效（解析函数
`market_view.parse_sentiment` 保证不炸 tick）。

## 还没做的

`degen` 杠杆赌徒：要经回环调借贷接口，上线前必须先给 engine 加单机器人负债
上限护栏（现在还没有），并确认利息结算/强平对机器人的语义。

## 线下活动短线模式

适合 8–10 小时的活动。管理页「批量生成」选择「活动短线」，默认推荐混编 50 个、
每个初始资金 500：追涨 12、跟风 12、波段 12、抄底 9、铁杆粉 5。填写三个正在交易
的市场 id 后，账户按 17/17/16 分配到固定主场。生成仍走原有资金 ledger。

常规每 3–5 分钟看盘，活动期间全天活跃；大行情可通过原有推送偶尔提前唤醒。
信念、从众、动量、记忆继续使用 believer 家族算法，活动模式缩短观察/退出周期，
并用更小的 `edge_scale` 放大有信号时的仓位变化，弱信号仍可不交易。
`activity_mode=event` 的常规间隔固定随机在 180–300 秒，`check_interval_sec` 不覆盖此范围。

多数活动人格会逐步改信：同一选项近 15 分钟至少三名不同真人买入、真人净流入为正，
且价格高于旧锚点至少 0.015；此证据持续观察 10 分钟后，每次看盘把长期锚点向现价
更新 12%。信号中断就重计观察期。官方 bot 与未知身份成交不用于长期改信。
推荐编制中的五个铁杆粉不更新长期锚点，也关闭随机观点冲击；长期模式默认 `anchor_adapt_rate=0`。
改信证据独立读取完整真人窗口，避免行情快照的最新 600 笔上限漏掉较早的卖单。
这是依据成交的适应机制，不会读取或判断现实世界新闻。

活动启用步骤：生成活动账户；核对 active 编制；将全局 `pve_tick_interval_sec` 设为 5，
保存后重启后端；最后打开 PvE 总闸。调度间隔只在启动时读取，改配置后必须重启。
默认每分钟最多 30 单，单笔/日成交额默认不限，滑点保护仍生效。
页面显示的单机器人日志与近一分钟下单数可用于观察运行状态。

100 名真人和 50 个 bot、每人初始 500、三个市场的开场建议：2 选项 `b=10000`，
3 选项 `b≈13333`，4 选项 `b=15000`。这是让开盘等概率时的资金冲击接近的起点，
不是最优深度保证。一笔 500 约推动 2.4–2.5 个百分点；同向净买入 2500 约推动
11–12 个百分点。半深度更激烈，1.5 倍深度更平缓；实际走势还取决于真人如何分配资金。

离线复现（不会创建线上账户或访问数据库）：

```bash
cd backend
APP_ENV=development python3 -m scripts.simulate_pve_event --hours 10 \
  --depths 10000 13333 15000 --outcomes 2 3 4 --seed 20260928 \
  --output /tmp/pve-event-simulation.json
```

比较长期、仅加快看盘、完整活动参数三个模式。输出包括成交间隔、单笔价格冲击、
15 分钟最大端点变化、一分钟采样的价格变化/对数收益标准差，以及逐小时成交数。
采样的波动率取每市场所有选项中的最大值，单笔冲击取该笔影响最大的选项。
模拟仅含 bot，卖出手续费固定 0.5%，没有真人、网络延迟、借贷或结算；选项数与深度
均是场景假设，结果不能作为现场行情保证。

## 验证与观测

- 单测：`pytest tests/test_pve_*.py`；集成参考 `tests/test_pve_engine.py`
  （ASGITransport 回环，机器人真下单）。
- 线上观测：`/admin/pve` 单机器人「日志」= 决策环形缓冲（含跳过原因、护栏拦截、
  行情推送），重启即弃；真实成交查 Transaction / 交易记录页。
- 全局急停：管理页引擎开关（`pve_enabled`），关闸后最迟一个 tick（默认 20s）全体停手。

## FX 机器人管理与操作

入口 `/admin/pve` 默认跳转 `/admin/pve/lmsr`，保留原 LMSR 管理功能。
`/admin/pve/fx` 是独立 FX 子页面。每个 FX 账户绑定一个交易对和一个策略，
不会同时由 LMSR 引擎调度。支持随机、库存调节和短线轮次三种冻结策略。

创建时可自定义人数、初始现金和初始外币，金额最多六位小数。5000 现金、1000 外币
仅是可修改的研究预设。库存基准始终等于该户实际初始外币量 F，创建后只读；
例如发放 600 外币，库存策略就以 600 为基准。现金走既有 ledger，初始外币有独立
发放审计，不伪造成交、不改变池价。创建的新账户默认 paused。

启用前核对实际资产、策略和交易对配置，再恢复目标账户，同时打开 FX PvE 总开关和
对应交易对开关；现有 FX 交易开关及交易对状态仍须允许交易。新配置默认关闭，
不会自动改变已有排行榜设置。账户停用、paused 或关闭开关会阻止后续策略成交。

FX tick 固定为 5 秒，是采样和调度周期。每户是否到达由到达过程决定，不保证每五秒
交易。短线个人噪声每 tick 预计算，估值、入场和退出只在本人到达时执行，没有超时退出。
负向轮次只卖自有外币再买回，不借钱、不借币。每笔使用真实现货报价和交易对手续费，
并受钱包、可用现金与池规模限制，研究中的零费用结果不能当作线上收益保证。

列表里的 MTM 是外币钱包按边际价格估值，清算值是全仓卖出预计获得的净现金，
两者都不含已有现金；总资产分别加一次现金。实际成交金额和收益以真实钱包、成交与
手续费为准。短线成交和剩余数量、成本同步提交；失败订单不扣减轮次数量。

退出账户按以下顺序操作：

1. 暂停账户，保留尚未完成的轮次。
2. 在 paused 状态手动清仓，核对 min_out。未完成的负向轮次会买回，其他外币仓位卖出。
   单次仍受池规模和资金限制，可能只成交一部分；查看实际余额和轮次后，明确再次点击
   清仓才能继续，系统不会自动重试。轮次结束后如仍有初始外币，也需继续清仓。
3. 轮次结束、外币钱包为空且没有现金或外币债务后退休。已有成交的账户保留身份和历史，
   剩余现金按原生命周期规则回收；符合条件的空白账户可直接删除。

重启会保留数据库中的短线轮次和真实资产；收盘窗口与热度只在内存，重启后重新预热，
热度从零开始。不读取 10 秒 K 线冒充 5 秒历史，不补跑停机期间的 tick 或订单。
决策日志也是内存环形缓冲，重启即丢；真实成交和发放记录查交易记录与审计。

“排行榜是否包含机器人”复用 `leaderboard_include_bots`，“财富统计是否包含机器人”
复用 `wealth_stats_include_bots`，作用于全部机器人，包含 LMSR 和 FX。隐藏只影响
这些展示统计，不删除公开成交、不隐藏池价格；排行榜变化可能受现有短缓存影响。

FX 管理 API 全部要求超管，前缀 `/api/v1/admin/pve/fx`：

| 方法与相对路径 | 用途 |
|---|---|
| GET `/overview` | 策略字段、冻结预设、交易对及引擎状态 |
| GET / PUT `/config` | 总开关与按交易对配置 |
| GET `/bots` | 账户、真实余额、估值及轮次 |
| POST `/bots/generate` | 自定义初始资产创建 paused 账户 |
| PATCH `/bots/{id}` | 启停、允许的模板和参数修改 |
| POST `/bots/batch` | 选中账户批量启停或改参 |
| POST `/bots/{id}/fund` | 现金注资 |
| POST `/bots/{id}/liquidate` | paused 账户一次真实清仓，带 min_out |
| DELETE `/bots/{id}` | 满足条件后删除或退休 |
| GET `/bots/{id}/log` | 临时决策日志 |

有未完成轮次时不能换模板或改短线参数；模板切换及短线改参须暂停且无未完成轮次。
库存、随机策略参数可在下一 tick 生效。只读基准和交易对绑定不能通过改参变更。

本次交付只包含实现、迁移文件和隔离测试结果，尚未部署或运行生产迁移，也未创建真实
机器人、实际发放资产或打开运行开关。上线操作须另行授权。
