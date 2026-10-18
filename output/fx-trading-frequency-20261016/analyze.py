"""Look arrivals, actual fills, volumes and inventory capacity; sigma remains108.
Run: python output/fx-trading-frequency-20261016/analyze.py
"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import csv
import hashlib
import json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

OUT=Path(__file__).resolve().parent
SOURCE=ROOT/'output/fx-shallow-calibration-20261016/a1.8_t2'
SEEDS=[20261029,20261030,20261031]
GROUPS={'random':list(range(10)),'inventory':list(range(10,15)),
        'trend':[16,18],'short_reversion':[15,17],'noise_short':[19]}
LABELS={'random':'纯随机（10户）','inventory':'库存调节（5户）','trend':'趋势短线（2户）',
        'short_reversion':'回归短线（2户）','noise_short':'噪声偏重短线（1户）'}


def stats(a):
    a=np.asarray(a,dtype=float)
    return dict(n=len(a),mean=float(a.mean()),median=float(np.median(a)),p90=float(np.quantile(a,.9)),max=float(a.max())) if len(a) else None


def main():
    hashes={};per_seed={};rows=[];all_arrival_gaps={name:[] for name in GROUPS};all_trade_gaps={name:[] for name in GROUPS}
    flow_blocks={name:[] for name in GROUPS}
    for seed in SEEDS:
        trace=SOURCE/f'seed{seed}'/'trace.npz'
        hashes[str(trace.relative_to(ROOT))]=hashlib.sha256(trace.read_bytes()).hexdigest()
        with np.load(trace) as z:x={k:z[k] for k in z.files}
        # Recreate only independent arrival RNG draws using the recorded
        # pre-tick intensity, then verify every group count against the trace.
        counts=np.zeros((10001,20),dtype=int)
        for actor in range(20):
            rng=np.random.default_rng(seed*1000003+actor*101+1)
            counts[1:,actor]=rng.poisson(x['intensity'][1:]/20)
        for key,ids in [('random',range(10)),('reversion',range(10,15)),('factor',range(15,20))]:
            np.testing.assert_array_equal(counts[:,list(ids)].sum(axis=1),x[f'{key}_arrivals'])
        assert (x['account_trades']<=counts).all()
        diff=np.diff(x['account_foreign'],axis=0)
        np.testing.assert_allclose(abs(diff[:,10:15]).sum(axis=1),x['reversion_executed_abs_quantity'][1:],atol=1e-8,rtol=0)
        np.testing.assert_allclose(abs(diff[:,15:]).sum(axis=1),x['factor_executed_abs_quantity'][1:],atol=1e-8,rtol=0)
        assert x['account_trades'][:,10:].max()<=1
        minutes=9640/12;d={}
        total_qty=x['executed_abs_quantity'][361:].sum();total_trades=x['trades'][361:].sum()
        for name,ids in GROUPS.items():
            arrivals=counts[361:,ids].sum();trades=x['account_trades'][361:,ids].sum()
            qty=x['random_executed_abs_quantity'][361:].sum() if name=='random' else abs(diff[360:,ids]).sum()
            gaps_a=[];gaps_t=[]
            for actor in ids:
                ta=np.repeat(np.arange(361,10001),counts[361:,actor]);tt=np.repeat(np.arange(361,10001),x['account_trades'][361:,actor].astype(int))
                gaps_a.extend(np.diff(ta)/12);gaps_t.extend(np.diff(tt)/12)
            all_arrival_gaps[name].extend(gaps_a);all_trade_gaps[name].extend(gaps_t)
            net=diff[360:,ids].sum(axis=1);width=360;n=len(net)//width;blocks=net[:n*width].reshape(n,width).sum(axis=1)
            flow_blocks[name].extend(blocks)
            np.testing.assert_allclose(net.sum(),(x['account_foreign'][-1,ids]-x['account_foreign'][360,ids]).sum(),atol=1e-8,rtol=0)
            d[name]=dict(accounts=len(ids),arrivals=int(arrivals),trades=int(trades),
                arrivals_per_minute=float(arrivals/minutes),arrivals_per_account_minute=float(arrivals/minutes/len(ids)),
                trades_per_minute=float(trades/minutes),trades_per_account_minute=float(trades/minutes/len(ids)),
                trade_per_arrival=float(trades/arrivals),quantity_per_minute=float(qty/minutes),
                mean_trade_size=float(qty/trades),volume_share=float(qty/total_qty),trade_share=float(trades/total_trades),
                within_account_arrival_gap_minutes=stats(gaps_a),within_account_trade_gap_minutes=stats(gaps_t),
                net_flow30_std=float(np.std(blocks,ddof=1)),net_flow30_abs_median=float(np.median(abs(blocks))),
                maximum_observed_abs_group_net_position=float(abs(x['account_foreign'][361:,ids].sum(axis=1)-1000*len(ids)).max()))
            rows.append(dict(seed=seed,group=name,**{k:v for k,v in d[name].items() if not isinstance(v,dict)}))
        np.testing.assert_allclose(sum(v['volume_share'] for v in d.values()),1,atol=1e-12)
        per_seed[str(seed)]=d
    means={}
    for name in GROUPS:
        values=[per_seed[str(s)][name] for s in SEEDS]
        means[name]={k:float(np.mean([d[k] for d in values])) for k in values[0] if not isinstance(values[0][k],dict)}
        means[name]['pooled_within_account_arrival_gap_minutes']=stats(all_arrival_gaps[name])
        means[name]['pooled_within_account_trade_gap_minutes']=stats(all_trade_gaps[name])
    correlations={name:float(np.corrcoef(flow_blocks['random'],flow_blocks[name])[0,1]) for name in GROUPS if name!='random'}
    result=dict(seed_equal_averages=means,per_seed=per_seed,
        descriptive_same_window_net_flow_correlations_with_random=correlations,
        structural_capacity=dict(trend_group_accounts=2,episode_quantity=180,
            max_abs_trend_net_foreign=360,inventory_group_accounts=5,per_account_target_cap=450,
            max_abs_inventory_group_target=2250,
            note='Target/position capacity is not realized trading force. Short positions return to baseline at episode end.'))
    (OUT/'metrics.json').write_text(json.dumps(result,indent=2)+'\n')
    with (OUT/'groups.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=rows[0]);writer.writeheader();writer.writerows(rows)
    (OUT/'manifest.json').write_text(json.dumps(dict(seeds=SEEDS,random_sigma=108,first_trade_tick=361,last_tick=10000,
        groups=GROUPS,source_sha256=hashes,
        methods=['Arrival RNG recreated from recorded pre-tick intensity and checked exactly against group arrival counts.',
            'Random gross volume uses exact trace field; per-actor close differences could hide same-tick round trips.',
            'Other actors execute at most once per tick, so foreign inventory differences give exact gross executed quantities.',
            'Gap summaries pool only within-account/within-seed intervals; observed boundary waits excluded.'],
        caveats=['Three existing research seeds, no independent validation or player comparison.',
            'Same-window flow correlation is descriptive, not causal attribution.','No parameter changes or new simulations.']),indent=2)+'\n')
    plot(means)
    for path,digest in hashes.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    (OUT/'audit.json').write_text(json.dumps(dict(arrivals_exactly_reconstructed=True,trade_counts_bounded_by_arrivals=True,
        strategy_gross_volume_matches_trace=True,group_volume_shares_sum_to_one=True,net_flow_endpoint_identity_verified=True,
        source_traces_unchanged=True,new_simulations=0),indent=2)+'\n')
    print(json.dumps(result,indent=2),flush=True)


def plot(means):
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
    names=list(GROUPS);labels=[LABELS[n] for n in names];ys=np.arange(5)
    fig,axes=plt.subplots(1,3,figsize=(16,5.5),layout='constrained')
    axes[0].barh(ys-.17,[means[n]['arrivals_per_account_minute'] for n in names],height=.32,color='#b27a16',label='看盘到达')
    axes[0].barh(ys+.17,[means[n]['trades_per_account_minute'] for n in names],height=.32,color='#176997',label='实际成交')
    axes[0].set(title='每户频次：看盘不等于下单',xlabel='次数／分钟',yticks=ys,yticklabels=labels);axes[0].legend(fontsize=8)
    axes[1].barh(ys,[means[n]['quantity_per_minute'] for n in names],color='#176997')
    axes[1].set(title='各组实际成交绝对量',xlabel='外币／分钟',yticks=ys,yticklabels=labels)
    axes[2].barh(ys,[means[n]['trade_per_arrival']*100 for n in names],color='#176997')
    axes[2].set(title='看盘后实际成交的比例',xlabel='成交次数／到达次数（%）',yticks=ys,yticklabels=labels,xlim=(0,100))
    for ax in axes:ax.invert_yaxis();ax.grid(axis='x',alpha=.18);ax.set_axisbelow(True)
    fig.suptitle('浅池慢候选：随机订单σ＝108，三个原种子，剔除启动30分钟',fontsize=14)
    fig.savefig(OUT/'frequency-and-volume.png',dpi=150);plt.close(fig)


if __name__=='__main__':main()
