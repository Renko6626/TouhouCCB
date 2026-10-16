"""Frozen exploratory comparison of inventory episodes and existing controls."""
from concurrent.futures import ProcessPoolExecutor, as_completed
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import time
import traceback
import warnings

import numpy as np

from research.fx.activity.model import ActivityState
from research.fx.common.amm import Account, Pool
from research.fx.common.clock import market_streams, draw_events
from research.fx.common.execution import execute
from research.fx.factor_activity.model import components
from research.fx.factor_activity.run import load_config, clean_json, hashes, ROOT
from research.fx.factor_activity.grid import compact_metrics
from research.fx.heterogeneous.profiles import make_profiles
from research.fx.injection.run import pair_metrics, simulate as old_simulate
from .model import BehaviorTrader, liquidation_mark

HERE = Path(__file__).parent
OUTPUT = HERE/'results/exploration'


def longest_empty(values):
    active = np.flatnonzero(np.asarray(values)>0)
    return int(np.max(np.diff(np.r_[-1,active,len(values)])-1))


def simulate(cfg,profiles,count,b,seed,variant,shock_tick=None,injection_gold=0):
    n = cfg['accounts']
    state = ActivityState(b,cfg['tau'],cfg['mean_arrivals'],cfg['psi'],cfg['initial_z'])
    arrival_rngs,mark_rngs,execution_rng = market_streams(seed,n)
    pool = Pool(Decimal(str(cfg['pool_gold'])),Decimal(str(cfg['pool_foreign'])),Decimal(str(cfg['fee_rate'])))
    baseline = Decimal(str(cfg['initial_foreign']))
    accounts = [Account(str(i),Decimal(str(cfg['initial_gold'])),baseline) for i in range(n)]
    traders = [None if i<count else BehaviorTrader(variant) for i in range(n)]
    fields = ['prices','gold','pool_foreign','returns','z_pre','intensity','arrivals','trades',
              'raw_demand','raw_abs_demand','actual_flow','executed_abs_quantity','gold_volume',
              'clipped_orders','skipped_orders','cash_saturated','foreign_saturated','wallet_gold_min','wallet_foreign_min']
    group_fields = ['arrivals','trades','raw_abs_demand','executed_abs_quantity','actual_flow','clipped_orders']
    fields += [f'{g}_{k}' for g in ['random','factor'] for k in group_fields]
    fields += ['injection_gold','random_net_position','factor_net_position','random_gold','factor_gold',
               'holding_accounts','long_basis','short_basis','long_quantity','short_quantity','entries','completions',
               'take_profit_exits','stop_loss_exits','trailing_exits','realized_pnl','idle_arrivals','order_attempts']
    x = {k:np.zeros(cfg['ticks']+1) for k in fields}
    for field,value in [('prices',pool.price),('gold',pool.gold),('pool_foreign',pool.foreign),
                         ('wallet_gold_min',cfg['initial_gold']),('wallet_foreign_min',baseline),
                         ('random_gold',count*cfg['initial_gold']),('factor_gold',(n-count)*cfg['initial_gold'])]:
        x[field][0] = float(value)
    logs = np.zeros(cfg['ticks']+1);logs[0] = math.log(float(pool.price))
    gold_total = pool.gold+sum(a.gold for a in accounts)
    foreign_total = pool.foreign+sum(a.foreign for a in accounts)
    issued = Decimal('0');issue = None;episodes = {};fills = [];realized = Decimal('0')
    for tick in range(1,cfg['ticks']+1):
        lam = state.intensity
        x['intensity'][tick],x['z_pre'][tick] = lam,state.z
        window = max(cfg['trend_window'],cfg['mean_window'])
        past = logs[max(0,tick-window):tick]
        trend,mean,_ = components(past,logs[tick-1],0,1,1,cfg)
        forecast = np.zeros(n)
        for i in range(count,n):
            p = profiles[i]
            terms = np.array([p['trend']*trend,p['reversion']*mean,p['sigma']*float(mark_rngs[i].normal())])
            forecast[i] = terms.sum()
        events = draw_events(arrival_rngs,lam/n,execution_rng)
        x['arrivals'][tick] = len(events)
        for event_index,i in enumerate(events):
            group = 'random' if i<count else 'factor'
            x[f'{group}_arrivals'][tick] += 1
            account = accounts[i]
            before_foreign,before_gold = account.foreign,account.gold
            if i<count:
                wanted = cfg['order_sigma_foreign']*float(mark_rngs[i].normal())
                quote = execute(pool,account,wanted,cfg['max_pool_fraction'])
                clipped = quote is None or abs(float(account.foreign-before_foreign))+2e-6<abs(wanted)
                filled = quote is not None
            else:
                trader = traders[i]
                event = trader.arrive(pool,account,float(forecast[i]),profiles[i]['sigma'],tick,cfg)
                if event['action'] in ('wait','same_tick'):
                    x['idle_arrivals'][tick] += 1
                    continue
                wanted = event['wanted'];filled = event['action'] != 'unfilled'
                clipped = not filled or event['clipped']
                if filled:
                    fills.append(dict(account=i,event_index=event_index,**event))
                    if event['action']=='open':
                        episodes[trader.episode.id] = trader.episode
                        x['entries'][tick] += 1
                    else:
                        realized += Decimal(event['realized_profit'])
                        if event['complete']:
                            x['completions'][tick] += 1
                            x[event['reason']+'_exits'][tick] += 1
            x['order_attempts'][tick] += 1
            x['raw_demand'][tick] += wanted;x['raw_abs_demand'][tick] += abs(wanted)
            x[f'{group}_raw_abs_demand'][tick] += abs(wanted)
            x['cash_saturated'][tick] += int(wanted>0 and before_gold<=0)
            x['foreign_saturated'][tick] += int(wanted<0 and before_foreign<=0)
            x['clipped_orders'][tick] += int(clipped);x[f'{group}_clipped_orders'][tick] += int(clipped)
            if not filled:
                x['skipped_orders'][tick] += 1
                continue
            actual = float(account.foreign-before_foreign)
            for field,value in [('trades',1),('actual_flow',actual),('executed_abs_quantity',abs(actual))]:
                x[field][tick] += value;x[f'{group}_{field}'][tick] += value
            x['gold_volume'][tick] += float(abs(account.gold-before_gold))
        if tick==shock_tick:
            issue = dict(tick=tick,amount=injection_gold,pre_price=str(pool.price),pre_gold=str(pool.gold),
                         pre_foreign=str(pool.foreign),pre_k=str(pool.gold*pool.foreign))
            if injection_gold:
                pool.inject(gold=Decimal(str(injection_gold)))
                issued += Decimal(str(injection_gold))
            issue.update(post_price=str(pool.price),post_gold=str(pool.gold),post_foreign=str(pool.foreign),post_k=str(pool.gold*pool.foreign))
            x['injection_gold'][tick] = injection_gold
        active = [t.episode for t in traders[count:] if t.episode is not None]
        x['holding_accounts'][tick] = len(active)
        for direction,label in [(1,'long'),(-1,'short')]:
            x[label+'_basis'][tick] = float(sum((e.basis for e in active if e.direction==direction),Decimal('0')))
            x[label+'_quantity'][tick] = float(sum((e.remaining for e in active if e.direction==direction),Decimal('0')))
        x['realized_pnl'][tick] = float(realized)
        for group,subset in [('random',accounts[:count]),('factor',accounts[count:])]:
            x[group+'_net_position'][tick] = float(sum((a.foreign-baseline for a in subset),Decimal('0')))
            x[group+'_gold'][tick] = float(sum((a.gold for a in subset),Decimal('0')))
        assert pool.gold+sum(a.gold for a in accounts)==gold_total+issued
        assert pool.foreign+sum(a.foreign for a in accounts)==foreign_total
        x['prices'][tick] = float(pool.price);x['gold'][tick] = float(pool.gold);x['pool_foreign'][tick] = float(pool.foreign)
        x['wallet_gold_min'][tick] = float(min(a.gold for a in accounts))
        x['wallet_foreign_min'][tick] = float(min(a.foreign for a in accounts))
        logs[tick] = math.log(x['prices'][tick])
        x['returns'][tick] = math.log(x['prices'][tick]/x['prices'][tick-1])
        state.advance(x['returns'][tick])
    assert all(a.gold>=0 and a.foreign>=0 for a in accounts)
    for i in range(count,n):
        e = traders[i].episode
        expected = baseline if e is None else baseline+e.direction*e.remaining
        assert accounts[i].foreign==expected
    all_episodes = list(episodes.values());done = [e for e in all_episodes if e.close_tick is not None]
    pending = [e for e in all_episodes if e.close_tick is None]
    unfinished = []
    for e in pending:
        mark = liquidation_mark(pool,e)
        unfinished.append(dict(**e.record(),age=cfg['ticks']-e.entry_tick,unrealized_pnl=str(mark['profit']),
                               liquidation_return=mark['return'],full_rebuy_affordable=e.direction==1 or accounts[int(e.account)].gold>=mark['gold']))
    holding = [e.close_tick-e.entry_tick for e in done]
    active_trades = np.flatnonzero(x['trades'])
    behavior_trades = np.flatnonzero(x['factor_trades'])
    summary = dict(seed=seed,b=b,random_count=count,variant=variant,ticks=cfg['ticks'],issue=issue,
                   peak_intensity=float(x['intensity'].max()),final_z=state.z,conserved_with_issue=True,
                   pool_gold=str(pool.gold),pool_foreign=str(pool.foreign),
                   accounts=[dict(name=a.name,policy='random' if i<count else 'episode',gold=str(a.gold),foreign=str(a.foreign)) for i,a in enumerate(accounts)],
                   behavior=dict(opened=len(all_episodes),completed=len(done),unfinished=len(pending),
                       unfinished_fraction=len(pending)/len(all_episodes) if all_episodes else None,
                       long_opened=sum(e.direction==1 for e in all_episodes),short_opened=sum(e.direction==-1 for e in all_episodes),
                       exits={reason:sum(e.closing_reason==reason for e in done) for reason in ['take_profit','stop_loss','trailing']},
                       mean_completed_holding=float(np.mean(holding)) if holding else None,
                       median_completed_holding=float(np.median(holding)) if holding else None,
                       max_completed_holding=max(holding) if holding else None,
                       realized_pnl=str(realized),unrealized_pnl=str(sum((Decimal(e['unrealized_pnl']) for e in unfinished),Decimal('0'))),
                       post_burn_entries=sum(e.entry_tick>cfg['burn_in'] for e in all_episodes),
                       carry_in=sum(e.entry_tick<=cfg['burn_in'] and (e.close_tick is None or e.close_tick>cfg['burn_in']) for e in all_episodes),
                       post_burn_completions=sum(e.close_tick>cfg['burn_in'] for e in done),
                       longest_no_trade_ticks=longest_empty(x['trades'][1:]),
                       post_burn_longest_no_trade_ticks=longest_empty(x['trades'][cfg['burn_in']+1:]),
                       last_trade_tick=int(active_trades[-1]) if len(active_trades) else None,
                       last_behavior_trade_tick=int(behavior_trades[-1]) if len(behavior_trades) else None),
                   unfinished_episodes=unfinished)
    return x,summary,[e.record() for e in all_episodes],fills


