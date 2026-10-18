"""Frozen baseline, half pool only; three paired seeds, no shocks.
Run: python output/fx-half-pool-20261016/run.py
"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import csv
import hashlib
import json
import runpy
import time
from decimal import Decimal
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from research.fx.reversion.repeated.entry_model import simulate
from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import clean_json
from backend.app.services.fx.amm import quote_buy

OUT=Path(__file__).resolve().parent
PREVIOUS=ROOT/'output/fx-calibration-20261016'
ANALYZE=runpy.run_path(str(PREVIOUS/'run.py'))['analyze']
SEEDS=[20261024,20261025,20261026]


def check(x,s,cfg):
    assert not s['events'] and not x['injection_gold'].any() and not x['injection_foreign'].any()
    assert all(np.isfinite(a).all() for a in x.values())
    assert x['account_gold'].min()>=0 and x['account_foreign'].min()>=0
    total_gold=cfg['pool_gold']+cfg['accounts']*cfg['initial_gold']
    total_foreign=cfg['pool_foreign']+cfg['accounts']*cfg['initial_foreign']
    np.testing.assert_allclose(x['gold']+x['account_gold'].sum(axis=1),total_gold,atol=1e-8,rtol=0)
    np.testing.assert_allclose(x['pool_foreign']+x['account_foreign'].sum(axis=1),total_foreign,atol=1e-8,rtol=0)
    np.testing.assert_allclose(x['account_wealth'],x['account_gold']+x['account_foreign']*x['prices'][:,None],atol=1e-8,rtol=0)
    np.testing.assert_array_equal(x['account_trades'].sum(axis=1),x['trades'])


def main():
    frozen_path=ROOT/'research/fx/reversion/repeated/FROZEN_CONFIG.json'
    frozen=json.loads(frozen_path.read_text())
    base={**frozen['research_reproduction']['selected_simulator_config'],**frozen['parameters']}
    protected=[frozen_path,ROOT/'research/fx/reversion/repeated/entry_model.py',
        ROOT/'research/fx/activity/model.py',ROOT/'research/fx/reversion/inventory.py',
        ROOT/'research/fx/episodes/model.py',ROOT/'research/fx/factor_activity/model.py']
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    old_manifest=json.loads((PREVIOUS/'manifest.json').read_text())
    for path,digest in hashes.items():assert old_manifest['protected_sha256'][path]==digest,path
    manifest=dict(seeds=SEEDS,baseline=base,overrides={'pool_gold':500000,'pool_foreign':100000},
        tick_seconds=5,ticks=10000,shocks={},fees=0,primary_exclusion_ticks=360,
        profiles=frozen['profiles'],activity_b=frozen['activity_b'],trend_multiplier=frozen['trend_multiplier'],
        baseline_reused_from=str(PREVIOUS.relative_to(ROOT)),protected_sha256=hashes,
        change_scope='Only initial pool reserves differ. No psi matching, profile, wallet or strategy changes.')
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    results={};traces={};rows=[]
    for name in ['baseline','half_pool']:
        results[name]={};traces[name]={}
        for seed in SEEDS:
            cfg={**base,'seeds':[seed]}
            if name=='baseline':
                src=PREVIOUS/'baseline'/f'seed{seed}'
                params=json.loads((src/'parameters.json').read_text())
                assert params['config']==cfg and params['profiles']==frozen['profiles']
                assert params['activity_b']==frozen['activity_b'] and params['trend_multiplier']==frozen['trend_multiplier']
                with np.load(src/'trace.npz') as z:x={k:z[k] for k in z.files}
                s=json.loads((src/'summary.json').read_text());episodes=json.loads((src/'trace-episodes.json').read_text())
                print('Reused baseline',seed,flush=True)
            else:
                cfg.update(pool_gold=500000,pool_foreign=100000)
                print('Running half pool',seed,flush=True);start=time.monotonic()
                x,s,episodes,fills,_,_=simulate(cfg,frozen['profiles'],5,frozen['activity_b'],seed,{},frozen['policy'],
                    {'trend_multiplier':frozen['trend_multiplier']})
                folder=OUT/name/f'seed{seed}';folder.mkdir(parents=True,exist_ok=True)
                save_trace(folder,'trace',x,episodes,fills)
                (folder/'summary.json').write_text(json.dumps(clean_json(s),indent=2)+'\n')
                print('Finished',seed,'seconds',round(time.monotonic()-start,2),flush=True)
            check(x,s,cfg)
            d=ANALYZE(x,episodes,s)
            d.update(mean_arrivals_per_minute=float(x['arrivals'][361:].sum()/(9640/12)),
                mean_intensity=float(x['intensity'][361:].mean()),peak_intensity=float(x['intensity'][361:].max()),
                fraction_zero_trade_ticks=float(np.mean(x['trades'][361:]==0)))
            results[name][str(seed)]=d;traces[name][seed]=x
            rows.append(dict(variant=name,seed=seed,range30=d['range30']['median'],range60=d['range60']['median'],
                abs_net30=d['abs_net30']['median'],cycle05=d['swings']['0.005']['cycles_minutes']['median'],
                cycle1=d['swings']['0.01']['cycles_minutes']['median'],
                amplitude05=d['swings']['0.005']['amplitude']['median'],
                trades_per_minute=d['trades_per_minute'],mean_intensity=d['mean_intensity'],peak_intensity=d['peak_intensity'],
                clipped_fraction=d['clipped_fraction'],executed_quantity_fraction=d['executed_quantity_fraction'],
                cash_saturated_arrivals=d['cash_saturated_arrivals'],foreign_saturated_arrivals=d['foreign_saturated_arrivals'],
                holding_median=d['completed_holding_minutes']['median'],net_change=d['net_change'],max_abs_displacement=d['max_abs_displacement'],
                short_realized_per_account=d['short_realized_per_account']))
    quotes=[]
    for amount in [500,1500,2500,5000]:
        for name,g,f in [('baseline',1000000,200000),('half_pool',500000,100000)]:
            q=quote_buy(Decimal(amount),Decimal(g),Decimal(f),Decimal(0))
            quotes.append(dict(pool=name,gold_in=amount,post_price_change=float(q.post_price/Decimal(5)-1),
                average_execution_price_deviation=float(Decimal(amount)/q.output_amount/Decimal(5)-1)))
    (OUT/'metrics.json').write_text(json.dumps(clean_json(results),indent=2)+'\n')
    (OUT/'player-quotes.json').write_text(json.dumps(quotes,indent=2)+'\n')
    with (OUT/'comparison.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=rows[0]);writer.writeheader();writer.writerows(rows)
    averages={name:{k:float(np.mean([r[k] for r in rows if r['variant']==name])) for k in rows[0] if k not in ['variant','seed']} for name in results}
    ratios=[]
    for seed in SEEDS:
        b=results['baseline'][str(seed)];c=results['half_pool'][str(seed)]
        ratios.append(dict(seed=seed,range30=c['range30']['median']/b['range30']['median'],
            same_threshold_cycle05=c['swings']['0.005']['cycles_minutes']['median']/b['swings']['0.005']['cycles_minutes']['median'],
            doubled_threshold_amplitude=c['swings']['0.01']['amplitude']['median']/b['swings']['0.005']['amplitude']['median'],
            doubled_threshold_cycle=c['swings']['0.01']['cycles_minutes']['median']/b['swings']['0.005']['cycles_minutes']['median']))
    (OUT/'summary.json').write_text(json.dumps(dict(seed_equal_averages=averages,paired_ratios=ratios),indent=2)+'\n')
    plot(traces)
    for path,digest in hashes.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    (OUT/'audit.json').write_text(json.dumps(dict(reused_baselines=3,new_half_pool_paths=3,
        baseline_parameters_and_source_hashes_verified=True,no_shocks=True,wallets_nonnegative=True,
        all_series_finite=True,assets_conserved_with_actual_pool_sizes=True,wallet_values_verified=True,
        trade_counts_verified=True,frozen_sources_unchanged=True),indent=2)+'\n')
    print(json.dumps(dict(averages=averages,paired_ratios=ratios),indent=2),flush=True)


def plot(traces):
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
    for tail,length,title in [('1000ticks',1001,'前1000tick（83分20秒）'),('full',10001,'完整10000tick（13小时53分20秒）')]:
        fig,axes=plt.subplots(3,2,figsize=(15,11),layout='constrained')
        for i,seed in enumerate(SEEDS):
            vals=[traces[name][seed]['prices'][:length] for name in ['baseline','half_pool']]
            low=min(v.min() for v in vals);high=max(v.max() for v in vals);pad=(high-low)*.07
            for j,(name,label,color) in enumerate([('baseline','100万／20万','#176997'),('half_pool','50万／10万','#b27a16')]):
                x=traces[name][seed];ax=axes[i,j]
                at=np.arange(length) if tail=='1000ticks' else np.arange(length)/720
                ax.plot(at,x['prices'][:length],lw=1,color=color,label='池价')
                ax.plot(at[1:],x['slow_reference'][1:length],lw=.9,ls='--',color='#555',label='30分钟库存参考价')
                ax.set(title=f'seed {seed}：{label}',ylabel='金圆券／外币',xlabel='tick（5秒）' if tail=='1000ticks' else '小时',
                    ylim=(low-pad,high+pad),xlim=(at[0],at[-1]))
                ax.axhline(5,lw=.6,color='#777');ax.grid(alpha=.18);ax.legend(fontsize=8)
        fig.suptitle('仅池深减半，baseline机器人参数不变：'+title+'\n每行价格坐标一致；无外部冲击',fontsize=14)
        fig.savefig(OUT/f'comparison-{tail}.png',dpi=150);plt.close(fig)


if __name__=='__main__':main()
