"""Personal factor heterogeneity; all existing sources and datasets stay unchanged."""
from concurrent.futures import ProcessPoolExecutor,as_completed
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import time
import traceback

import numpy as np

from backend.app.services.fx.quantize import amount_down
from research.fx.common.amm import Account,Pool
from research.fx.common.clock import market_streams,draw_events
from research.fx.common.analysis import return_diagnostics,state_diagnostics
from research.fx.factor_activity.model import components,target_position,execute_target
from research.fx.factor_activity.run import load_config,hashes,ROOT,clean_json,simulate as old_simulate
from research.fx.factor_activity.grid import compact_metrics
from research.fx.activity.run import simulate as activity_simulate
from research.fx.activity.model import ActivityState
from .profiles import make_profiles

HERE=Path(__file__).parent
OUTPUT=HERE/'results/experiment'


def simulate(cfg,profiles,b,seed):
    if len(profiles)!=cfg['accounts']:raise ValueError('one profile per account is required')
    state=ActivityState(b,cfg['tau'],cfg['mean_arrivals'],cfg['psi'],cfg['initial_z'])
    arrival_rngs,mark_rngs,execution_rng=market_streams(seed,cfg['accounts'])
    pool=Pool(Decimal(str(cfg['pool_gold'])),Decimal(str(cfg['pool_foreign'])),Decimal(str(cfg['fee_rate'])))
    initial_foreign=Decimal(str(cfg['initial_foreign']))
    accounts=[Account(str(i),Decimal(str(cfg['initial_gold'])),initial_foreign) for i in range(cfg['accounts'])]
    fields=['prices','gold','pool_foreign','returns','z_pre','intensity','arrivals','trades',
        'raw_demand','raw_abs_demand','actual_flow','executed_abs_quantity','gold_volume',
        'clipped_orders','skipped_orders','cash_saturated','foreign_saturated',
        'wallet_gold_min','wallet_foreign_min','forecast_noise_sum','max_abs_position',
        'standard_noise_sum','forecast_alignment']
    x={k:np.zeros(cfg['ticks']+1) for k in fields}
    x['prices'][0]=float(pool.price);x['gold'][0]=float(pool.gold);x['pool_foreign'][0]=float(pool.foreign)
    x['wallet_gold_min'][0]=cfg['initial_gold'];x['wallet_foreign_min'][0]=cfg['initial_foreign']
    logs=np.zeros(cfg['ticks']+1);logs[0]=math.log(float(pool.price))
    moments=np.zeros((cfg['accounts'],7))
    initial_gold_total=pool.gold+sum(a.gold for a in accounts)
    initial_foreign_total=pool.foreign+sum(a.foreign for a in accounts)
    for tick in range(1,cfg['ticks']+1):
        observed_price=pool.price;lam=state.intensity
        x['intensity'][tick]=lam;x['z_pre'][tick]=state.z
        window=max(cfg['trend_window'],cfg['mean_window'])
        past=logs[max(0,tick-window):tick]
        trend,mean,_=components(past,logs[tick-1],0,1,1,cfg)
        forecast=np.zeros(cfg['accounts'])
        for i,rng in enumerate(mark_rngs):
            profile=profiles[i];epsilon=float(rng.normal());noise=profile['sigma']*epsilon
            terms=np.array([profile['trend']*trend,profile['reversion']*mean,noise])
            forecast[i]=terms.sum();x['forecast_noise_sum'][tick]+=noise
            x['standard_noise_sum'][tick]+=epsilon
            if tick>cfg['burn_in']:
                moments[i,0]+=1;moments[i,1:4]+=terms;moments[i,4:7]+=terms*terms
        x['forecast_alignment'][tick]=float(abs(np.sign(forecast).mean()))
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
                x['skipped_orders'][tick]+=1;x['clipped_orders'][tick]+=int(abs(raw)>2e-6)
                continue
            actual=float(account.foreign-before)
            x['clipped_orders'][tick]+=int(quote.input_amount<unclipped_input)
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
    for profile,row in zip(profiles,moments):
        count=row[0];means=row[1:4]/count if count else np.zeros(3)
        std=np.sqrt(np.maximum(0,row[4:7]/count-means*means)) if count else np.zeros(3)
        factors.append(dict(profile=profile,updates=int(count),means=means.tolist(),std=std.tolist()))
    summary=dict(seed=seed,b=b,ticks=cfg['ticks'],final_z=state.z,
        arrivals=int(x['arrivals'].sum()),trades=int(x['trades'].sum()),gold_volume=float(x['gold_volume'].sum()),
        peak_intensity=float(x['intensity'].max()),conserved=True,nonnegative_wallets=True,
        max_abs_position=float(x['max_abs_position'].max()),factors=factors,
        pool_gold=str(pool.gold),pool_foreign=str(pool.foreign),
        accounts=[dict(name=a.name,gold=str(a.gold),foreign=str(a.foreign)) for a in accounts])
    return x,summary


