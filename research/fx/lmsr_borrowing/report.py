"""Build the exploratory readout from saved results, without new simulations."""
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from .policies import PRESETS
from .run import OUT, LABELS, VARIANTS


def run():
    rows = json.loads((OUT/'metrics.json').read_text())
    aggregate = json.loads((OUT/'aggregate.json').read_text())
    audit = json.loads((OUT/'audit.json').read_text())
    plt.rcParams.update({'font.family': 'Noto Sans CJK SC', 'axes.unicode_minus': False})
    fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    colors = ['#267288', '#b57337', '#7e609e']
    for variant, color in zip(VARIANTS, colors):
        with np.load(OUT/f'{variant}-normal-81/trace.npz') as trace:
            hours = np.arange(len(trace['prices'])) * 5 / 3600
            axes[0].plot(hours, trace['prices'], label=LABELS[variant], color=color, lw=1)
    axes[0].set(title='常态价格路径，种子81', ylabel='金圆券／外币')
    axes[0].legend(ncol=3)
    names = ['看多铁杆', '看空铁杆', '追涨', '波段', '抄底']
    with np.load(OUT/'belief_mix-normal-81/trace.npz') as trace:
        for i, (name, color) in enumerate(zip(names, ['#267288', '#b57337', '#7e609e', '#7c8248', '#c76787'])):
            axes[1].plot(hours, trace['account_foreign'][:, i+10]-1000,
                         label=name, color=color, lw=1)
    axes[1].axhline(0, color='#666666', lw=.6)
    axes[1].set(title='信念混编的五户净库存，相对初始1000外币', ylabel='外币', xlabel='小时')
    axes[1].legend(ncol=5)
    for ax in axes:
        ax.grid(alpha=.2)
        ax.set_xlim(0, hours[-1])
    fig.tight_layout()
    fig.savefig(OUT/'paths-and-personas.png', dpi=150)
    plt.close(fig)

    lines = ['# LMSR 策略借到 FX AMM：首轮探索', '',
        '2026-10-16。更值得继续探索的是信念人格：它在本轮增加了常态波动与行为差异，但削弱了冲击回归。中位数库存也能增加波动，不过没有明显减少交易，候选账户相对不交易的市值优势下降。两者均不自动替换已冻结配置。', '',
        '## 实验范围', '',
        '20户=10随机+5候选库存+5原短线；每tick5秒，每户5000金圆券+1000外币。每次只替换5位库存户，其他规则保留。三组×三个种子81/82/83×常态/冲击=18条，每条10000tick约13小时53分钟。无玩家、无借贷、不补钱、零手续费。参数在结果出现前固定，没有扫参。', '',
        '策略移植的是行为部件：中位数库存借鉴quant的volharvest；信念混编借鉴后端believer家族。FX用对数价格替代logit或主观概率，保留研究的±500净库存上限与50%调整。没有完整移植原策略的建仓、现金下注和退出规则，详见[实验说明](../README.md)。', '',
        '## 结果与取舍', '',
        '下表先在每条路径内计算指标，再对三个种子等权平均。波幅是完整、不重叠30分钟窗口的最高最低差除期初价中位数；持仓只统计5位原短线户已完成轮次。', '',
        '|常态指标|原库存|中位数库存|信念混编|', '|---|---:|---:|---:|']
    def table(scenario, specs):
        for label, key, fmt in specs:
            values = [next(r[key] for r in aggregate if r['variant']==v and r['scenario']==scenario) for v in VARIANTS]
            lines.append('|'+label+'|'+'|'.join(format(value, fmt) for value in values)+'|')
    table('normal', [('30分钟波幅中位数','range30','.2%'),
        ('全部成交／分钟','trades_per_minute','.2f'),
        ('5位候选合计成交／分钟','inventory_trades_per_minute','.2f'),
        ('候选每户期末净值相对不交易／金圆券','inventory_vs_hold','+.2f'),
        ('原短线每户已实现盈亏／金圆券','short_realized_per_account','+.2f'),
        ('原短线持仓中位数／分钟','holding_median_minutes','.2f')])
    lines += ['', '|冲击指标|原库存|中位数库存|信念混编|', '|---|---:|---:|---:|']
    table('shock', [('冲击后10分钟原始回吐','mean_retraced_10m','.1%'),
        ('候选每户期末净值相对不交易／金圆券','inventory_vs_hold','+.2f'),
        ('原短线每户已实现盈亏／金圆券','short_realized_per_account','+.2f')])
    lines += ['', '![三种子比较](comparison.png)', '',
        '图中蓝点为三条种子路径，橙线为均值，散点不是置信区间。冲击回吐的多个事件在同一路径内相关；每条9个可观察事件，不把27个事件视为独立样本。10分钟以最后60秒均价衡量，终点冲击无后续不计入。', '',
        '### 信念人格：更有区别，也更少回归', '',
        '信念混编常态30分钟波幅约2.07%，原配置约1.59%；原短线已完成轮次的持仓中位数由7.36分钟降至5.31分钟。但冲击后10分钟回吐由47.7%降至29.0%，且三个种子分别均低于原配置。若仍重视冲击后较快回归，这是一项明显代价。', '',
        '常态候选五户均值相对不交易为+122.51，与原库存+118.38接近，不能称为整体盈利升级：同种子差值一正两负。内部差别更有趣，三个种子中追涨户均落后于不交易、抄底户均领先；这符合让玩家能理解和针对不同对手的探索方向，但尚未放入玩家，不能说玩家已经能稳定利用。', '',
        '|信念人格|常态期末净值相对不交易，三个种子均值／金圆券|三个种子的范围|', '|---|---:|---:|']
    belief = [r for r in rows if r['variant']=='belief_mix' and r['scenario']=='normal']
    for i, name in enumerate(names):
        values = [r['inventory_vs_hold_individual'][i] for r in belief]
        lines.append(f'|{name}|{np.mean(values):+.2f}|{min(values):+.2f} ～ {max(values):+.2f}|')
    lines += ['', '这些数值包含未实现损益和初始外币重估后的对照，并非已实现交易盈利；人格也各只有一户，不能据此得出人格类别的普遍收益排名。看空铁杆的锚点可以逐步变化，价格下跌后也会转为净买入，并非永久只卖。', '',
        '### 中位数库存：不适合直接替代当前库存户', '',
        '最初假设是用鲁棒中心和偏离门槛减少细小波动的追逐。实际5位候选每分钟成交2.37笔，原库存2.38笔，几乎不变；进出门槛后回到中性库存仍会交易，且原有执行会成交很小的余额调整。本轮没有测出明显的成交次数节省。', '',
        '候选相对不交易的每户净值差从+118.38降到+32.38，三个种子都下降；冲击回吐也变弱。它可能适合少量较迟钝的背景户，但这组参数不支持将其当作更好的回归主力。中位数、门槛及尺度一起改变，不能单独归因于其中某个部件，也不能断言volharvest本身无效。', '',
        '## 可继续考虑的方向', '',
        '优先保留信念模型作为少量人格：例如多空铁杆、追涨和抄底，配合现有库存回归力量，而非让所有库存户都变成人格户。这是基于本轮的下一步假设，尚未测“只替换一两户”的效果，不作为已验证配置。旧LMSR的跟风羊需要真实净流量信号，活动模式的慢改信需要真人成交，本轮未测。', '',
        '固定网格和定投死拿没有进入首轮：前者增加了固定价格区间选择，后者长期单向消耗现金，较难回答本轮要解决的对手差异问题。它们可以成为特定活动角色，但目前证据不足以优先。', '',
        '![价格与人格库存](paths-and-personas.png)', '',
        '图用第一个预定种子81展示路径及库存，不按结果挑选。三种策略价格是各自内生形成的，不是共同外生行情。', '',
        '## 验证和边界', '',
        f'运行内核的18条路径检查通过：两币守恒、真实钱包净值、余额非负、成交计数、候选钱包变化及目标上限；六条基线逐字段精确复现冻结轨迹；同种子常态/冲击首事件前轨迹相同。{audit["protected_files_unchanged"]}个已有研究文件的哈希保持不变。', '',
        '已有检查命令：`python -m unittest research.fx.tests.test_execution research.fx.tests.test_entry_policies research.fx.tests.test_population_physical_metrics research.fx.tests.test_episodes`，10项通过。未新增永久测试；已查看生成图片。', '',
        '仅三个已使用过的种子，不提供置信区间或显著性结论；初始锚点、信号尺度和门槛是这轮研究选择，未做稳健性扫参。无真人、正手续费、持续单边行情、24小时或线上性能验证。常态短线仍亏损，不能把更快退出当盈利证明。候选收益按各自路径的期末价与不交易比较，未进行共同外生价格下的收益检验，也没有剔除候选自身价格影响。', '',
        '数据：[逐种子指标](metrics.json)、[聚合指标](aggregate.json)、[执行参数和哈希](manifest.json)、[执行检查](audit.json)。每条路径保留完整npz、钱包、成交记录和短线轮次。报告复现：`MPLCONFIGDIR=/tmp/sunyunbo/matplotlib python -m research.fx.lmsr_borrowing.report`。', '']
    (OUT/'report.md').write_text('\n'.join(lines))
    provenance = dict(metrics_sha256=hashlib.sha256((OUT/'metrics.json').read_bytes()).hexdigest(),
        report_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    (OUT/'report_provenance.json').write_text(json.dumps(provenance, indent=2)+'\n')
    print(OUT/'report.md')


if __name__ == '__main__':
    run()
