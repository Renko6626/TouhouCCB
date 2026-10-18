"""Fixed-version extension; never modifies the frozen simulator or its state."""
import argparse
import hashlib
import json
import math
from concurrent.futures import ProcessPoolExecutor, as_completed
from decimal import Decimal
from pathlib import Path

import numpy as np
from numpy.polynomial import Polynomial

from research.fx.common.analysis import state_diagnostics
from research.fx.common.diagnostics import acf, describe
from .model import linear_theory, load_config
from .run import ROOT, clean_json, run_one, source_hashes

HERE=Path(__file__).parent
OUTPUT=HERE/'results/robustness'


def freeze():
    manifest=HERE/'frozen-reference.json'
    if manifest.exists():
        data=json.loads(manifest.read_text())
        for name,digest in data['sha256'].items():
            if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=digest:
                raise AssertionError(f'frozen source/result changed: {name}')
        return data
    paths=[ROOT/name for name in source_hashes()]
    paths += [HERE/'configs/reference.json', HERE/'report.py']
    paths += sorted(p for p in (HERE/'results/reference').rglob('*') if p.is_file())
    data=dict(frozen_version='activity-reference-2026-10-15', sha256={
        str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})
    manifest.write_text(json.dumps(data,indent=2)+'\n')
    return data


def sixth_moment_theory(cfg, b=.25):
    """Expand in u=z², using compound-Poisson Gaussian conditional moments."""
    a=math.exp(-1/cfg['tau']); d=1-a*a
    u=Polynomial([0,1]); lam=Polynomial([(1-b)*cfg['mean_arrivals'],b])
    # E[S²]=lambda; E[S⁴]=3(lambda+lambda²);
    # E[S⁶]=15(lambda+3lambda²+lambda³), for S=sum epsilon.
    poly=a**6*u**3+15*a**4*d*u**2*lam+45*a*a*d*d*u*(lam+lam**2)+15*d**3*(lam+3*lam**2+lam**3)
    c=linear_theory(b,cfg['tau'],cfg['mean_arrivals'])['c']
    n=95000
    return dict(coefficients_u_0_to_3=poly.coef.tolist(), q6=float(poly.coef[3]),
        q6_closed_form=a**6+15*a**4*d*b+45*a*a*d*d*b*b+15*d**3*b**3,
        positive_lower_coefficients=bool(np.all(poly.coef[:3]>0)),
        mean_intensity_neff_stationary_approx=n*(1-c)/(1+c),
        mean_intensity_neff_stationary_finite=n/(1+2*sum((1-k/n)*c**k for k in range(1,n))))


def bands(curve):
    return {f'{lo}-{hi}':float(np.mean(curve[lo:hi+1])) for lo,hi in [(1,1),(2,10),(11,30),(31,100),(101,300),(301,1000),(1001,5000)]}


def window_summary(x, start, stop):
    """Inclusive actual tick boundaries; no re-simulation/reset at the boundary."""
    r=x['returns'][start:stop+1]; lam=x['intensity'][start:stop+1]
    activity=state_diagnostics(lam,r)
    row=dict(start=start,stop=stop,n=len(r),returns=describe(r),
        abs_bands=bands(acf(abs(r),5000)),sq_bands=bands(acf(r*r,5000)),
        signed_bands=bands(acf(r,5000)),signed_max_1_1000=float(np.max(abs(acf(r,1000)[1:]))),
        activity=activity,moments=dict(mean_intensity=float(lam.mean()),variance_intensity=float(lam.var()),
        peak_intensity=float(lam.max()),intensity_q99=float(np.quantile(lam,.99)),
        intensity_q999=float(np.quantile(lam,.999)),mean_arrivals=float(x['arrivals'][start:stop+1].mean()),
        max_arrivals=int(x['arrivals'][start:stop+1].max())))
    if 'trades' in x:
        arrivals=x['arrivals'][start:stop+1].sum()
        row['execution']={k:float(x[k][start:stop+1].sum()) for k in ['arrivals','trades','clipped_orders','skipped_orders','cash_saturated','foreign_saturated','gold_volume']}
        row['execution'].update(clipped_fraction=float(x['clipped_orders'][start:stop+1].sum()/arrivals),
            executed_fraction_of_raw_abs=float(x['executed_abs_quantity'][start:stop+1].sum()/x['raw_abs_demand'][start:stop+1].sum()),
            minimum_wallet_gold=float(x['wallet_gold_min'][start:stop+1].min()),
            minimum_wallet_foreign=float(x['wallet_foreign_min'][start:stop+1].min()))
    return clean_json(row)


