"""Round 3: existing inventory horizons/W persistence, then only six smaller-A runs."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from .diagnostics import acf
from .response_report import block_mean, correlation, table
from .switching import Agent, target_observations
from .switching_run import read_config, run_one, simulate

ROOT = Path(__file__).resolve().parent
WIDTHS = [1,5,10,30,60,100,240,500,1000,2000,4000,8192,16000]
MODES = ['fixed_m4','fixed','fixed_m12','random','performance','matched_noise','noise']
LABELS = ['4M/12R','8M/8R','12M/4R','random','B','4N matched','20N']
SEEDS = [41,42,43]


def load_run(directory):
    result = json.loads((directory/'result.json').read_text())
    with np.load(directory/'series.npz') as raw:
        series = {key:raw[key] for key in raw.files}
    return result, series


def inventory_horizons(x, burn, widths=WIDTHS, noise_variance=320):
    s, n = x['inventory_strategy'][burn:], x['inventory_noise'][burn:]
    p = np.log(x['prices'][burn:])
    rows = []
    for width in widths:
        ds, dn = s[width:]-s[:-width], n[width:]-n[:-width]
        vs, vn = float(ds.var(ddof=1)), float(dn.var(ddof=1))
        twice_cov = float(2*np.cov(ds,dn,ddof=1)[0,1])
        total = float((ds+dn).var(ddof=1))
        np.testing.assert_allclose(total,vs+vn+twice_cov,rtol=1e-12,atol=1e-8)
        rows.append(dict(width=width,samples=len(ds),strategy_var=vs,noise_var=vn,two_cov=twice_cov,
                        net_inventory_var=total,actual_return_var=float((p[width:]-p[:-width]).var(ddof=1)),
                        linear_return_var=(2/x['pool_foreign'][0])**2*total,
                        ideal_noise_ratio=vn/(noise_variance*width) if noise_variance>0 else None))
    return rows


def persistence(x, burn):
    w = x['predicted_second_moment'][burn+1:]
    r2 = np.diff(np.log(x['prices']))[burn:]**2
    curve = acf(w,5000)
    blocks = []
    for width in [30,100,240,1000]:
        pw, actual = block_mean(w,width), block_mean(r2,width)
        blocks.append(dict(width=width,complete_blocks=len(pw),next_pairs=len(pw)-1,
                           same_block_corr=correlation(pw,actual),next_block_corr=correlation(pw[:-1],actual[1:])))
    return dict(W_acf=curve.tolist(), blocks=blocks,
        drift_share=float(np.mean(x['predicted_mean'][burn+1:]**2)/np.mean(w)),
        W_acf_bands={f'{lo}-{hi}':float(np.mean(curve[lo:hi+1])) for lo,hi in
                      [(1,1),(2,10),(11,30),(31,100),(101,300),(301,1000),(1001,5000)]})


def existing_analysis(previous, output):
    rows = []
    for mode in MODES:
        for seed in SEEDS:
            directory = previous/f'{mode}-seed{seed}'
            result,x = load_run(directory)
            cfg=result['config']
            noise_variance=sum(a['kind']=='N' for a in result['simulation']['accounts'])*cfg['rate']*(cfg['noise_gold_size']/x['prices'][0])**2
            rows.append(dict(mode=mode,seed=seed,burn_in=result['config']['burn_in'],
                source_series=str(directory/'series.npz'),
                source_sha256=hashlib.sha256((directory/'series.npz').read_bytes()).hexdigest(),
                inventory=inventory_horizons(x,result['config']['burn_in'],noise_variance=noise_variance),
                persistence=persistence(x,result['config']['burn_in'])))
    (output/'existing-analysis.json').write_text(json.dumps(dict(runs=rows),indent=2)+'\n')
    print('Existing 21 paths: inventory covariance identities and W analyses complete',flush=True)
    return rows


def replay_targets(result, x):
    """Replay only fixed-type signals/states against historical prices; no orders."""
    cfg,seed = result['config'],result['seed']
    assert result['mode']=='fixed' and cfg['type_counts']==[8,8,4]
    agents = [Agent(i,'M' if i<8 else 'R' if i<16 else 'N',False,seed,cfg,x['prices'][0]) for i in range(20)]
    n=len(x['prices'])-1
    schedules=[np.random.default_rng(seed*1000003+i*101+1).poisson(cfg['rate'],n) for i in range(20)]
    np.testing.assert_array_equal(np.sum(schedules,axis=0),x['arrivals'][1:])
    history=np.asarray([math.log(price) for price in x['prices']])
    previous=[0]*20
    recorded={key:np.zeros(n+1) for key in
              ['signal_strong_M','signal_strong_R','candidate_changes_M','candidate_changes_R']}
    for tick in range(1,n+1):
        for agent in agents[:16]:
            agent.observe(history[tick-1],history[:tick])
        for key,value in target_observations(agents,previous).items():
            recorded[key][tick]=value
        # With one public signal per tick, this transition is idempotent;
        # execution order and multiple wakes cannot change the resulting z.
        for i,agent in enumerate(agents[:16]):
            if schedules[i][tick-1]>0:
                agent.state=agent.candidate_state()
    return recorded


def amplitude_metrics(amplitude,result,x,observations):
    burn=result['config']['burn_in']; post=slice(burn+1,None)
    p=persistence(x,burn); diag=result['diagnostics']
    row=dict(amplitude=amplitude,seed=result['seed'],chi=2*.2*amplitude/(200000*.0005),
        return_sd=diag['returns']['std'],signed_acf1=diag['signed_acf'][1],
        abs_acf_101_1000=float(np.mean(diag['absolute_acf'][101:1001])),
        abs_dfa_512_8192=diag['dfa']['absolute_returns']['fits']['512-8192']['alpha'],
        return_dfa_512_8192=diag['dfa']['returns']['fits']['512-8192']['alpha'],
        W_acf_101_300=p['W_acf_bands']['101-300'],W_acf_301_1000=p['W_acf_bands']['301-1000'],
        W_same_block_100=p['blocks'][1]['same_block_corr'],W_next_block_100=p['blocks'][1]['next_block_corr'],
        strategy_gold_volume_per_tick=float((x['volume_M'][post]+x['volume_R'][post]).mean()),
        strategy_flow_variance=float(x['strategy_flow'][post].var(ddof=1)),
        noise_flow_variance=float(x['noise_flow'][post].var(ddof=1)),
        switches=result['simulation']['switches'],demand_mean=float(x['demand_D'][post].mean()))
    for kind in ['M','R']:
        row[f'strong_{kind}']=float(observations[f'signal_strong_{kind}'][post].mean()/8)
        row[f'candidate_changes_{kind}']=float(observations[f'candidate_changes_{kind}'][post].mean()/8)
    return row,p


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous',type=Path,default=ROOT/'results/switching-v2')
    parser.add_argument('--output',type=Path,default=ROOT/'results/impact-v3')
    parser.add_argument('--workers',type=int,default=3)
    args=parser.parse_args(); args.output.mkdir(parents=True,exist_ok=True)
    existing=existing_analysis(args.previous,args.output)
    cfg={**read_config(),'initial_gold':5000,'base_foreign':1000}
    # Historical A500 replay is a diagnostic, not a seventh/new market path.
    smoke_cfg={**cfg,'ticks':1000,'burn_in':50}
    smoke,smoke_summary=simulate(smoke_cfg,'fixed',41,0)
    old_result,old=load_run(args.previous/'fixed-seed41')
    for key in old:
        np.testing.assert_array_equal(smoke[key],old[key][:1001])
    smoke_record=dict(mode='fixed',seed=41,config=smoke_cfg)
    replayed=replay_targets(smoke_record,smoke)
    for key in replayed:
        np.testing.assert_array_equal(replayed[key],smoke[key])
    print('1000-tick A500 legacy prefix unchanged; target-only replay matches live observations',flush=True)
    tasks=[({**cfg,'position_cap':a},'fixed',seed,0,str(args.output/f'a{a}')) for a in [125,250] for seed in SEEDS]
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        new=list(executor.map(run_one,tasks))
    rows,loaded,curves=[],{},{}
    for amplitude in [125,250,500]:
        for seed in SEEDS:
            directory=(args.previous if amplitude==500 else args.output/f'a{amplitude}')/f'fixed-seed{seed}'
            result,x=load_run(directory)
            if amplitude==500:
                observations=replay_targets(result,x)
                np.savez_compressed(args.output/f'a500-target-replay-seed{seed}.npz',**observations)
            else:
                observations=x
            with np.load(args.previous/f'fixed-seed{seed}/series.npz') as baseline:
                for key in ['arrivals','noise_demand']:
                    np.testing.assert_array_equal(x[key],baseline[key])
            assert result['simulation']['conservation'] and result['simulation']['switches']==0
            row,p=amplitude_metrics(amplitude,result,x,observations)
            rows.append(row); loaded[amplitude,seed]=x; curves[amplitude,seed]=p
    means={str(a):{key:float(np.mean([r[key] for r in rows if r['amplitude']==a]))
                  for key in rows[0] if key not in ['amplitude','seed']} for a in [125,250,500]}
    (args.output/'summary.json').write_text(json.dumps(dict(means=means,runs=rows,
        validation=dict(new_paths=6,baseline_market_paths_reused=3,paired_arrivals_noise=True,
                        wallet_gold=5000,base_foreign=1000,A500_prefix_1000_identical=True,
                        target_replay_prefix_matches=True),
        analysis_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()),indent=2)+'\n')
    fig,axes=plt.subplots(1,3,figsize=(15,4),constrained_layout=True)
    for mode,label,color in zip(['fixed_m4','fixed','fixed_m12','performance'],['4M/12R','8M/8R','12M/4R','B'],
                               ['#9333ea','#64748b','#d97706','#2563eb']):
        selected=[r for r in existing if r['mode']==mode]
        vs=np.mean([[v['strategy_var'] for v in r['inventory']] for r in selected],axis=0)
        vn=np.mean([[v['noise_var'] for v in r['inventory']] for r in selected],axis=0)
        cov=np.mean([[v['two_cov'] for v in r['inventory']] for r in selected],axis=0)
        axes[0].loglog(WIDTHS,vs,'o-',lw=.8,label=label,color=color)
        axes[1].loglog(WIDTHS,vn,'o-',lw=.8,label=label,color=color)
        axes[2].semilogx(WIDTHS,cov,'o-',lw=.8,label=label,color=color)
    axes[1].loglog(WIDTHS,320*np.asarray(WIDTHS),'k--',lw=.8,label='Ideal 320*w')
    for ax,title in zip(axes,['Strategy inventory-difference variance','Noise inventory-difference variance','Twice cross-horizon covariance']):
        ax.set(title=title,xlabel='Horizon (ticks)');ax.legend(fontsize=7)
    fig.savefig(args.output/'inventory-horizons.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(1,3,figsize=(15,4),constrained_layout=True)
    for a,color in zip([125,250,500],['#16a34a','#d97706','#2563eb']):
        for seed in SEEDS:
            x=loaded[a,seed]
            w=curves[a,seed]['W_acf']
            r=np.diff(np.log(x['prices']))[5000:]
            axes[0].semilogx(np.arange(1,1001),w[1:1001],lw=.6,alpha=.7,color=color,
                            label=f'A={a}' if seed==41 else None)
            axes[1].semilogx(np.arange(1,1001),acf(abs(r),1000)[1:],lw=.6,alpha=.7,color=color)
            blocks=curves[a,seed]['blocks']
            axes[2].plot([b['width'] for b in blocks],[b['next_block_corr'] for b in blocks],
                         'o-',lw=.7,alpha=.7,color=color)
    for ax,title in zip(axes,['Full predicted second moment W ACF','Absolute-return ACF','Current W block vs next squared-return block']):
        ax.set(title=title,xlabel='Ticks');ax.axhline(0,lw=.6,color='#94a3b8')
    axes[0].legend(fontsize=8);axes[2].set_xscale('log')
    fig.savefig(args.output/'amplitude-persistence.png',dpi=150);plt.close(fig)
    for a in [125,250,500]:
        print(f'A={a}: '+json.dumps(means[str(a)]),flush=True)


if __name__=='__main__':
    main()
