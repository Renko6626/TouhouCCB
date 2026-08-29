"""BelieverTemplate（信念驱动散户）单测。

模型：长线信念 edge（belief − price）与短线动量 edge（trend_coef × window_change）
按 w_swing 加权混合；信念被从众项/观点冲击/风向注入演化。
所有测试关 skip_prob / shock_prob 保证确定性。
"""
from datetime import timedelta

from app.services.pve.market_view import parse_sentiment
from app.services.pve.templates import BelieverTemplate
from tests.pve_helpers import NOW, make_bot, make_trade, make_view

# 确定性基线：无随机跳过、无观点冲击、无从众
DET = dict(skip_prob=0.0, shock_prob=0.0, herd_coef=0.0)


def test_believer_buys_favorite_when_belief_above_price():
    """本命倾斜后信念高于市价 → 买入本命。"""
    bot = make_bot(BelieverTemplate, outcome_id=11, conviction=0.3, w_swing=0.0, **DET)
    a = BelieverTemplate().decide(bot, make_view(price_a=0.5, price_b=0.5))
    assert a is not None and a.side == "buy" and a.outcome_id == 11
    # 信念已初始化且归一化（LMSR 概率语义）
    beliefs = bot.memory["beliefs"]
    assert abs(sum(beliefs.values()) - 1.0) < 1e-6
    assert beliefs[11] > 0.5


def test_believer_idles_when_no_edge():
    """无本命倾斜、信念=市价、无动量 → edge 不过行动阈值，看盘不动。"""
    bot = make_bot(BelieverTemplate, outcome_id=11, conviction=0.0, w_swing=0.0, **DET)
    assert BelieverTemplate().decide(bot, make_view()) is None


def test_herd_belief_follows_price():
    """墙头草（herd_coef=1）：价格涨上去，信念整个被带过去 → 不再觉得便宜/贵。"""
    t = BelieverTemplate()
    bot = make_bot(BelieverTemplate, outcome_id=11, conviction=0.0, w_swing=0.0,
                   skip_prob=0.0, shock_prob=0.0, herd_coef=1.0)
    t.decide(bot, make_view(price_a=0.5, price_b=0.5))       # 初始化信念 = 0.5
    a = t.decide(bot, make_view(price_a=0.8, price_b=0.2))   # 价格大涨
    assert bot.memory["beliefs"][11] > 0.75                  # 信念真心跟上去了
    assert a is None                                          # 所以不觉得有便宜可捡


def test_stubborn_belief_buys_the_underpriced_side():
    """铁杆（herd_coef=0）：市场大涨也不改看法 → 反手买被打下去的另一边（抄底自涌现）。"""
    t = BelieverTemplate()
    bot = make_bot(BelieverTemplate, outcome_id=11, conviction=0.0, w_swing=0.0, **DET)
    t.decide(bot, make_view(price_a=0.5, price_b=0.5))
    a = t.decide(bot, make_view(price_a=0.8, price_b=0.2))
    assert a is not None and a.side == "buy" and a.outcome_id == 12


def test_flow_herding_follows_the_crowd():
    """跟风羊（herd_signal=flow）：价格没动但人群在净买入 → 信念上移 → 跟着买。"""
    t = BelieverTemplate()
    bot = make_bot(BelieverTemplate, outcome_id=11, conviction=0.0, w_swing=0.0,
                   skip_prob=0.0, shock_prob=0.0,
                   herd_signal="flow", herd_coef=1.0, flow_step=0.1, flow_scale=30.0)
    trades = [make_trade(5, outcome_id=11, side="buy", shares=50),
              make_trade(8, outcome_id=11, side="buy", shares=40)]
    t.decide(bot, make_view())                      # 初始化信念 = 0.5
    a = t.decide(bot, make_view(trades=trades))     # 人群净买 90 份
    assert bot.memory["beliefs"][11] > 0.5
    assert a is not None and a.side == "buy" and a.outcome_id == 11


def test_opinion_shock_perturbs_belief():
    """观点冲击（shock_prob=1）：什么信号都没有，信念也会自己漂——外生噪声源。"""
    t = BelieverTemplate()
    bot = make_bot(BelieverTemplate, outcome_id=11, conviction=0.0, w_swing=0.0,
                   skip_prob=0.0, herd_coef=0.0, shock_prob=1.0, shock_scale=0.2)
    t.decide(bot, make_view())
    t.decide(bot, make_view())
    assert abs(bot.memory["beliefs"][11] - 0.5) > 0.01


# 30 分钟前有一笔成交，成交后市场价 [0.40, 0.60]；现价 0.55/0.45
# → outcome11 窗口内 +0.15（涨），outcome12 −0.15（跌）
MOMENTUM_TRADES = [make_trade(29, outcome_id=11, post=[0.40, 0.60])]
MOMENTUM_VIEW = dict(price_a=0.55, price_b=0.45, trades=MOMENTUM_TRADES)


def test_swing_momentum_chases_the_riser():
    """纯波段动量派（w_swing=1, trend_coef>0）：信念无 edge，也会追窗口内涨得凶的。"""
    bot = make_bot(BelieverTemplate, outcome_id=11, conviction=0.0,
                   w_swing=1.0, trend_coef=1.0, **DET)
    a = BelieverTemplate().decide(bot, make_view(**MOMENTUM_VIEW))
    assert a is not None and a.side == "buy" and a.outcome_id == 11


