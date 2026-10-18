"""Exploratory 10 random / 3 inventory / 3 reversion / 4 trend comparison.

Run from repository root: python output/fx-role-mix-20261016/run.py
Only output artifacts change. Existing research and production defaults stay intact.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import hashlib
import json
import runpy
from collections import Counter
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import clean_json

OUT = Path(__file__).resolve().parent
SIM = runpy.run_path(str(OUT/'model.py'))['simulate']
ANALYZE = runpy.run_path(str(ROOT/'output/fx-calibration-20261016/run.py'))['analyze']
CHECK = runpy.run_path(str(ROOT/'output/fx-half-pool-20261016/run.py'))['check']
SOURCE = ROOT/'output/fx-shallow-calibration-20261016'
SEEDS = [20261029, 20261030, 20261031]
GROUPS = {
    'baseline': {'random': list(range(10)), 'inventory': list(range(10,15)),
                 'reversion': [15,17], 'trend': [16,18], 'noise_short': [19]},
    'candidate': {'random': list(range(10)), 'inventory': [10,11,12],
                  'reversion': [13,14,15], 'trend': [16,17,18,19]},
}


def write(path, value):
    path.write_text(json.dumps(clean_json(value), ensure_ascii=False, indent=2)+'\n')


def metrics(x, episodes, summary, name, decisions):
    m = ANALYZE(x, episodes, summary)
    groups = GROUPS[name]
    short_ids = [i for g,ids in groups.items() if g not in ('random','inventory') for i in ids]
    m['short_realized_per_account'] = float(summary['behavior']['realized_pnl'])/len(short_ids)
    hours_minutes = (len(x['prices'])-361)/12
    m['groups'] = {}
    for group, ids in groups.items():
        # Random can round-trip within one tick: use its exact aggregate trace.
        volume = (float(x['random_executed_abs_quantity'][361:].sum()) if group=='random'
                  else float(np.abs(np.diff(x['account_foreign'][360:,ids],axis=0)).sum()))
        trades = float(x['account_trades'][361:,ids].sum())
        entries = [d for d in decisions if d['account'] in ids and d['tick']>=361 and d['action']=='open']
        dominant = Counter(d['dominant'] for d in entries)
        m['groups'][group] = dict(accounts=len(ids), gross_foreign=volume,
            trades_per_minute=trades/hours_minutes,
            final_mtm_vs_hold_per_account=float(np.mean(x['account_wealth'][-1,ids])-(5000+1000*x['prices'][-1])),
            entry_dominance=dict(dominant), analyzed_entries=len(entries))
    p = x['prices'][360:]
    # Ratio of net movement to cumulative movement, in disjoint 5-minute windows.
    efficiencies = []
    for start in range(0,len(p)-60,60):
        r = np.diff(np.log(p[start:start+61]))
        if abs(r).sum(): efficiencies.append(abs(r.sum())/abs(r).sum())
    m['efficiency5_median'] = float(np.median(efficiencies))
    return m


def main():
    candidate = json.loads((SOURCE/'candidate-a1.8_t2.json').read_text())
    cfg, original = candidate['parameters'], candidate['profiles']
    # Preserve original personal parameters; clone intended types with independent RNGs.
    origins = list(range(13))+[15,17,15,16,18,16,18]
    profiles = [{**original[src], 'account':i,
                 'role': next(g for g,ids in GROUPS['candidate'].items() if i in ids)}
                for i,src in enumerate(origins)]
    manifest = dict(seeds=SEEDS, config=cfg, activity_b=candidate['activity_b'],
        trend_multiplier=candidate['trend_multiplier'], candidate_profiles=profiles,
        source_profile_by_account=origins, groups=GROUPS,
        comparison='Latest shallow candidate a1.8_t2 at random sigma108; NOT remote production defaults.',
        scope='Only group composition and cloned personal profile assignments change. Existing signal noise retained.',
        caveats=['Three exploratory seeds; no players, shocks or fees.',
                 'Same seed does not ensure matched arrivals once endogenous activity differs.',
                 'Reversion/trend names describe intended bias, not guaranteed signal dominance.',
                 'Completed holding excludes unfinished episodes; window metrics exclude first 30 minutes.'],
        source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [SOURCE/'candidate-a1.8_t2.json', ROOT/'research/fx/reversion/repeated/entry_model.py', OUT/'model.py']})
    write(OUT/'manifest.json',manifest)
    traces = {}; result = {'baseline':{},'candidate':{}}
    for seed in SEEDS:
        src = SOURCE/'a1.8_t2'/f'seed{seed}'
        params = json.loads((src/'parameters.json').read_text())
        assert params['config']=={**cfg,'seeds':[seed]} and params['profiles']==original
        with np.load(src/'trace.npz') as z: baseline = {k:z[k] for k in z.files}
        summary = json.loads((src/'summary.json').read_text())
        episodes = json.loads((src/'trace-episodes.json').read_text())
        # Confirm the experimental layout fork reproduces the original path exactly.
        if seed==SEEDS[0]:
            print('Verifying unchanged-layout reproduction',seed,flush=True)
            reproduced = SIM({**cfg,'seeds':[seed]},original,5,candidate['activity_b'],seed,{},
                             'weak_trend',{'trend_multiplier':candidate['trend_multiplier']})
            for key in baseline: np.testing.assert_array_equal(baseline[key],reproduced[0][key])
        # Entry diagnostics from the earlier audited baseline path.
        decision_path = ROOT/f'output/fx-short-audit-20261016/decisions-seed{seed}.json'
        decisions = json.loads(decision_path.read_text())
        result['baseline'][str(seed)] = metrics(baseline,episodes,summary,'baseline',decisions)
        CHECK(baseline,summary,cfg)
        print('Running candidate',seed,flush=True)
        x,s,e,f,inv,d = SIM({**cfg,'seeds':[seed]},profiles,3,candidate['activity_b'],seed,{},
                           'weak_trend',{'trend_multiplier':candidate['trend_multiplier'],'episode_start':13})
        CHECK(x,s,cfg)
        folder = OUT/f'seed{seed}'; folder.mkdir(exist_ok=True)
        save_trace(folder,'trace',x,e,f)
        write(folder/'summary.json',s); write(folder/'decisions.json',d)
        result['candidate'][str(seed)] = metrics(x,e,s,'candidate',d)
        traces[seed] = {'baseline':baseline,'candidate':x}
        print('Finished',seed,flush=True)
    write(OUT/'metrics.json',result)
    keys = ['range30','range60','trades_per_minute','efficiency5_median','completed_holding_minutes','net_change']
    averages = {}
    for name,rows in result.items():
        averages[name] = {k:float(np.mean([m[k]['median'] if isinstance(m[k],dict) else m[k] for m in rows.values()])) for k in keys}
        for label in ['0.005','0.01']:
            for field in ['amplitude','cycles_minutes']:
                averages[name][f'swing{label}_{field}'] = float(np.mean([m['swings'][label][field]['median'] for m in rows.values()]))
    write(OUT/'averages.json',averages)
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
    for kind,start,end in [('1000ticks',360,1361),('full',0,10001)]:
        fig,axes = plt.subplots(3,2,figsize=(14,10),layout='constrained',sharex=True)
        for row,seed in enumerate(SEEDS):
            window = [traces[seed][name]['prices'][start:end] for name in ['baseline','candidate']]
            low,high = min(p.min() for p in window),max(p.max() for p in window)
            pad = (high-low)*.08
            for col,(name,label,color) in enumerate([('baseline','原组合：10噪声＋5库存＋5混合短线','#176997'),
                                                   ('candidate','候选：10噪声＋3库存＋3回归＋4趋势','#b27a16')]):
                x=traces[seed][name]; ax=axes[row,col]
                t=np.arange(start,end)
                ax.plot(t,x['prices'][start:end],color=color,lw=.9,label='池价')
                reference=x['slow_reference'][start:end].copy()
                if start==0: reference[0]=np.nan
                ax.plot(t,reference,color='#555555',ls='--',lw=.8,label='库存参考价')
                ax.set(title=f'{label}\nseed {seed}',ylabel='金币／外币',xlabel='tick（每tick 5秒）',
                       ylim=(low-pad,high+pad),xlim=(start,end-1))
                ax.grid(alpha=.18);ax.legend(fontsize=8,ncol=2)
        fig.suptitle(('热身后1000tick（第360～1360tick）' if kind=='1000ticks' else '完整10000tick')+
                     '；每行同一价格坐标；浅池50万／10万；随机σ108；无冲击、零费率',fontsize=12)
        fig.savefig(OUT/f'comparison-{kind}.png',dpi=145);plt.close(fig)
    write(OUT/'audit.json',dict(unchanged_layout_exact_reproduction=True,new_paths=3,reused_paths=3,
        finite_series=True,nonnegative_wallets=True,asset_conservation=True,wealth_and_trade_counts_verified=True,
        unchanged_source_hashes=all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in manifest['source_sha256'].items())))
    print(json.dumps(averages,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__': main()
