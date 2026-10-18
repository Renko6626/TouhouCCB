"""One no-shock run of the frozen FX model, with descriptive time-scale metrics.

Run from repository root: python output/fx-timescale-20261016/run.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import csv
import hashlib
import json
import time
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy.signal import welch
from research.fx.reversion.repeated.entry_model import simulate
from research.fx.reversion.repeated.population_time_metrics import metrics
from research.fx.reversion.repeated.timing_metrics import price_ranges
from research.fx.common.diagnostics import acf
from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import clean_json

OUT = Path(__file__).resolve().parent
FROZEN = ROOT / 'research/fx/reversion/repeated/FROZEN_CONFIG.json'
SEED = 20261016


def swings(prices, threshold):
    """Alternating extrema confirmed by a relative-price reversal; no smoothing.

    Exclude the first boundary leg and unconfirmed final extremum. Durations
    refer to extremum timestamps, not the later time of confirmation.
    """
    pivots = []
    direction = 0
    low = high = 0
    extreme = 0
    for i in range(1, len(prices)):
        if direction == 0:
            if prices[i] < prices[low]: low = i
            if prices[i] > prices[high]: high = i
            if prices[i] >= prices[low] * (1 + threshold):
                pivots.append((low, i)); direction = 1; extreme = i
            elif prices[i] <= prices[high] * (1 - threshold):
                pivots.append((high, i)); direction = -1; extreme = i
        elif direction == 1:
            if prices[i] > prices[extreme]: extreme = i
            elif prices[i] <= prices[extreme] * (1 - threshold):
                pivots.append((extreme, i)); direction = -1; extreme = i
        else:
            if prices[i] < prices[extreme]: extreme = i
            elif prices[i] >= prices[extreme] * (1 + threshold):
                pivots.append((extreme, i)); direction = 1; extreme = i
    legs = [dict(start=a[0], end=b[0], confirmed_at=b[1],
                 minutes=(b[0]-a[0])*5/60,
                 change=prices[b[0]]/prices[a[0]]-1)
            for a, b in zip(pivots[1:-1], pivots[2:])]
    cycles = [(pivots[i+2][0]-pivots[i][0])*5/60 for i in range(1,len(pivots)-2)]
    return pivots, legs, cycles


def stats(values):
    a = np.asarray(values)
    if not len(a): return dict(n=0, median=None, p90=None, mean=None)
    return dict(n=len(a), median=float(np.median(a)), p90=float(np.quantile(a,.9)), mean=float(a.mean()))


def main():
    frozen = json.loads(FROZEN.read_text())
    cfg = {**frozen['research_reproduction']['selected_simulator_config'], 'seeds':[SEED]}
    assert cfg['ticks'] == 10000 and frozen['tick_seconds'] == 5
    cfg.update(frozen['parameters'])
    settings = {'trend_multiplier':frozen['trend_multiplier']}
    sources = [FROZEN, ROOT/'research/fx/reversion/repeated/entry_model.py',
               ROOT/'research/fx/episodes/model.py', ROOT/'research/fx/activity/model.py']
    manifest = dict(seed=SEED, config=cfg, policy=frozen['policy'], settings=settings,
                    profiles=frozen['profiles'], activity_b=frozen['activity_b'], shocks={},
                    burn_in_ticks=0, note='Full startup path; descriptive single-seed experiment, no stationarity assumption.',
                    source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    start=time.monotonic()
    print('Simulating 10000 ticks, seed',SEED,flush=True)
    x,s,episodes,fills,inventory,decisions = simulate(cfg,frozen['profiles'],5,frozen['activity_b'],SEED,{},frozen['policy'],settings)
    print('Simulation complete, seconds:',round(time.monotonic()-start,2),flush=True)
    assert not s['events'] and not np.any(x['injection_gold']) and not np.any(x['injection_foreign'])
    assert all(np.isfinite(a).all() for a in x.values())
    assert x['account_gold'].min()>=0 and x['account_foreign'].min()>=0
    np.testing.assert_allclose(x['gold']+x['account_gold'].sum(axis=1),1100000,atol=1e-8,rtol=0)
    np.testing.assert_allclose(x['pool_foreign']+x['account_foreign'].sum(axis=1),220000,atol=1e-8,rtol=0)
    np.testing.assert_allclose(x['account_wealth'],x['account_gold']+x['account_foreign']*x['prices'][:,None],atol=1e-8,rtol=0)
    np.testing.assert_array_equal(x['account_trades'].sum(axis=1),x['trades'])
    save_trace(OUT,'trace',x,episodes,fills)
    (OUT/'summary.json').write_text(json.dumps(clean_json(s),indent=2)+'\n')
    result=metrics(x,s,episodes,5)
    prices=x['prices']; r=np.diff(np.log(prices)); hours=np.arange(len(prices))*5/3600
    result.update(seed=SEED, net_change=float(prices[-1]/prices[0]-1),
                  total_range=float(prices.max()/prices.min()-1), trades_per_minute=float(x['trades'].sum()/(10000*5/60)))
    holding=[(e['close_tick']-e['entry_tick'])*5/60 for e in episodes if e['close_tick'] is not None]
    result['completed_holding_minutes']=stats(holding)
    result['swings']={}; pivot_sets={}
    for threshold in [.002,.005,.01]:
        pivots,legs,cycles=swings(prices,threshold); pivot_sets[threshold]=pivots
        result['swings'][str(threshold)]=dict(leg_minutes=stats([l['minutes'] for l in legs]),
            overlapping_cycle_minutes=stats(cycles), legs=legs,
            excluded='First boundary leg and unconfirmed tail; cycles overlap, not independent observations.')
    scale_rows=[]
    for minutes in [1,5,15,30,60]:
        width=minutes*12; n=len(r)//width; blocks=r[:n*width].reshape(n,width)
        net=blocks.sum(axis=1); gross=np.abs(blocks).sum(axis=1)
        up=net[net>0].sum(); down=-net[net<0].sum()
        scale_rows.append(dict(minutes=minutes,complete_blocks=n,discarded_ticks=len(r)-n*width,
            direction_retention=float(np.abs(net).sum()/gross.sum()),
            mean_absolute_body=float(np.abs(net).mean()),
            up_down_body_ratio=float(up/down) if down else None,
            variance_ratio=float(np.var(net,ddof=1)/(width*np.var(blocks.ravel(),ddof=1))),
            mean_range=float(np.mean([(prices[i*width:(i+1)*width+1].max()/prices[i*width:(i+1)*width+1].min()-1) for i in range(n)]))))
    result['scale_metrics']=scale_rows
    with (OUT/'scales.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=scale_rows[0].keys());writer.writeheader();writer.writerows(scale_rows)
    curves={name:acf(v,720) for name,v in [('returns',r),('absolute_returns',abs(r)),('squared_returns',r*r)]}
    result['acf_at_minutes']={name:{str(m):float(curve[m*12]) for m in [1,5,15,30,60]} for name,curve in curves.items()}
    freq,power=welch(r,fs=1/5,nperseg=2048,detrend='constant')
    keep=(freq>=1/3600)&(freq<=1/60); periods=1/freq[keep]/60; ps=power[keep]
    result['descriptive_spectral_peak_minutes_1_to_60']=float(periods[np.argmax(ps)])
    result['limitations']=['One seed; no confidence intervals or stable-period claim.',
        'No external shocks or players, zero fees, no warm-up discarded.',
        'Threshold swings are retrospective confirmed extrema; unfinished tail excluded.',
        'Variance ratios are descriptive; no significance test.',
        'No shock experiment: price-recovery half-life not estimated.']
    (OUT/'metrics.json').write_text(json.dumps(clean_json(result),ensure_ascii=False,indent=2)+'\n')
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
    fig,axes=plt.subplots(3,1,figsize=(14,10),layout='constrained')
    axes[0].plot(hours,prices,color='#176997',lw=.8,label='池价（5秒收盘）')
    axes[0].plot(hours[1:],x['slow_reference'][1:],color='#b27a16',lw=1.1,label='30分钟库存参考')
    pv=pivot_sets[.005]; axes[0].plot([hours[t] for t,_ in pv],[prices[t] for t,_ in pv],color='#a14870',lw=.8,marker='o',ms=2,label='0.5%反转确认的极值')
    axes[0].set(ylabel='金圆券／外币',title='冻结FX机器人，无冲击单路径：20户，5秒tick，seed 20261016');axes[0].legend(ncol=3,fontsize=9)
    width=12;count=len(r)//width
    axes[1].plot(np.arange(count)/60,np.sqrt((r[:count*width].reshape(count,width)**2).sum(axis=1))*100,color='#176997',lw=.8)
    axes[1].set(ylabel='1分钟实现波动（%）',title='每分钟内5秒对数收益平方和的平方根')
    axes[2].plot(hours[1:],x['intensity'][1:]*12,color='#176997',lw=.8)
    axes[2].set(ylabel='期望到达次数／分钟',xlabel='模拟时间（小时）',title='公共到达强度：到达不保证成交')
    for ax in axes: ax.grid(alpha=.18);ax.set_xlim(0,hours[-1])
    fig.savefig(OUT/'path.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(2,2,figsize=(13,9),layout='constrained')
    for threshold,color in zip([.002,.005,.01],['#176997','#b27a16','#a14870']):
        vals=sorted(l['minutes'] for l in result['swings'][str(threshold)]['legs'])
        if vals: axes[0,0].step(vals,np.arange(1,len(vals)+1)/len(vals),where='post',color=color,label=f'{threshold:.1%}反转（{len(vals)}段）')
    axes[0,0].set(xlabel='单向波段时长（分钟）',ylabel='累计占比',title='波段时长随反转门槛变化');axes[0,0].legend(fontsize=9)
    for (name,label),color in zip([('returns','收益'),('absolute_returns','绝对收益'),('squared_returns','收益平方')],['#176997','#b27a16','#a14870']):
        axes[0,1].plot(np.arange(60)+.5,curves[name][1:].reshape(60,12).mean(axis=1),lw=1,color=color,label=label)
    axes[0,1].axhline(0,color='#444',lw=.7);axes[0,1].set(xlabel='滞后（分钟）',ylabel='自相关（每1分钟滞后区间取均值）',title='5秒收益与波动强度的自相关');axes[0,1].legend(fontsize=9)
    axes[1,0].plot([v['minutes'] for v in scale_rows],[v['direction_retention']*100 for v in scale_rows],marker='o',color='#176997')
    axes[1,0].set(xlabel='聚合周期（分钟）',ylabel='方向保留率（%）',ylim=(0,100),title='大周期净变化／5秒累计绝对变化')
    axes[1,1].plot(periods[::-1],ps[::-1],color='#176997',lw=1)
    axes[1,1].set(xlabel='对应周期（分钟）',ylabel='收益谱密度（每Hz）',title='收益功率谱：单路径峰值不代表固定周期')
    for ax in axes.flat: ax.grid(alpha=.18)
    fig.savefig(OUT/'timescales.png',dpi=150);plt.close(fig)
    (OUT/'audit.json').write_text(json.dumps(dict(no_shocks=True,finite_series=True,nonnegative_wallets=True,
        both_assets_conserved=True,wallet_valuation_verified=True,trade_counts_verified=True,
        simulator_elapsed_seconds=time.monotonic()-start),indent=2)+'\n')
    print(json.dumps(clean_json({k:v for k,v in result.items() if k in ['net_change','total_range','trades_per_minute','completed_holding_minutes','scale_metrics','acf_at_minutes','descriptive_spectral_peak_minutes_1_to_60']}),ensure_ascii=False,indent=2),flush=True)
    print('Swing durations:',{k:v['leg_minutes'] for k,v in result['swings'].items()},flush=True)


if __name__=='__main__': main()
