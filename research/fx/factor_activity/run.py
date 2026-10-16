"""Three fixed comparison cells; activity v1 and archived files stay frozen."""
import hashlib
import json
import math
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from pathlib import Path

import numpy as np

from research.fx.common.amm import Account,Pool
from research.fx.common.clock import market_streams,draw_events
from research.fx.common.analysis import return_diagnostics,state_diagnostics
from backend.app.services.fx.quantize import amount_down
from research.fx.activity.model import ActivityState
from research.fx.activity.run import simulate as activity_simulate,source_hashes,clean_json,ROOT
from .model import components,target_position,execute_target

HERE=Path(__file__).parent
OUTPUT=HERE/'results/comparison'
CASES=('factor_fixed','activity_v1','factor_activity')


def load_config():
    return json.loads((HERE/'config.json').read_text())


def simulate(cfg,case,seed):
    if case=='activity_v1':
        x,s=activity_simulate(cfg,'amm',.25,seed)
        s.update(case=case,factors=None)
        return x,s
    if case not in CASES:
        raise ValueError('unknown case')
    b=0 if case=='factor_fixed' else .25
    state=ActivityState(b,cfg['tau'],cfg['mean_arrivals'],cfg['psi'],cfg['initial_z'])
    arrival_rngs,mark_rngs,execution_rng=market_streams(seed,cfg['accounts'])
    pool=Pool(Decimal(str(cfg['pool_gold'])),Decimal(str(cfg['pool_foreign'])),Decimal(str(cfg['fee_rate'])))
    initial_foreign=Decimal(str(cfg['initial_foreign']))
    accounts=[Account(str(i),Decimal(str(cfg['initial_gold'])),initial_foreign) for i in range(cfg['accounts'])]
    fields=['prices','gold','pool_foreign','returns','z_pre','intensity','arrivals','trades',
        'raw_demand','raw_abs_demand','actual_flow','executed_abs_quantity','gold_volume',
        'clipped_orders','skipped_orders','cash_saturated','foreign_saturated',
        'wallet_gold_min','wallet_foreign_min','forecast_noise_sum','max_abs_position']
    x={k:np.zeros(cfg['ticks']+1) for k in fields}
    x['prices'][0]=float(pool.price);x['gold'][0]=float(pool.gold);x['pool_foreign'][0]=float(pool.foreign)
    x['wallet_gold_min'][0]=cfg['initial_gold'];x['wallet_foreign_min'][0]=cfg['initial_foreign']
    logs=np.zeros(cfg['ticks']+1);logs[0]=math.log(float(pool.price))
    # Rows are groups; columns count, sums(T,M,N), sums of squares(T,M,N).
    moments=np.zeros((len(cfg['factor_mix']),7))
    initial_gold_total=pool.gold+sum(a.gold for a in accounts)
    initial_foreign_total=pool.foreign+sum(a.foreign for a in accounts)
    for tick in range(1,cfg['ticks']+1):
        observed_price=pool.price
        lam=state.intensity
        x['intensity'][tick]=lam;x['z_pre'][tick]=state.z
        window=max(cfg['trend_window'],cfg['mean_window'])
        past=logs[max(0,tick-window):tick]
        trend,mean,_=components(past,logs[tick-1],0,1,1,cfg)
        forecast=np.zeros(cfg['accounts'])
        for i,rng in enumerate(mark_rngs):
            group=i%len(cfg['factor_mix']);a,c=cfg['factor_mix'][group]
            noise=cfg['noise_sigma']*float(rng.normal())
            terms=np.array([a*trend,c*mean,noise])
            forecast[i]=terms.sum();x['forecast_noise_sum'][tick]+=noise
            if tick>cfg['burn_in']:
                moments[group,0]+=1;moments[group,1:4]+=terms;moments[group,4:7]+=terms*terms
        events=draw_events(arrival_rngs,lam/cfg['accounts'],execution_rng)
        x['arrivals'][tick]=len(events)
        for i in events:
            account=accounts[i];before=account.foreign;before_gold=account.gold
            target=target_position(float(forecast[i]),cfg)
            exact_wanted=Decimal(str(cfg['adjustment']))*(target-(before-initial_foreign))
            unclipped_quantity=amount_down(abs(exact_wanted))
            unclipped_input=(amount_down(unclipped_quantity*observed_price)
                             if exact_wanted>0 else unclipped_quantity)
            raw,quote=execute_target(pool,account,target,initial_foreign,observed_price,cfg)
            x['raw_demand'][tick]+=raw;x['raw_abs_demand'][tick]+=abs(raw)
            x['cash_saturated'][tick]+=int(raw>0 and before_gold<=0)
            x['foreign_saturated'][tick]+=int(raw<0 and before<=0)
            if quote is None:
                x['skipped_orders'][tick]+=1
                x['clipped_orders'][tick]+=int(abs(raw)>2e-6)
                continue
            actual=float(account.foreign-before)
            # Isolate limits from budget/slippage: the planned input is a gold
            # budget on buys and a foreign amount on sells, not exact-out q.
            limited=quote.input_amount<unclipped_input
            x['clipped_orders'][tick]+=int(limited)
            x['trades'][tick]+=1;x['actual_flow'][tick]+=actual
            x['executed_abs_quantity'][tick]+=abs(actual)
            x['gold_volume'][tick]+=float(quote.input_amount if raw>0 else quote.output_amount)
        x['prices'][tick]=float(pool.price);x['gold'][tick]=float(pool.gold);x['pool_foreign'][tick]=float(pool.foreign)
        x['wallet_gold_min'][tick]=float(min(a.gold for a in accounts))
        x['wallet_foreign_min'][tick]=float(min(a.foreign for a in accounts))
        x['max_abs_position'][tick]=float(max(abs(a.foreign-initial_foreign) for a in accounts))
        logs[tick]=math.log(x['prices'][tick])
        x['returns'][tick]=math.log(x['prices'][tick]/x['prices'][tick-1])
        state.advance(x['returns'][tick])
    assert pool.gold+sum(a.gold for a in accounts)==initial_gold_total
    assert pool.foreign+sum(a.foreign for a in accounts)==initial_foreign_total
    assert all(a.gold>=0 and a.foreign>=0 for a in accounts)
    assert x['max_abs_position'].max()<=cfg['position_cap']+1e-6
    factors=[]
    for weights,row in zip(cfg['factor_mix'],moments):
        count=row[0];means=row[1:4]/count if count else np.zeros(3)
        sds=np.sqrt(np.maximum(0,row[4:7]/count-means*means)) if count else np.zeros(3)
        factors.append(dict(weights=weights,updates=int(count),means=means.tolist(),std=sds.tolist()))
    s=dict(case=case,seed=seed,b=b,ticks=cfg['ticks'],final_z=state.z,
        arrivals=int(x['arrivals'].sum()),trades=int(x['trades'].sum()),gold_volume=float(x['gold_volume'].sum()),
        peak_intensity=float(x['intensity'].max()),conserved=True,nonnegative_wallets=True,
        max_abs_position=float(x['max_abs_position'].max()),factors=factors,
        pool_gold=str(pool.gold),pool_foreign=str(pool.foreign),
        accounts=[dict(name=a.name,gold=str(a.gold),foreign=str(a.foreign)) for a in accounts])
    return x,s