def analyze_path(directory):
    result=json.loads((directory/'result.json').read_text()); cfg=result['config']
    s=result['summary']; burn=cfg['burn_in']; ticks=cfg['ticks']
    with np.load(directory/'series.npz') as x:
        windows={'full':window_summary(x,burn+1,ticks),'prefix100k':window_summary(x,burn+1,100000)}
        edges=np.linspace(burn,ticks,4,dtype=int)
        for i in range(3):
            windows[f'third{i+1}']=window_summary(x,int(edges[i]+1),int(edges[i+1]))
        assert all(len(x[k])==ticks+1 and np.isfinite(x[k]).all() for k in x.files)
        a=math.exp(-1/cfg['tau'])
        np.testing.assert_allclose(x['z_pre'][2:],a*x['z_pre'][1:-1]+math.sqrt(1-a*a)*x['returns'][1:-1]/cfg['psi'],rtol=1e-12,atol=1e-12)
        np.testing.assert_allclose(x['intensity'][1:],(1-s['b'])*cfg['mean_arrivals']+s['b']*x['z_pre'][1:]**2,rtol=1e-12,atol=1e-12)
        if s['environment']=='amm':
            np.testing.assert_allclose(x['returns'][1:],np.log(x['prices'][1:]/x['prices'][:-1]),rtol=1e-12,atol=1e-15)
            assert (x['prices']>0).all() and (x['gold']>0).all() and (x['pool_foreign']>0).all()
            assert (x['wallet_gold_min']>=0).all() and (x['wallet_foreign_min']>=0).all()
            for asset in ['gold','foreign']:
                expected=Decimal(str(cfg['pool_'+asset]))+cfg['accounts']*Decimal(str(cfg['initial_'+asset]))
                assert Decimal(s['pool_'+asset])+sum(Decimal(v[asset]) for v in s['accounts'])==expected
        else:
            np.testing.assert_array_equal(x['returns'][1:],cfg['psi']*x['raw_mark_sum'][1:])
    for name,digest in result['source_sha256'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
    artifact=dict(environment=s['environment'],b=s['b'],seed=s['seed'],summary=s,windows=windows,
                  saved_points=ticks+1,full_trace_checks=True,source_hashes_current=True)
    (directory/'windows.json').write_text(json.dumps(artifact,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    return artifact


def verify_saved_paths(paths):
    """Independent arithmetic check and lifetime bounds for a saved study."""
    checks, runtime = {}, {}
    for p in paths:
        name=f"{p['environment']}-b{p['b']:g}-seed{p['seed']}"
        full=p['windows']['full']; thirds=[p['windows'][f'third{i}'] for i in range(1,4)]
        assert full['n']==495000 and sum(w['n'] for w in thirds)==495000
        assert [(w['start'],w['stop']) for w in thirds]==[(5001,170000),(170001,335000),(335001,500000)]
        assert (p['windows']['prefix100k']['start'],p['windows']['prefix100k']['stop'])==(5001,100000)
        with np.load(OUTPUT/name/'series.npz') as x:
            runtime[name]=dict(peak_intensity=float(x['intensity'][1:].max()),
                max_arrivals=int(x['arrivals'][1:].max()),scope='ticks1-500000_including_burn_in')
            if p['environment']=='amm':
                runtime[name].update(minimum_closing_wallet_gold=float(x['wallet_gold_min'][1:].min()),
                    minimum_closing_wallet_foreign=float(x['wallet_foreign_min'][1:].min()))
            r=x['returns'][5001:]; v=r*r; centered=v-v.mean(); denominator=np.dot(centered,centered)
            saved=json.loads((OUTPUT/name/'result.json').read_text())['diagnostics']
            for lag in [1,301,1000,5000]:
                np.testing.assert_allclose(np.dot(centered[:-lag],centered[lag:])/denominator,
                    saved['squared_acf'][lag],rtol=1e-12,atol=1e-12)
            for w in [full,*thirds]:
                r=x['returns'][w['start']:w['stop']+1]; lam=x['intensity'][w['start']:w['stop']+1]
                np.testing.assert_allclose(lam.mean(),w['moments']['mean_intensity'],rtol=1e-12,atol=1e-12)
                for block in w['activity']['blocks']:
                    width=block['width']; n=len(r)//width
                    state=lam[:n*width].reshape(n,width).mean(1)
                    actual=(r*r)[:n*width].reshape(n,width).mean(1)
                    assert n==block['complete_blocks']
                    if p['b']==0:
                        assert block['next_block_corr'] is None
                    else:
                        np.testing.assert_allclose(np.corrcoef(state[:-1],actual[1:])[0,1],
                            block['next_block_corr'],rtol=1e-12,atol=1e-12)
        checks[name]=dict(saved_points=p['saved_points'],analysis_returns=full['n'],
            full_trace_checks=p['full_trace_checks'],source_hashes_current=p['source_hashes_current'],
            independent_direct_squared_acf_lags=[1,301,1000,5000],
            all_full_and_third_next_block_correlations_verified=True,segment_boundaries_verified=True)
    return dict(paths=checks,runtime_including_burn_in=runtime,frozen_reference_unchanged=True,
                fixed_rate_inputs_cross_executor_exact=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--workers',type=int,default=3)
    parser.add_argument('--analyze-only',action='store_true')
    args=parser.parse_args()
    freeze()
    cfg=load_config(HERE/'configs/robustness.json')
    reference=load_config()
    assert {k:v for k,v in cfg.items() if k not in ('ticks','seeds')}=={k:v for k,v in reference.items() if k not in ('ticks','seeds')}
    OUTPUT.mkdir(parents=True,exist_ok=True)
    theory=sixth_moment_theory(cfg)
    np.testing.assert_allclose(theory['q6'],theory['q6_closed_form'],rtol=1e-14)
    assert theory['q6']>1 and theory['positive_lower_coefficients']
    (OUTPUT/'theory.json').write_text(json.dumps(theory,indent=2)+'\n')
    if not args.analyze_only:
        for environment in ['linear','amm']:
            tasks=[(cfg,environment,b,seed,str(OUTPUT)) for b in cfg['b_values'] for seed in cfg['seeds']]
            with ProcessPoolExecutor(max_workers=args.workers) as executor:
                futures={executor.submit(run_one,task):task for task in tasks}
                for future in as_completed(futures):
                    summary=future.result()
                    print(f"completed {summary['name']}: mean intensity={summary['moments']['mean_intensity']:.6f}; peak={summary['peak_intensity']:.3f}",flush=True)
    artifacts=[]
    for environment in ['linear','amm']:
        for b in cfg['b_values']:
            for seed in cfg['seeds']:
                artifacts.append(analyze_path(OUTPUT/f'{environment}-b{b:g}-seed{seed}'))
                print(f'analyzed {environment} b={b} seed={seed}',flush=True)
    for seed in cfg['seeds']:
        with np.load(OUTPUT/f'linear-b0-seed{seed}/series.npz') as linear,np.load(OUTPUT/f'amm-b0-seed{seed}/series.npz') as amm:
            for field in ['arrivals','raw_mark_sum','raw_demand','raw_abs_demand']:
                np.testing.assert_array_equal(linear[field],amm[field])
    freeze()
    validation=verify_saved_paths(artifacts)
    (OUTPUT/'validation.json').write_text(json.dumps(validation,indent=2)+'\n')
    data=dict(config=cfg,paths=artifacts,validation=dict(frozen_reference_unchanged=True,
        successful_paths=len(artifacts),full_trace_checks=True,fixed_rate_inputs_cross_executor_exact=True),
        source_sha256=source_hashes())
    (OUTPUT/'study.json').write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+'\n')


if __name__=='__main__':
    main()