def source_hashes():
    value=hashes()
    value['research/fx/factor_activity/grid.py']=hashlib.sha256((HERE.parent/'factor_activity/grid.py').read_bytes()).hexdigest()
    for p in [HERE/'run.py',HERE/'profiles.py',HERE/'config.json']:
        value[str(p.relative_to(ROOT))]=hashlib.sha256(p.read_bytes()).hexdigest()
    return value


def run_one(task):
    cfg,population,seed,b,sources=task
    name=f"{population['id']}-b{b:g}-seed{seed}";directory=OUTPUT/name;directory.mkdir(parents=True,exist_ok=True)
    record=dict(name=name,model=population['model'],population=population['id'],
        population_seed=population['population_seed'],spread=population['spread'],seed=seed,b=b,
        profiles=population['profiles'],config=cfg,source_sha256=sources)
    start=time.perf_counter()
    try:
        if population['model']=='activity_v1':x,s=activity_simulate(cfg,'amm',b,seed)
        else:x,s=simulate(cfg,population['profiles'],b,seed)
        sample=slice(cfg['burn_in']+1,None);r=x['returns'][sample]
        metrics=compact_metrics(x,s,cfg)
        metrics['forecast_alignment']=float(x['forecast_alignment'][sample].mean()) if 'forecast_alignment' in x else None
        diagnostics=return_diagnostics(r);diagnostics['activity']=state_diagnostics(x['intensity'][sample],r)
        record.update(status='complete',summary=s,metrics=metrics,diagnostics=diagnostics)
        np.savez_compressed(directory/'series.npz',**x)
    except Exception as exc:
        record.update(status='failed',error=f'{type(exc).__name__}: {exc}',traceback=traceback.format_exc())
    record['seconds']=time.perf_counter()-start
    record=clean_json(record)
    (directory/'result.json').write_text(json.dumps(record,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    return record


def main():
    spec=json.loads((HERE/'config.json').read_text());cfg=load_config()
    cfg.update(ticks=spec['ticks'],burn_in=spec['burn_in'],seeds=spec['market_seeds'])
    populations=[dict(id='fixed',model='fixed',population_seed=None,spread=0,profiles=make_profiles(cfg,0,0))]
    for spread in spec['spreads']:
        for seed in spec['population_seeds']:
            populations.append(dict(id=f's{spread:g}-p{seed}',model=f'hetero_{spread:g}',population_seed=seed,
                spread=spread,profiles=make_profiles(cfg,seed,spread)))
    populations.append(dict(id='v1',model='activity_v1',population_seed=None,spread=None,profiles=None))
    sources=source_hashes();OUTPUT.mkdir(parents=True,exist_ok=True)
    frozen=json.loads((HERE.parent/'activity/baseline-v1.json').read_text())['sha256']
    assert all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in frozen.items())
    manifest=dict(spec=spec,config=cfg,populations=populations,source_sha256=sources,frozen_files_verified=len(frozen))
    path=OUTPUT/'manifest.json'
    if path.exists():assert json.loads(path.read_text())==manifest,'existing manifest changed'
    else:path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    # Use the actual archived algorithm as the zero-spread oracle before long runs.
    short={**cfg,'ticks':200,'burn_in':30};compatibility={}
    for case,b in [('factor_fixed',0),('factor_activity',.25)]:
        old,_=old_simulate(short,case,71);new,_=simulate(short,make_profiles(short,0,0),b,71)
        assert all(np.array_equal(old[k],new[k]) for k in old)
        compatibility[case]=dict(all_original_fields_exact=True,fields=len(old))
    (OUTPUT/'compatibility.json').write_text(json.dumps(compatibility,indent=2)+'\n')
    tasks=[];records=[]
    for p in populations:
        for b in ([.25] if p['model']=='activity_v1' else [0,.25]):
            for seed in spec['market_seeds']:
                path=OUTPUT/f"{p['id']}-b{b:g}-seed{seed}"/'result.json'
                if path.exists():
                    r=json.loads(path.read_text());assert r['config']==cfg and r['source_sha256']==sources and r['profiles']==p['profiles']
                    records.append(r)
                else:tasks.append((cfg,p,seed,b,sources))
    print(json.dumps(dict(cached=len(records),pending=len(tasks))),flush=True)
    with ProcessPoolExecutor(max_workers=spec['workers']) as executor:
        for future in as_completed([executor.submit(run_one,t) for t in tasks]):
            r=future.result();records.append(r)
            print(json.dumps(dict(done=len(records),name=r['name'],status=r['status'],seconds=round(r['seconds'],2))),flush=True)
    records.sort(key=lambda r:r['name'])
    (OUTPUT/'summary.json').write_text(json.dumps(clean_json(records),ensure_ascii=False,indent=2)+'\n')
    print('Finished heterogeneous experiment.',flush=True)


if __name__=='__main__':main()
