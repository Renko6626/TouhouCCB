"""Supplement saved repeated-shock paths with diagnostics; never reruns the model."""
import hashlib
import json
from pathlib import Path

import numpy as np

from research.fx.common.diagnostics import acf, describe
from .run import HERE, OUTPUT


BANDS = [(1, 1), (2, 10), (11, 30), (31, 180), (101, 300), (301, 1000)]


def diagnostics(returns):
    result = dict(shape=describe(returns), acf={})
    for name, values in [('signed', returns), ('abs', abs(returns)), ('sq', returns**2)]:
        curve = acf(values, 1000)
        # Independent direct calculation checks FFT normalization and lag alignment.
        centered = values - values.mean()
        for lag in [1, 31, 180, 1000]:
            expected = np.dot(centered[lag:], centered[:-lag]) / np.dot(centered, centered)
            assert np.isclose(curve[lag], expected, rtol=1e-10, atol=1e-13)
        result['acf'][name] = {f'{lo}_{hi}': float(curve[lo:hi+1].mean()) for lo, hi in BANDS}
        result['acf'][name]['max_abs_1_100'] = float(abs(curve[1:101]).max())
    return result


def masked_diagnostics(returns, valid):
    """Pairs stay on their original tick grid; missing observations are not joined."""
    result = dict(shape=describe(returns[valid]), acf={})
    for name, values in [('signed', returns), ('abs', abs(returns)), ('sq', returns**2)]:
        centered = values - values[valid].mean()
        variance = np.mean(centered[valid]**2)
        curve, counts = [], []
        for lag in range(1, 181):
            pairs = valid[lag:] & valid[:-lag]
            counts.append(int(pairs.sum()))
            curve.append(float(np.mean((centered[lag:] * centered[:-lag])[pairs]) / variance))
        result['acf'][name] = dict(lag1=curve[0], mean_31_180=float(np.mean(curve[30:])))
    result['pair_counts_31_180_min_max'] = [min(counts[30:]), max(counts[30:])]
    return result


def ranges(prices, valid=None):
    values = []
    for start in range(0, len(prices)-180, 180):
        # Window prices include the start and end; mask indexes the 180 returns.
        if valid is not None and not valid[start:start+180].all():
            continue
        window = prices[start:start+181]
        values.append(float((window.max()-window.min())/window[0]))
    return dict(windows=len(values), median=float(np.median(values)),
                p10=float(np.quantile(values, .1)), p90=float(np.quantile(values, .9)))


