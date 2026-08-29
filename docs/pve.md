# PvE 机器人：写一个新的行为模板

面向「要给机器人加新算法」的你。架构背景见
`docs/superpowers/specs/2026-08-29-pve-bots-design.md`，本文只讲怎么写模板。

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

## 验证与观测

- 单测：`pytest tests/test_pve_*.py`；集成参考 `tests/test_pve_engine.py`
  （ASGITransport 回环，机器人真下单）。
- 线上观测：`/admin/pve` 单机器人「日志」= 决策环形缓冲（含跳过原因、护栏拦截、
  行情推送），重启即弃；真实成交查 Transaction / 交易记录页。
- 全局急停：管理页引擎开关（`pve_enabled`），关闸后最迟一个 tick（默认 20s）全体停手。