def test_swing_reversal_buys_the_dip():
    """抄底派（trend_coef<0）：同样的行情，买的是被砸下去的那边等反弹。"""
    bot = make_bot(BelieverTemplate, outcome_id=11, conviction=0.0,
                   w_swing=1.0, trend_coef=-1.0, **DET)
    a = BelieverTemplate().decide(bot, make_view(**MOMENTUM_VIEW))
    assert a is not None and a.side == "buy" and a.outcome_id == 12


def test_swing_takes_profit():
    """波段客（w_swing=1）：浮盈过止盈线 → 落袋为安，卖掉部分或全部。"""
    bot = make_bot(BelieverTemplate, outcome_id=11, conviction=0.0, w_swing=1.0,
                   take_profit=0.15, holdings={11: (20, 5)}, **DET)  # 成本 0.25，现价 0.5 → +100%
    a = BelieverTemplate().decide(bot, make_view())
    assert a is not None and a.side == "sell" and a.outcome_id == 11
    assert 0 < float(a.shares) <= 20


def test_fan_never_takes_profit():
    """信仰党（w_swing=0）：同样翻倍的浮盈也不止盈——拿到结算。"""
    bot = make_bot(BelieverTemplate, outcome_id=11, conviction=0.0, w_swing=0.0,
                   take_profit=0.15, holdings={11: (20, 5)}, **DET)
    assert BelieverTemplate().decide(bot, make_view()) is None


def test_swing_cuts_loss_when_belief_gone():
    """波段客浮亏过割肉线、信念也不再支持 → 清仓离场。"""
    bot = make_bot(BelieverTemplate, outcome_id=11, conviction=0.0, w_swing=1.0,
                   stop_loss=0.25, holdings={11: (20, 16)}, **DET)  # 成本 0.8，现价 0.5 → −37.5%
    a = BelieverTemplate().decide(bot, make_view())
    assert a is not None and a.side == "sell" and a.outcome_id == 11
    assert float(a.shares) == 20


def test_sentiment_tilts_the_edge():
    """管理员风向注入：本无 edge 的机器人被风向带动买入目标 outcome。"""
    bot = make_bot(BelieverTemplate, outcome_id=11, conviction=0.0, w_swing=0.0,
                   sentiment_gain=1.0, **DET)
    a = BelieverTemplate().decide(bot, make_view(sentiment={11: 0.2}))
    assert a is not None and a.side == "buy" and a.outcome_id == 11


def test_parse_sentiment_valid_and_expiry_and_garbage():
    """site_config `pve_sentiment` 解析：正常解析 / 过期失效 / 垃圾输入不炸。"""
    future = (NOW + timedelta(hours=2)).isoformat()
    past = (NOW - timedelta(hours=2)).isoformat()
    raw = '{"tilts": {"42": 0.15, "43": -0.1}, "expires_at": "%s"}' % future
    assert parse_sentiment(raw, NOW) == {42: 0.15, 43: -0.1}
    assert parse_sentiment(raw.replace(future, past), NOW) == {}
    assert parse_sentiment('{"tilts": {"42": 0.15}}', NOW) == {42: 0.15}  # 无期限=一直生效
    assert parse_sentiment(None, NOW) == {}
    assert parse_sentiment("", NOW) == {}
    assert parse_sentiment("not json", NOW) == {}
    assert parse_sentiment('{"tilts": "oops"}', NOW) == {}
    assert parse_sentiment('{"tilts": {"x": 0.1}}', NOW) == {}


def test_persona_presets_registered_with_distinct_params():
    """薄子类预设已注册且参数点位符合人设——它们是同一模型的不同点位，不是独立脚本。"""
    from app.services.pve.templates import TEMPLATE_REGISTRY as R

    for name in ("believer", "fan", "swinger", "chaser", "sheep", "bottom_fisher"):
        assert name in R, f"预设 {name} 未注册"
    assert R["sheep"].default_params["herd_signal"] == "flow"          # 跟人群不跟价格
    assert R["bottom_fisher"].default_params["trend_coef"] < 0         # 等回调
    assert R["chaser"].default_params["trend_coef"] > 0                # 追动量
    assert R["fan"].default_params["w_swing"] < 0.2                    # 拿到结算
    assert R["fan"].default_params["conviction"] > R["swinger"].default_params["conviction"]
    assert R["swinger"].default_params["w_swing"] >= 0.7               # 靠短线吃饭


def test_persona_presets_decide_smoke():
    """全部预设在典型行情下正常出决策（Action 或 None），不抛异常。"""
    from app.services.pve.templates import TEMPLATE_REGISTRY as R

    for name in ("believer", "fan", "swinger", "chaser", "sheep", "bottom_fisher"):
        cls = R[name]
        for seed in range(5):
            bot = make_bot(cls, seed=seed, holdings={11: (20, 8)})
            a = cls().decide(bot, make_view(**MOMENTUM_VIEW))
            assert a is None or (a.side in ("buy", "sell") and float(a.shares) > 0)
