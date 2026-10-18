"""Paired-seed no-shock comparison; frozen model and earlier outputs stay intact.

Run: python output/fx-timescale-variants-20261016/run.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import csv
import hashlib
import json
import runpy
import time
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from research.fx.reversion.repeated.entry_model import simulate
from research.fx.reversion.repeated.population_time_metrics import metrics
from research.fx.common.diagnostics import acf
from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import clean_json

OUT = Path(__file__).resolve().parent
PREVIOUS = ROOT/'output/fx-timescale-20261016'
helpers = runpy.run_path(str(PREVIOUS/'run.py'))
swings, stats = helpers['swings'], helpers['stats']
SEED = 20261016
VARIANTS = {
    'baseline': {},
    'A_inventory015': {'inventory_adjustment': .15},
    'B_wider_exits': {'take_profit': .008, 'stop_loss': .01,
                      'trail_arm': .004, 'trail_drawdown': .004},
}
LABELS = {'baseline': '原配置', 'A_inventory015': 'A：库存调仓比例0.15',
          'B_wider_exits': 'B：短线退出门槛加倍'}
COLORS = {'baseline': '#176997', 'A_inventory015': '#b27a16', 'B_wider_exits': '#a14870'}


def scale_metrics(prices):
    r = np.diff(np.log(prices))
    rows = []
    for minutes in [1, 5, 15, 30, 60]:
        w = minutes * 12
        n = len(r)//w
        blocks = r[:n*w].reshape(n, w)
        net = blocks.sum(axis=1)
        gross = abs(blocks).sum()
        down = -net[net < 0].sum()
        rows.append(dict(minutes=minutes, complete_blocks=n, discarded_ticks=len(r)-n*w,
            direction_retention=float(abs(net).sum()/gross),
            mean_absolute_body=float(abs(net).mean()),
            up_down_body_ratio=float(net[net > 0].sum()/down) if down else None,
            variance_ratio=float(np.var(net, ddof=1)/(w*np.var(blocks.ravel(), ddof=1))),
            mean_range=float(np.mean([prices[i*w:(i+1)*w+1].max()/prices[i*w:(i+1)*w+1].min()-1
                                      for i in range(n)]))))
    return rows


def validate(x, summary, episodes):
    assert not summary['events']
    assert not np.any(x['injection_gold']) and not np.any(x['injection_foreign'])
    assert all(np.isfinite(a).all() for a in x.values())
    assert x['account_gold'].min() >= 0 and x['account_foreign'].min() >= 0
    np.testing.assert_allclose(x['gold']+x['account_gold'].sum(axis=1), 1100000, atol=1e-8, rtol=0)
    np.testing.assert_allclose(x['pool_foreign']+x['account_foreign'].sum(axis=1), 220000, atol=1e-8, rtol=0)
    np.testing.assert_allclose(x['account_wealth'], x['account_gold']+x['account_foreign']*x['prices'][:, None], atol=1e-8, rtol=0)
    np.testing.assert_array_equal(x['account_trades'].sum(axis=1), x['trades'])
    done = [e for e in episodes if e['close_tick'] is not None]
    np.testing.assert_allclose(sum(float(e['realized_profit']) for e in done),
                               float(summary['behavior']['realized_pnl']), atol=1e-7, rtol=0)


def main():
    frozen_path = ROOT/'research/fx/reversion/repeated/FROZEN_CONFIG.json'
    frozen = json.loads(frozen_path.read_text())
    base = {**frozen['research_reproduction']['selected_simulator_config'],
            **frozen['parameters'], 'seeds': [SEED]}
    assert frozen['tick_seconds'] == 5 and base['ticks'] == 10000
    sources = [frozen_path, PREVIOUS/'run.py', PREVIOUS/'metrics.json', PREVIOUS/'trace.npz',
               ROOT/'research/fx/reversion/repeated/entry_model.py',
               ROOT/'research/fx/reversion/inventory.py', ROOT/'research/fx/episodes/model.py',
               ROOT/'research/fx/activity/model.py', ROOT/'research/fx/factor_activity/model.py',
               ROOT/'research/fx/common/clock.py', ROOT/'research/fx/common/execution.py']
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    manifest = dict(seed=SEED, tick_seconds=5, ticks=10000, shocks={}, burn_in_ticks=0,
        baseline_config=base, overrides=VARIANTS, profiles=frozen['profiles'],
        policy=frozen['policy'], trend_multiplier=frozen['trend_multiplier'], activity_b=frozen['activity_b'],
        protected_sha256=hashes, limitation='One seed, full startup path, zero fees, no players. Same seed does not mean identical orders after feedback diverges.')
    (OUT/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    results, traces, audits = {}, {}, {}
    for name, overrides in VARIANTS.items():
        start = time.monotonic()
        print('Running', name, overrides, flush=True)
        cfg = {**base, **overrides}
        x, summary, episodes, fills, inventory, decisions = simulate(
            cfg, frozen['profiles'], 5, frozen['activity_b'], SEED, {}, frozen['policy'],
            {'trend_multiplier': frozen['trend_multiplier']})
        validate(x, summary, episodes)
        if name == 'baseline':
            with np.load(PREVIOUS/'trace.npz') as old:
                assert set(old.files) == set(x)
                for k in old.files: np.testing.assert_array_equal(x[k], old[k], err_msg=k)
        directory = OUT/name
        directory.mkdir(exist_ok=True)
        save_trace(directory, 'trace', x, episodes, fills)
        for label, value in [('summary', summary), ('config', cfg), ('inventory', inventory), ('decisions', decisions)]:
            (directory/f'{label}.json').write_text(json.dumps(clean_json(value), ensure_ascii=False, indent=2)+'\n')
        result = metrics(x, summary, episodes, 5)
        prices = x['prices']
        done = [e for e in episodes if e['close_tick'] is not None]
        result.update(variant=name, seed=SEED, net_change=float(prices[-1]/prices[0]-1),
            total_range=float(prices.max()/prices.min()-1),
            max_abs_displacement_from_start=float(np.max(abs(prices/prices[0]-1))),
            trades_per_minute=float(x['trades'].sum()/(10000*5/60)),
            completed_holding_minutes=stats([(e['close_tick']-e['entry_tick'])/12 for e in done]),
            unfinished_max_age_minutes=max(((10000-e['entry_tick'])/12 for e in episodes if e['close_tick'] is None),default=None),
            stop_loss_fraction=float(np.mean([e['closing_reason']=='stop_loss' for e in done])),
            scale_metrics=scale_metrics(prices), swings={})
        for threshold in [.002, .005, .01]:
            _, legs, cycles = swings(prices, threshold)
            result['swings'][str(threshold)] = dict(leg_minutes=stats([v['minutes'] for v in legs]),
                overlapping_cycle_minutes=stats(cycles),
                absolute_leg_change=stats([abs(v['change']) for v in legs]), legs=legs)
        for role, lo, hi in [('random',0,10), ('inventory',10,15), ('short',15,20)]:
            wealth = x['account_wealth'][-1,lo:hi]
            result[f'{role}_mean_wealth_change'] = float(wealth.mean()-10000)
            result[f'{role}_mean_wealth_vs_hold'] = float(wealth.mean()-(5000+1000*prices[-1]))
        (directory/'metrics.json').write_text(json.dumps(clean_json(result),ensure_ascii=False,indent=2)+'\n')
        results[name], traces[name] = result, x
        audits[name] = dict(no_shocks=True, all_series_finite=True, wallets_nonnegative=True,
            assets_conserved=True, wallet_values_verified=True, trade_counts_verified=True,
            realized_episode_pnl_verified=True, elapsed_seconds=time.monotonic()-start)
        print(name, 'completed in', round(time.monotonic()-start,2), 'seconds', flush=True)
    rows = []
    for name, r in results.items():
        rows.append(dict(variant=name, net_change=r['net_change'], total_range=r['total_range'],
            range30_median=r['range30']['median'], trades_per_minute=r['trades_per_minute'],
            leg05_median=r['swings']['0.005']['leg_minutes']['median'],
            leg05_p90=r['swings']['0.005']['leg_minutes']['p90'],
            cycle05_median=r['swings']['0.005']['overlapping_cycle_minutes']['median'],
            leg1_median=r['swings']['0.01']['leg_minutes']['median'],
            cycle1_median=r['swings']['0.01']['overlapping_cycle_minutes']['median'],
            holding_median=r['completed_holding_minutes']['median'],
            holding_p90=r['completed_holding_minutes']['p90'],
            completed_episodes=r['completed_holding_minutes']['n'], unfinished=r['unfinished'],
            unfinished_max_age_minutes=r['unfinished_max_age_minutes'],
            short_realized_per_account=r['short_realized_per_account'],
            short_mean_vs_hold=r['short_mean_vs_hold'], stop_loss_fraction=r['stop_loss_fraction'],
            retention5=r['scale_metrics'][1]['direction_retention'], vr5=r['scale_metrics'][1]['variance_ratio']))
    with (OUT/'comparison.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=rows[0]);writer.writeheader();writer.writerows(rows)
    (OUT/'metrics.json').write_text(json.dumps(clean_json(results),ensure_ascii=False,indent=2)+'\n')
    plot(traces, results)
    for path, digest in hashes.items(): assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    audits['frozen_sources_and_previous_outputs_unchanged'] = True
    audits['baseline_exactly_reproduces_previous_trace'] = True
    (OUT/'audit.json').write_text(json.dumps(audits,indent=2)+'\n')
    print(json.dumps(clean_json(rows),ensure_ascii=False,indent=2),flush=True)


def plot(traces, results):
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
    hours = np.arange(10001)/720
    all_pct = np.concatenate([(x['prices']/x['prices'][0]-1)*100 for x in traces.values()])
    low, high = all_pct.min()-.3, all_pct.max()+.3
    fig, axes = plt.subplots(3,2,figsize=(15,10),layout='constrained')
    for i, (name,x) in enumerate(traces.items()):
        pct=(x['prices']/x['prices'][0]-1)*100
        ref=(x['slow_reference'][1:]/x['prices'][0]-1)*100
        axes[i,0].plot(hours,pct,lw=.8,color=COLORS[name],label='价格')
        axes[i,0].plot(hours[1:],ref,lw=1,color='#555',ls='--',label='30分钟库存参考')
        axes[i,0].set(title=LABELS[name]+'：完整路径',ylabel='相对初价变化（%）',ylim=(low,high),xlim=(0,hours[-1]))
        axes[i,0].legend(fontsize=8,ncol=2)
        axes[i,1].plot(hours[:1441]*60,pct[:1441],lw=.8,color=COLORS[name])
        axes[i,1].set(title=LABELS[name]+'：前120分钟',ylabel='相对初价变化（%）',xlim=(0,120),ylim=(low,high))
    axes[2,0].set_xlabel('模拟时间（小时）');axes[2,1].set_xlabel('模拟时间（分钟）')
    for ax in axes.flat: ax.axhline(0,color='#555',lw=.6);ax.grid(alpha=.18)
    fig.suptitle('无冲击三组对照：同一seed 20261016，20户，5秒tick，10000tick；价格轴统一',fontsize=13)
    fig.savefig(OUT/'paths.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(2,2,figsize=(13,9),layout='constrained')
    for name,r in results.items():
        vals=sorted(v['minutes'] for v in r['swings']['0.005']['legs'])
        axes[0,0].step(vals,np.arange(1,len(vals)+1)/len(vals),where='post',color=COLORS[name],label=f"{LABELS[name]}（{len(vals)}段）")
        axes[0,1].plot([v['minutes'] for v in r['scale_metrics']],[v['direction_retention']*100 for v in r['scale_metrics']],marker='o',color=COLORS[name],label=LABELS[name])
        axes[1,0].plot([v['minutes'] for v in r['scale_metrics']],[v['mean_range']*100 for v in r['scale_metrics']],marker='o',color=COLORS[name],label=LABELS[name])
        absolute=acf(abs(np.diff(np.log(traces[name]['prices']))),720)[1:].reshape(60,12).mean(axis=1)
        axes[1,1].plot(np.arange(60)+.5,absolute,color=COLORS[name],label=LABELS[name],lw=1)
    axes[0,0].set(title='0.5%反转门槛：单向波段时长',xlabel='分钟',ylabel='累计占比')
    axes[0,1].set(title='方向保留率',xlabel='聚合周期（分钟）',ylabel='大周期净变化／5秒累计绝对变化（%）',ylim=(0,100))
    axes[1,0].set(title='完整时间块内的平均高低波幅',xlabel='时间块长度（分钟）',ylabel='最高价／最低价−1（%）',ylim=(0,None))
    axes[1,1].set(title='绝对收益自相关（每1分钟滞后区间取均值）',xlabel='滞后（分钟）',ylabel='自相关')
    axes[1,1].axhline(0,color='#555',lw=.6)
    for ax in axes.flat: ax.grid(alpha=.18);ax.legend(fontsize=8)
    fig.savefig(OUT/'timescales.png',dpi=150);plt.close(fig)


if __name__ == '__main__': main()