def main():
    inputs = [OUTPUT/'manifest.json', OUTPUT/'summary.json']
    inputs += [OUTPUT/f'{case}{suffix}' for case in ['control', 'shock']
               for suffix in ['.npz', '-episodes.json']]
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    summary = json.loads((OUTPUT/'summary.json').read_text())
    events = summary['shock']['events']
    event_ticks = np.array([e['tick'] for e in events])
    result = {}
    for case in ['control', 'shock']:
        with np.load(OUTPUT/f'{case}.npz') as saved:
            x = {key: saved[key] for key in saved.files}
        returns = x['returns'][1:]
        assert np.allclose(returns, np.diff(np.log(x['prices'])), atol=1e-14, rtol=1e-9)
        bot_returns = returns.copy()
        for event in summary[case]['events']:
            tick = event['tick']
            bot_returns[tick-1] = np.log(float(event['pre_price'])/x['prices'][tick-1])
            jump = np.log(float(event['post_price'])/float(event['pre_price']))
            assert np.isclose(bot_returns[tick-1]+jump, returns[tick-1], atol=1e-14)
        ticks = np.arange(1, len(returns)+1)
        quiet = np.ones(len(returns), dtype=bool)
        no_jump = quiet.copy()
        for tick in event_ticks:
            quiet &= ~((ticks >= tick) & (ticks <= tick+60))
            no_jump &= ticks != tick
        case_result = dict(raw=diagnostics(returns), bot_only=diagnostics(bot_returns),
                           quiet_same_calendar_mask=masked_diagnostics(returns, quiet),
                           range30_all=ranges(x['prices']),
                           range30_no_event_tick=ranges(x['prices'], no_jump),
                           range30_no_event_or_first_10m=ranges(x['prices'], quiet))
        counts = {key: int(x[key][1:].sum()) for key in
                  ['arrivals', 'trades', 'order_attempts', 'clipped_orders', 'skipped_orders']}
        counts.update(mean_intensity=float(x['intensity'][1:].mean()),
                      max_intensity=float(x['intensity'][1:].max()), max_arrivals=int(x['arrivals'][1:].max()),
                      clipped_per_attempt=counts['clipped_orders']/counts['order_attempts'],
                      executed_over_intended_abs=float(x['executed_abs_quantity'][1:].sum()/x['raw_abs_demand'][1:].sum()),
                      executed_abs_foreign=float(x['executed_abs_quantity'][1:].sum()))
        case_result['execution'] = counts
        case_result['groups'] = {group: dict(trades=int(x[f'{group}_trades'][1:].sum()),
            executed_volume_share=float(x[f'{group}_executed_abs_quantity'][1:].sum()/counts['executed_abs_foreign']))
            for group in ['random', 'reversion', 'factor']}
        episodes = json.loads((OUTPUT/f'{case}-episodes.json').read_text())
        holding = np.array([(e['close_tick']-e['entry_tick'])/6 for e in episodes if e['close_tick'] is not None])
        behavior = summary[case]['behavior']
        case_result['short_episodes'] = dict(behavior, holding_minutes_mean=float(holding.mean()),
            holding_minutes_median=float(np.median(holding)), holding_minutes_p90=float(np.quantile(holding, .9)),
            fraction_completed_5_15m=float(np.mean((holding >= 5) & (holding <= 15))))
        case_result['wallets'] = dict(min_gold=float(x['account_gold'].min()),
            min_foreign=float(x['account_foreign'].min()),
            min_marked_wealth=float(x['account_wealth'].min()),
            max_individual_drawdown=float(np.max(1-x['account_wealth']/np.maximum.accumulate(x['account_wealth'], axis=0))))
        case_result['event_tick_fraction_of_sum_squared_returns'] = float(np.sum(returns[event_ticks-1]**2)/np.sum(returns**2))
        result[case] = case_result
    for path, digest in hashes.items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest, path
    result['sources_sha256'] = hashes
    result['analysis_source_sha256'] = hashlib.sha256((HERE/'metrics.py').read_bytes()).hexdigest()
    (OUTPUT/'metrics.json').write_text(json.dumps(result, indent=2, allow_nan=False)+'\n')
    c, s = result['control'], result['shock']
    lines = ['# 连续冲击实验的补充指标', '',
        '沿用已保存的两条10000tick路径，市场种子81；不重跑、不调参。每tick10秒，无预热丢弃。单路径描述，不能替代跨种子验证。', '',
        '## 收益统计', '',
        'r为相邻收盘对数收益，含零收益。“去瞬时跳价”仅将10个事件tick替换为log(该tick常规成交后、发行前价格/上tick收盘价)，保留原始时序与所有后续反应；它仍受此前冲击影响，不等同于无冲击市场。', '',
        '|指标|无冲击对照|连续冲击原始收益|连续冲击去瞬时跳价|',
        '|---|---:|---:|---:|']
    samples = [c['raw'], s['raw'], s['bot_only']]
    rows = [('单tick收益标准差', lambda d:f"{d['shape']['std']:.3%}"),
            ('零收益占比', lambda d:f"{d['shape']['zero_fraction']:.2%}"),
            ('超额峰度（正态=0）', lambda d:f"{d['shape']['excess_kurtosis']:.3f}"),
            ('收益ACF lag1', lambda d:f"{d['acf']['signed']['1_1']:.5f}"),
            ('收益ACF最大绝对值 lag1–100', lambda d:f"{d['acf']['signed']['max_abs_1_100']:.5f}")]
    for field, label in [('abs', '绝对收益'), ('sq', 'r²')]:
        for band in ['31_180', '101_300', '301_1000']:
            rows.append((f'{label} ACF {band.replace("_", "–")}', lambda d,f=field,b=band:f"{d['acf'][f][b]:.5f}"))
    for label, value in rows:
        lines.append('|'+label+'|'+'|'.join(value(d) for d in samples)+'|')
    lines += ['', 'ACF沿用common.diagnostics：全局减均值后，滞后乘积和除以零滞后平方和，不作样本对数修正。表中区间为各lag等权平均；31–180tick约5–30分钟，301–1000约50–167分钟。最大绝对值只是描述，不是显著性或不可套利检验。', '',
        f"10个事件tick占原始收益平方和的{s['event_tick_fraction_of_sum_squared_returns']:.2%}；这是事件tick整体占比，含当tick普通成交及交叉项，不能直接解释成外生冲击的因果方差贡献。", '',
        '## 剔除事件及其后10分钟的敏感性', '',
        '两条路径均使用同一个日历掩码：去掉每次事件tick及其后60tick，保留9450个收益。ACF仅匹配两个端点都有效的原始tick对，不压缩、不拼接；允许端点之间跨越缺口，因此仍不代表一段完全不受冲击影响的市场。这里按有效对数归一化协方差/有效样本方差，与上表的全样本有偏ACF估计量略有不同。', '',
        '|指标|无冲击同日历掩码|冲击路径掩码|', '|---|---:|---:|']
    for field, label in [('signed', '收益'), ('abs', '绝对收益'), ('sq', 'r²')]:
        vals = [d['quiet_same_calendar_mask']['acf'][field]['mean_31_180'] for d in [c,s]]
        lines.append(f'|{label} ACF31–180|{vals[0]:.5f}|{vals[1]:.5f}|')
    lines += ['', '## 波幅、交易和持仓', '', '|指标|无冲击对照|连续冲击|', '|---|---:|---:|']
    for key, label in [('range30_all', '全部30分钟窗口波幅中位数'),
                       ('range30_no_event_tick', '不含事件tick的30分钟窗口波幅中位数'),
                       ('range30_no_event_or_first_10m', '避开事件及其后10分钟的完整窗口波幅中位数')]:
        lines.append('|'+label+'|'+'|'.join(f"{d[key]['median']:.2%}（{d[key]['windows']}窗；P10–P90 {d[key]['p10']:.2%}～{d[key]['p90']:.2%}）" for d in [c,s])+'|')
    for key, label, fmt in [('trades', '实际成交笔数', ',.0f'), ('mean_intensity', '平均预定到达强度/tick', '.3f'),
            ('max_intensity', '最大预定到达强度/tick', '.3f'), ('max_arrivals', '单tick最大实际到达数', '.0f'),
            ('clipped_per_attempt', '裁剪订单/实际订单尝试', '.2%'),
            ('executed_over_intended_abs', '实际执行绝对量/意向绝对量', '.2%')]:
        lines.append('|'+label+'|'+'|'.join(format(d['execution'][key],fmt) for d in [c,s])+'|')
    for key, label in [('holding_minutes_mean','已完成短线平均持仓/分钟'),('holding_minutes_median','已完成短线中位持仓/分钟'),('holding_minutes_p90','已完成短线P90持仓/分钟')]:
        lines.append('|'+label+'|'+'|'.join(f"{d['short_episodes'][key]:.2f}" for d in [c,s])+'|')
    for group, label in [('random', '随机'), ('reversion', '库存调节'), ('factor', '短线')]:
        lines.append('|'+label+'成交绝对量占比|'+'|'.join(f"{d['groups'][group]['executed_volume_share']:.1%}" for d in [c,s])+'|')
    lines += ['', '30分钟波幅=(窗口最高价−最低价)/期初价，非重叠完整180tick窗口，含两个端点价格，尾部不足窗口丢弃。对照的窗口剔除也按相同事件日历，方便对照；尚未达到的2%目标不应改写为已达成。持仓统计只包括已完成轮次，期末未平仓留在原报告中。', '',
        f"短线完成/未完成：对照{c['short_episodes']['completed']}/{c['short_episodes']['unfinished']}，冲击{s['short_episodes']['completed']}/{s['short_episodes']['unfinished']}。冲击路径退出原因：{s['short_episodes']['exits']}；短线已实现盈亏{s['short_episodes']['realized_pnl']}金圆券，不能与整钱包净值变化混淆。", '',
        '## 判断边界', '',
        '冲击后有回吐、活动增加，短线持仓也缩短；常态波幅仍低于2%，平均短线持仓仍略长于5–15分钟目标。去跳价后5–30分钟的幅度相关为正，但无冲击对照的同段相关很弱，较远滞后也没有稳定正相关证据。这轮没有关闭反馈b=0的消融，不将冲击响应直接归因于某一种机制，更不能沿用旧纯随机基线的长路径结论来认证当前混合模型。', '',
        '全路径短滞后收益有轻微负相关，不能宣称严格“涨跌没有相关性”；10次强制外部跳价极大抬高原始峰度，不能用558宣称机器人内生复现了那么厚的尾。正超额峰度也不等于幂律尾，零收益较多的稀疏成交本身会影响峰度。', '',
        '重现：`python -m research.fx.reversion.repeated.metrics`。脚本核对FFT与直接点积ACF、收益与价格对齐、跳价分解和输入文件哈希；数据与来源哈希见[metrics.json](metrics.json)。本补充未修改模型、重跑模拟或重新进行多种子验证。', '']
    (OUTPUT/'metrics.md').write_text('\n'.join(lines))
    print('Saved metrics.json and metrics.md; input hashes unchanged; direct ACF and return checks passed.')


if __name__ == '__main__':
    main()