def metrics(x,summary,cfg):
    # Constant post-burn paths really have undefined ACF/kurtosis. Preserve nulls.
    with warnings.catch_warnings():
        warnings.simplefilter('ignore',RuntimeWarning)
        result = compact_metrics(x,summary,cfg)
    sample = slice(cfg['burn_in']+1,None)
    attempts = float(x['order_attempts'][sample].sum())
    result.update(order_attempts=attempts,clipped_fraction_per_attempt=float(x['clipped_orders'][sample].sum()/attempts) if attempts else None,
                  mean_holding_accounts=float(x['holding_accounts'][sample].mean()),
                  post_burn_behavior_trades=float(x['factor_trades'][sample].sum()),
                  post_burn_behavior_quantity=float(x['factor_executed_abs_quantity'][sample].sum()))
    return clean_json(result)


def source_hashes():
    value = hashes()
    for path in [HERE/'model.py',HERE/'run.py',HERE/'config.json',HERE.parent/'factor_activity/grid.py',
                 HERE.parent/'heterogeneous/profiles.py',HERE.parent/'injection/run.py']:
        value[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return value


def save_trace(directory,label,x,episodes,fills):
    np.savez_compressed(directory/f'{label}.npz',**x)
    (directory/f'{label}-episodes.json').write_text(json.dumps(episodes,ensure_ascii=False,indent=2)+'\n')
    (directory/f'{label}-fills.jsonl').write_text(''.join(json.dumps(f,ensure_ascii=False)+'\n' for f in fills))


def run_one(task):
    stage,cfg,profiles,count,b,seed,variant,spec,sources = task
    name = f'{variant}-random{count:02d}-b{b:g}-seed{seed}'
    directory = OUTPUT/stage/name;directory.mkdir(parents=True,exist_ok=True)
    record = dict(stage=stage,name=name,random_count=count,b=b,seed=seed,variant=variant,config=cfg,source_sha256=sources)
    start = time.perf_counter()
    try:
        if stage=='normal':
            x,summary,episodes,fills = simulate(cfg,profiles,count,b,seed,variant)
            record.update(summary=summary,metrics=metrics(x,summary,cfg))
            record['segments'] = []
            for number,indices in enumerate(np.array_split(np.arange(cfg['burn_in']+1,cfg['ticks']+1),3),1):
                sub = {k:v[indices] for k,v in x.items()}
                sm = {**summary,'peak_intensity':float(sub['intensity'].max())}
                record['segments'].append(dict(segment=number,first_tick=int(indices[0]),last_tick=int(indices[-1]),metrics=metrics(sub,sm,{**cfg,'burn_in':-1})))
            save_trace(directory,'series',x,episodes,fills)
        else:
            control,cs,ce,cf = simulate(cfg,profiles,count,b,seed,variant,spec['shock_tick'],0)
            shock,ss,se,sf = simulate(cfg,profiles,count,b,seed,variant,spec['shock_tick'],spec['injection_gold'])
            assert all(np.array_equal(shock[k][:spec['shock_tick']],control[k][:spec['shock_tick']]) for k in control)
            record.update(control_summary=cs,shock_summary=ss,response=pair_metrics(shock,control,ss,spec))
            save_trace(directory,'control',control,ce,cf);save_trace(directory,'shock',shock,se,sf)
        record['status'] = 'complete'
    except Exception as exc:
        record.update(status='failed',error=f'{type(exc).__name__}: {exc}',traceback=traceback.format_exc())
    record['seconds'] = time.perf_counter()-start
    (directory/'result.json').write_text(json.dumps(clean_json(record),ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    return record


def main():
    spec = json.loads((HERE/'config.json').read_text())
    cfg = load_config()
    cfg.update({k:spec[k] for k in ['entry_quantity','take_profit','stop_loss','trail_arm','trail_drawdown']})
    cfg['seeds'] = spec['market_seeds']
    profiles = make_profiles(cfg,spec['population_seed'],spec['spread'])
    sources = source_hashes();OUTPUT.mkdir(parents=True,exist_ok=True)
    frozen = json.loads((HERE.parent/'activity/baseline-v1.json').read_text())['sha256']
    assert all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in frozen.items())
    manifest = dict(spec=spec,config=cfg,profiles=profiles,source_sha256=sources,frozen_files_verified=len(frozen))
    path = OUTPUT/'manifest.json'
    if path.exists():assert json.loads(path.read_text())==manifest
    else:path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    # A policy-free population still follows the old random implementation exactly.
    short = {**cfg,'ticks':200,'burn_in':30};compatibility = []
    for b in spec['b_values']:
        for issue_amount in [0,spec['injection_gold']]:
            old,_ = old_simulate(short,profiles,20,b,81,100,issue_amount)
            new,_,_,_ = simulate(short,profiles,20,b,81,'fixed',100,issue_amount)
            common = sorted(old.keys()&new.keys())
            assert all(np.array_equal(old[k],new[k]) for k in common),(b,issue_amount)
            compatibility.append(dict(b=b,injection_gold=issue_amount,exact_common_fields=len(common)))
    (OUTPUT/'compatibility.json').write_text(json.dumps(compatibility,indent=2)+'\n')
    tasks,records = [],[]
    for stage in ['normal','shock']:
        scfg = {**cfg,'ticks':spec['normal_ticks'] if stage=='normal' else spec['shock_ticks'],
                'burn_in':spec['burn_in'] if stage=='normal' else spec['shock_tick']}
        for variant in spec['variants']:
            for count in spec['random_counts']:
                for b in spec['b_values']:
                    for seed in spec['market_seeds']:
                        name = f'{variant}-random{count:02d}-b{b:g}-seed{seed}'
                        path = OUTPUT/stage/name/'result.json'
                        if path.exists():
                            r = json.loads(path.read_text());assert r['source_sha256']==sources and r['config']==scfg
                            records.append(r)
                        else:tasks.append((stage,scfg,profiles,count,b,seed,variant,spec,sources))
    print(json.dumps(dict(cached=len(records),pending_tasks=len(tasks),compatibility='passed')),flush=True)
    with ProcessPoolExecutor(max_workers=spec['workers']) as executor:
        for future in as_completed([executor.submit(run_one,t) for t in tasks]):
            r = future.result();records.append(r)
            print(json.dumps(dict(done=len(records),stage=r['stage'],name=r['name'],status=r['status'],seconds=round(r['seconds'],1))),flush=True)
    records.sort(key=lambda r:(r['stage'],r['name']))
    (OUTPUT/'summary.json').write_text(json.dumps(clean_json(records),ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    assert len(records)==48 and all(r['status']=='complete' for r in records)


if __name__=='__main__':main()
