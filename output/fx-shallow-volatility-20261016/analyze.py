"""Descriptive volatility of saved shallow-pool holdouts; no new simulations.
Run: python output/fx-shallow-volatility-20261016/analyze.py
"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import csv
import hashlib
import json
import numpy as np
from scipy.stats import skew,kurtosis
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from research.fx.common.diagnostics import acf

OUT=Path(__file__).resolve().parent
SOURCE=ROOT/'output/fx-shallow-calibration-20261016'
SEEDS=[20261029,20261030,20261031]
NAMES=['deep_baseline','shallow_unmodified','a1.8_t2']
LABELS=['深池原配置','浅池未调参数','浅池慢候选']
COLORS=['#176997','#777','#a14870']
WIDTHS=[1,12,60,180,360,720]


def describe(a):
    a=np.asarray(a,dtype=float)
    return dict(n=len(a),mean=float(a.mean()),median=float(np.median(a)),
        p10=float(np.quantile(a,.1)),p90=float(np.quantile(a,.9)),p99=float(np.quantile(a,.99)),max=float(a.max()))


def analyze(prices):
    p=prices[360:];r=np.diff(np.log(p));sq=r*r
    horizons={}
    for w in WIDTHS:
        n=len(r)//w;fine=r[:n*w].reshape(n,w);net=fine.sum(axis=1)
        rv=np.sqrt((fine*fine).sum(axis=1))
        ranges=np.array([(p[i*w:(i+1)*w+1].max()-p[i*w:(i+1)*w+1].min())/p[i*w] for i in range(n)])
        horizons[str(w)]=dict(minutes=w/12,blocks=n,discarded_ticks=len(r)-n*w,
            net_return_std=float(np.std(net,ddof=1)),absolute_net_return=describe(abs(net)),
            realized_volatility=describe(rv),high_low_range=describe(ranges),
            variance_ratio=float(np.var(net,ddof=1)/(w*np.var(fine.ravel(),ddof=1))),
            direction_retention=float(abs(net).sum()/abs(fine).sum()))
    n=len(r)//12;minute=r[:n*12].reshape(n,12)
    minute_net=minute.sum(axis=1);minute_rv=np.sqrt((minute*minute).sum(axis=1))
    curves={k:acf(v,60) for k,v in [('minute_abs_return',abs(minute_net)),('minute_rv',minute_rv)]}
    acf_metrics={name:dict(lag1=float(v[1]),mean_lags1_5=float(v[1:6].mean()),mean_lags6_30=float(v[6:31].mean()),curve=v.tolist())
                 for name,v in curves.items()}
    shares={}
    for fraction in [.001,.01,.05,.1]:
        count=int(np.ceil(len(r)*fraction))
        shares[str(fraction)]=dict(ticks=count,share_of_squared_returns=float(np.sort(sq)[-count:].sum()/sq.sum()))
    rv30=np.array(horizons['360']['realized_volatility']['median'])
    # Independent identities: net log returns from price endpoints and total RV.
    for w in WIDTHS:
        n=len(r)//w;ends=np.arange(n+1)*w
        net=np.log(p[ends[1:]]/p[ends[:-1]])
        np.testing.assert_allclose(np.std(net,ddof=1),horizons[str(w)]['net_return_std'],rtol=1e-11,atol=1e-14)
        fine=r[:n*w].reshape(n,w)
        np.testing.assert_allclose(np.sum(np.sqrt((fine*fine).sum(axis=1))**2),np.dot(r[:n*w],r[:n*w]),rtol=1e-13)
    active=r[r!=0]
    result=dict(ticks=len(r),hours=len(r)*5/3600,zero_return_fraction=float(np.mean(r==0)),
        nonzero_return_std=float(np.std(active,ddof=1)),nonzero_count=len(active),
        skewness=float(skew(r,bias=False)),excess_kurtosis=float(kurtosis(r,fisher=True,bias=False)),
        active_excess_kurtosis=float(kurtosis(active,fisher=True,bias=False)),
        max_abs_tick_return=float(abs(r).max()),total_realized_volatility=float(np.sqrt(np.dot(r,r))),
        variance_concentration=shares,acf=acf_metrics,horizons=horizons)
    return result,minute_rv,np.sqrt((r[:len(r)//360*360].reshape(-1,360)**2).sum(axis=1))


def main():
    result={};rv30s={};curves={};rows=[];hashes={}
    for name in NAMES:
        result[name]={};rv30s[name]=[];curves[name]=[]
        for seed in SEEDS:
            file=SOURCE/name/f'seed{seed}'/'trace.npz'
            hashes[str(file.relative_to(ROOT))]=hashlib.sha256(file.read_bytes()).hexdigest()
            with np.load(file) as z:prices=z['prices'].copy()
            assert len(prices)==10001 and np.isfinite(prices).all() and (prices>0).all()
            m,minute_rv,rv30=analyze(prices)
            result[name][str(seed)]=m;rv30s[name].extend(rv30);curves[name].append(m['acf']['minute_rv']['curve'])
            for w,h in m['horizons'].items():
                rows.append(dict(variant=name,seed=seed,minutes=h['minutes'],blocks=h['blocks'],
                    net_return_std=h['net_return_std'],abs_net_median=h['absolute_net_return']['median'],
                    rv_median=h['realized_volatility']['median'],rv_p90=h['realized_volatility']['p90'],
                    range_median=h['high_low_range']['median'],variance_ratio=h['variance_ratio'],
                    direction_retention=h['direction_retention']))
    averages={}
    for name,by_seed in result.items():
        ms=list(by_seed.values());avg=lambda values:float(np.mean(values))
        averages[name]=dict(horizons={str(w):{key:avg([row[key] for row in rows if row['variant']==name and row['minutes']==w/12])
            for key in ['net_return_std','abs_net_median','rv_median','rv_p90','range_median','variance_ratio','direction_retention']} for w in WIDTHS},
            zero_return_fraction=avg([m['zero_return_fraction'] for m in ms]),
            nonzero_return_std=avg([m['nonzero_return_std'] for m in ms]),
            excess_kurtosis=avg([m['excess_kurtosis'] for m in ms]),
            active_excess_kurtosis=avg([m['active_excess_kurtosis'] for m in ms]),
            top1pct_variance_share=avg([m['variance_concentration']['0.01']['share_of_squared_returns'] for m in ms]),
            minute_rv_acf1=avg([m['acf']['minute_rv']['lag1'] for m in ms]),
            minute_rv_acf6_30=avg([m['acf']['minute_rv']['mean_lags6_30'] for m in ms]))
    manifest=dict(seeds=SEEDS,variants=NAMES,source_sha256=hashes,first_price_index=360,returns_per_seed=9640,
        return_definition='Natural log returns. Non-overlapping blocks aligned to tick 360; incomplete final block discarded.',
        rv_definition='sqrt(sum(5-second log return squared)), not centered or annualized.',
        aggregation='Headline metrics are per-seed estimates averaged with equal seed weight; CDF pools 78 non-overlapping windows descriptively.',
        limitations=['Three seed paths, zero fees, no shocks or players.','No confidence intervals or statistical significance tests.',
            'ACF is computed separately in each path, never across concatenated seeds.','No long-memory or price-recovery half-life claim.',
            'The slow candidate was already examined on these seeds; this is descriptive follow-up, not new holdout validation.'])
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    (OUT/'metrics.json').write_text(json.dumps(dict(per_seed=result,seed_equal_averages=averages),indent=2)+'\n')
    with (OUT/'horizons.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=rows[0]);writer.writeheader();writer.writerows(rows)
    plot(averages,rv30s,curves,result)
    for path,digest in hashes.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    (OUT/'audit.json').write_text(json.dumps(dict(paths_analyzed=9,new_simulations=0,source_traces_unchanged=True,
        finite_positive_prices=True,net_returns_independently_checked_from_endpoints=True,
        realized_variance_partition_identity_verified=True),indent=2)+'\n')
    print(json.dumps(averages,indent=2),flush=True)


def plot(means,rv30s,curves,result):
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
    fig,axes=plt.subplots(2,2,figsize=(14,9),layout='constrained')
    for name,label,color in zip(NAMES,LABELS,COLORS):
        h=means[name]['horizons']
        axes[0,0].plot([w/12 for w in WIDTHS],[h[str(w)]['net_return_std']*100 for w in WIDTHS],marker='o',color=color,label=label)
        a=np.sort(rv30s[name])*100
        axes[0,1].step(a,np.arange(1,len(a)+1)/len(a),where='post',color=color,label=label)
        axes[1,0].plot(np.arange(1,61),np.mean(curves[name],axis=0)[1:],color=color,label=label,lw=1)
        fractions=[.001,.01,.05,.1]
        shares=[np.mean([m['variance_concentration'][str(f)]['share_of_squared_returns'] for m in result[name].values()])*100 for f in fractions]
        axes[1,1].plot(np.array(fractions)*100,shares,marker='o',color=color,label=label)
    axes[0,0].set(xscale='log',xlabel='持有时间（分钟，对数轴）',ylabel='净对数收益标准差（%）',title='不同持有时间的净变化波动率',ylim=(0,None))
    axes[0,0].set_xticks([1/12,1,5,15,30,60],['5秒','1','5','15','30','60'])
    axes[0,1].set(xlabel='30分钟实现波动率（%）',ylabel='累计占比',title='累计波动量：每组78个完整半小时窗口')
    axes[1,0].set(xlabel='滞后（分钟）',ylabel='三个种子平均自相关',title='1分钟实现波动率的持续性')
    axes[1,0].axhline(0,color='#444',lw=.7)
    axes[1,1].set(xscale='log',xlabel='最大绝对收益的tick占比（%）',ylabel='贡献的收益平方总和占比（%）',title='波动是否集中在少数跳动',ylim=(0,100))
    axes[1,1].set_xticks([.1,1,5,10],['0.1','1','5','10'])
    for ax in axes.flat:ax.grid(alpha=.18);ax.legend(fontsize=9)
    fig.suptitle('浅池慢候选的波动率：原验证种子20261029／30／31，剔除启动30分钟，不年化',fontsize=14)
    fig.savefig(OUT/'volatility.png',dpi=150);plt.close(fig)


if __name__=='__main__':main()
