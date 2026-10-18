"""Round 4: fixed-reference horizon variants and matched raw-noise decomposition."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from .diagnostics import acf
from .impact_study import WIDTHS, load_run, persistence, inventory_horizons
from .response_report import inspect
from .switching import Agent
from .switching_run import read_config, run_one, simulate

ROOT=Path(__file__).resolve().parent
SEEDS=[41,42,43]
CASES=['short30','hom240','hom1920','mixed']


def overlap_bias(ticks,width):
    count=ticks-width+1
    if not 1<=width<count:
        raise ValueError('reference formula requires 1<=width<M')
    n=width-1
    summed=count*width*n-(count+width)*n*(n+1)//2+n*(n+1)*(2*n+1)//6
    return 1-2*summed/(count*(count-1)*width)


def noise_decomposition(x,burn,nu):
    raw=np.cumsum(x['noise_demand'])[burn:]
    actual=x['inventory_noise'][burn:]
    error=actual-raw
    ticks=len(raw)-1
    rows=[]
    for width in WIDTHS:
        dr,de,da=raw[width:]-raw[:-width],error[width:]-error[:-width],actual[width:]-actual[:-width]
        vr,ve,va=float(dr.var(ddof=1)),float(de.var(ddof=1)),float(da.var(ddof=1))
        cov=float(2*np.cov(dr,de,ddof=1)[0,1])
        np.testing.assert_allclose(va,vr+ve+cov,rtol=1e-11,atol=1e-7)
        bias=overlap_bias(ticks,width)
        rows.append(dict(width=width,samples=len(dr),raw_var=vr,error_var=ve,two_cov=cov,actual_var=va,
                         ideal_population_var=nu*width,finite_sample_expectation=nu*width*bias,
                         finite_sample_ratio=bias,raw_over_expected=vr/(nu*width*bias),
                         actual_over_raw=va/vr))
    return rows


def analyze_old_noise(output):
    previous=ROOT/'results/switching-v2'
    rows=[]
    for mode in ['fixed_m4','fixed','fixed_m12','random','performance','matched_noise','noise']:
        for seed in SEEDS:
            directory=previous/f'{mode}-seed{seed}'
            result,x=load_run(directory)
            nu=sum(a['kind']=='N' for a in result['simulation']['accounts'])*.05*40**2
            rows.append(dict(mode=mode,seed=seed,source=str(directory/'series.npz'),
                source_sha256=hashlib.sha256((directory/'series.npz').read_bytes()).hexdigest(),
                curves=noise_decomposition(x,result['config']['burn_in'],nu)))
    (output/'noise-analysis.json').write_text(json.dumps(dict(runs=rows),indent=2)+'\n')
    print('Old 21 paths: matched raw/execution-error noise covariance decomposition complete',flush=True)


def replay_short_signals(result,x):
    cfg={**result['config'],'normalize_signals':True}
    history=np.asarray([math.log(p) for p in x['prices']])
    rows={}
    for kind,index in [('M',0),('R',1)]:
        agent=Agent(0,kind,False,result['seed'],cfg,x['prices'][0])
        values=[]
        for tick in range(1,len(history)):
            agent.observe(history[tick-1],history[:tick])
            if tick>cfg['burn_in']:values.append(agent.signals[index])
        values=np.asarray(values)
        rows[f'{kind}:30']=dict(samples=8*len(values),raw_mean=float(values.mean()),raw_sd=float(values.std()),
            normalized_mean=float(values.mean()),normalized_sd=float(values.std()),strong_fraction=float(np.mean(abs(values)>cfg['entry_threshold'])))
    return rows


def analyze_case(case,result,x):
    burn=result['config']['burn_in']
    row,_,_=inspect(result,x)
    p=persistence(x,burn)
    diag=result['diagnostics']
    for lo,hi in [(101,300),(301,1000),(1001,5000)]:
        row[f'abs_acf_{lo}_{hi}']=float(np.mean(diag['absolute_acf'][lo:hi+1]))
        row[f'squared_acf_{lo}_{hi}']=float(np.mean(diag['squared_acf'][lo:hi+1]))
        row[f'W_acf_{lo}_{hi}']=p['W_acf_bands'][f'{lo}-{hi}']
    for block in p['blocks']:
        row[f'next_block_{block["width"]}']=block['next_block_corr']
        row[f'same_block_{block["width"]}']=block['same_block_corr']
    row['direction_max_abs_1_1000']=float(np.max(abs(np.asarray(diag['signed_acf'][1:1001]))))
    row['strategy_variance_share']=row['strategy_impact_variance']/row['actual_return_variance']
    row['case']=case
    row['persistence_screen']=bool(all(row[f'{stat}_acf_{lo}_{hi}']>=.01
                                      for stat in ['abs','squared','W'] for lo,hi in [(101,300),(301,1000)])
                                   and row['next_block_100']>=.1 and row['next_block_240']>=.1)
    signal_stats=result['simulation'].get('signal_statistics')
    if signal_stats is None:signal_stats=replay_short_signals(result,x)
    return dict(metrics=row,W=p,inventory=inventory_horizons(x,burn),signal_statistics=signal_stats,
                correlations={k:diag[k] for k in ['signed_acf','absolute_acf','squared_acf']})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'results/horizon-v4')
    parser.add_argument('--workers',type=int,default=3)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    analyze_old_noise(args.output)
    cfg={**read_config(),'position_cap':250,'normalize_signals':True}
    smoke,_=simulate({**cfg,'ticks':1000,'burn_in':50},'fixed',41,0)
    baseline=ROOT/'results/impact-v3/a250'
    with np.load(baseline/'fixed-seed41/series.npz') as old:
        for key in old.files:np.testing.assert_array_equal(smoke[key],old[key][:1001])
    print('Normalized 30/30 exactly preserves all saved A250 fields over 1000-tick prefix',flush=True)
    variants=dict(hom240={**cfg,'trend_window':240,'mean_window':240},
                  hom1920={**cfg,'trend_window':1920,'mean_window':1920},
                  mixed={**cfg,'window_mix':[30,120,480,1920]})
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        list(executor.map(run_one,[(variants[case],'fixed',seed,0,str(args.output/case))
                          for case in CASES[1:] for seed in SEEDS]))
    analyses=[];provenance=[]
    for case in CASES:
        for seed in SEEDS:
            directory=(baseline if case=='short30' else args.output/case)/f'fixed-seed{seed}'
            result,x=load_run(directory)
            assert result['simulation']['conservation'] and result['simulation']['switches']==0
            with np.load(baseline/f'fixed-seed{seed}/series.npz') as old:
                for key in ['arrivals','noise_demand']:np.testing.assert_array_equal(x[key],old[key])
            analysis=analyze_case(case,result,x)
            analyses.append(analysis)
            provenance.append(dict(case=case,seed=seed,path=str(directory),
                              series_sha256=hashlib.sha256((directory/'series.npz').read_bytes()).hexdigest()))
    rows=[a['metrics'] for a in analyses]
    means={case:{key:float(np.mean([r[key] for r in rows if r['case']==case]))
                 for key in rows[0] if key not in ['case','mode','seed','persistence_screen']
                 and all(r[key] is not None for r in rows if r['case']==case)} for case in CASES}
    (args.output/'analysis.json').write_text(json.dumps(dict(runs=analyses),indent=2)+'\n')
    (args.output/'summary.json').write_text(json.dumps(dict(means=means,runs=rows,provenance=provenance,
        validation=dict(new_paths=9,baseline_paths_reused=3,paired_arrivals_noise=True,
                        reference_30_identity=True,normalized_30_prefix_1000_identical=True),
        analysis_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()),indent=2)+'\n')
    for case in CASES:
        print(case,json.dumps({key:means[case][key] for key in ['return_sd','signed_acf1','abs_acf_101_300',
                  'abs_acf_301_1000','W_acf_301_1000','next_block_100','next_block_240','strategy_variance_share']}),flush=True)


if __name__=='__main__':main()