def hashes():
    value=source_hashes()
    paths=[HERE/'model.py',Path(__file__),HERE/'config.json']
    paths += [ROOT/'research/fx/archive/2026-10-15'/name for name in ['expectation.py','position.py','configs/baseline.json']]
    value.update({str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})
    return value


def run_one(task):
    cfg,case,seed=task;directory=OUTPUT/f'{case}-seed{seed}';directory.mkdir(parents=True,exist_ok=True)
    x,s=simulate(cfg,case,seed);sample=slice(cfg['burn_in']+1,None)
    r=x['returns'][sample]
    d=return_diagnostics(r);d['activity']=state_diagnostics(x['intensity'][sample],r)
    d['execution']={k:float(x[k][sample].sum()) for k in ['arrivals','trades','gold_volume','clipped_orders','skipped_orders','raw_abs_demand','executed_abs_quantity']}
    d['execution']['clipped_fraction']=d['execution']['clipped_orders']/d['execution']['arrivals']
    d['mean_intensity']=float(x['intensity'][sample].mean())
    result=clean_json(dict(config=cfg,summary=s,diagnostics=d,source_sha256=hashes()))
    np.savez_compressed(directory/'series.npz',**x)
    (directory/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    return dict(case=case,seed=seed,arrivals=s['arrivals'],trades=s['trades'],mean_intensity=d['mean_intensity'])


def main():
    cfg=load_config();OUTPUT.mkdir(parents=True,exist_ok=True)
    frozen=json.loads((HERE.parent/'activity/baseline-v1.json').read_text())['sha256']
    assert all(frozen[name]==digest for name,digest in source_hashes().items())
    tasks=[(cfg,case,seed) for case in CASES for seed in cfg['seeds']]
    with ProcessPoolExecutor(max_workers=3) as executor:
        for row in executor.map(run_one,tasks):print(json.dumps(row),flush=True)
    validation={}
    for seed in cfg['seeds']:
        with np.load(OUTPUT/f'activity_v1-seed{seed}/series.npz') as new,np.load(HERE.parent/f'activity/results/reference/amm-b0.25-seed{seed}/series.npz') as old:
            assert set(new.files)==set(old.files)
            assert all(np.array_equal(new[k],old[k]) for k in old.files)
        with np.load(OUTPUT/f'factor_fixed-seed{seed}/series.npz') as fixed,np.load(OUTPUT/f'factor_activity-seed{seed}/series.npz') as hybrid:
            np.testing.assert_array_equal(fixed['forecast_noise_sum'],hybrid['forecast_noise_sum'])
        validation[str(seed)]=dict(v1_entire_trace_exact=True,forecast_noise_paired_between_factor_cases=True)
    (OUTPUT/'validation.json').write_text(json.dumps(validation,indent=2)+'\n')


if __name__=='__main__':main()
