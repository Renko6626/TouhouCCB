"""Configuration-driven experiments using the existing Decimal AMM and bots.

Input JSON: defaults, grid (Cartesian product), cases (explicit overrides), seeds.
Every result has its own parameters, source hashes, status and saved raw arrays.
"""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
import hashlib
from itertools import product
import json
from pathlib import Path
from time import perf_counter
import traceback

import numpy as np

from .amm import Account, Pool
from .analysis import analyze, compact
from .expectation import ExpectationTrader
from .bots import Bot
from .position import PositionTrader
from .threshold import ThresholdTrader
from .locked_position import LockedPositionTrader
from .simulation import Simulation


@dataclass(frozen=True)
class Config:
    ticks: int = 100000
    burn_in: int = 2000
    seed: int = 1
    bots: int = 20
    rate: float = .05
    order_size: float = 200
    trend_window: int = 30
    mean_window: int = 30
    trend_strength: float = 30
    reversion_strength: float = .1
    noise_sigma: float = .005
    sensitivity: float = 100
    gold: float = 1000000
    foreign: float = 200000
    fee_rate: float = .0005
    strategy: str = 'fixed'
    position_cap: float = 500
    adjustment: float = .2
    window_mix: tuple[int, ...] = ()
    factor_mix: tuple[tuple[float, float], ...] = ()

    entry_threshold: float = .001
    exit_threshold: float = 0.
    threshold_mix: tuple[float, ...] = ()

    def identifier(self):
        parameters=asdict(self)
        if self.strategy not in ('threshold','locked_position'):
            for key in ('entry_threshold','exit_threshold','threshold_mix'):
                parameters.pop(key)
        elif not self.threshold_mix:
            parameters.pop('threshold_mix')
        if self.strategy=='locked_position':
            parameters.pop('adjustment')
        if not self.window_mix:
            parameters.pop('window_mix')
        if not self.factor_mix:
            parameters.pop('factor_mix')
        if self.strategy=='fixed':
            for key in ('strategy','position_cap','adjustment'):
                parameters.pop(key)
        return hashlib.sha256(json.dumps(parameters,sort_keys=True).encode()).hexdigest()[:16]


def expand(document):
    defaults=document.get('defaults',{})
    axes=document.get('grid',{})
    keys=list(axes)
    cases=[dict(zip(keys,values)) for values in product(*(axes[k] for k in keys))] if axes else []
    cases+=document.get('cases',[])
    if not cases:
        cases=[{}]
    unique={}
    for case in cases:
        for seed in document.get('seeds',[1]):
            parameters={**defaults,**case,'seed':seed}
            if 'window' in parameters:
                window=parameters.pop('window')
                parameters.update(trend_window=window,mean_window=window)
            parameters['window_mix']=tuple(parameters.get('window_mix',()))
            parameters['factor_mix']=tuple(tuple(pair) for pair in parameters.get('factor_mix',()))
            parameters['threshold_mix']=tuple(parameters.get('threshold_mix',()))
            cfg=Config(**parameters)
            if cfg.threshold_mix and (cfg.bots%len(cfg.threshold_mix) or any(not np.isfinite(v) or v<=abs(cfg.exit_threshold) for v in cfg.threshold_mix)):
                raise ValueError('threshold_mix needs equal groups and positive entry > abs(exit)')
            if cfg.factor_mix and (cfg.bots%len(cfg.factor_mix) or any(len(pair)!=2 or any(not np.isfinite(v) or v<0 for v in pair) for pair in cfg.factor_mix)):
                raise ValueError('factor_mix needs equal-sized groups of nonnegative finite (trend,reversion) pairs')
            if cfg.window_mix and (any(not isinstance(w,int) or w<=0 for w in cfg.window_mix) or cfg.bots%len(cfg.window_mix)):
                raise ValueError('window_mix needs positive integer windows and equal-sized bot groups')
            if cfg.ticks-cfg.burn_in<20000 or cfg.burn_in<max(cfg.trend_window,cfg.mean_window,*cfg.window_mix):
                raise ValueError('need >=20000 analyzed ticks and a burn-in covering the full history window')
            if cfg.bots<1 or cfg.rate<=0 or cfg.order_size<=0:
                raise ValueError('experiments need positive bot count, rate and order size')
            unique[cfg.identifier()]=cfg
    return list(unique.values())


class ObservedTrader(Bot):
    def __init__(self,account,cfg,moments,arrivals,group_moments=None):
        super().__init__(account)
        kwargs=dict(trend_window=cfg.trend_window,mean_window=cfg.mean_window,
                         trend_strength=cfg.trend_strength,reversion_strength=cfg.reversion_strength,
                         noise_sigma=cfg.noise_sigma,sensitivity=cfg.sensitivity,
                         rate_per_tick=cfg.rate,gold_size=Decimal(str(cfg.order_size)))
        if cfg.strategy=='fixed':
            self.trader=ExpectationTrader(account,**kwargs)
        elif cfg.strategy in ('position','threshold','locked_position'):
            trader_class={'position':PositionTrader,'threshold':ThresholdTrader,'locked_position':LockedPositionTrader}[cfg.strategy]
            thresholds=dict(entry_threshold=cfg.entry_threshold,exit_threshold=cfg.exit_threshold) if cfg.strategy!='position' else {}
            self.trader=trader_class(account,position_cap=Decimal(str(cfg.position_cap)),
                                    adjustment=cfg.adjustment,**thresholds,**kwargs)
        else:
            raise ValueError('strategy must be fixed, position, threshold or locked_position')
        self.burn_in=cfg.burn_in; self.moments=moments; self.group_moments=group_moments
        self.arrivals=arrivals

    def decide(self,view,rng):
        before=self.trader.total_arrivals
        orders=self.trader.decide(view,rng)
        self.arrivals[view.tick]+=self.trader.total_arrivals-before
        if view.tick>=self.burn_in:
            factors=(self.trader.trend_component,self.trader.reversion_component,self.trader.noise_component)
            for moments in (self.moments,self.group_moments):
                if moments is None:
                    continue
                for row,weight in zip(moments,(1,len(orders))):
                    row[0]+=weight
                    for i,value in enumerate(factors):
                        row[1+i]+=weight*value; row[4+i]+=weight*value*value
        return orders


def simulate(cfg):
    moments=[[0.]*7 for _ in range(2)]
    arrivals=np.zeros(cfg.ticks+1,dtype=np.int32)
    bot_configs=[replace(cfg,trend_window=cfg.window_mix[i%len(cfg.window_mix)],
                         mean_window=cfg.window_mix[i%len(cfg.window_mix)]) if cfg.window_mix else cfg
                 for i in range(cfg.bots)]
    if cfg.factor_mix:
        bot_configs=[replace(c,trend_strength=cfg.factor_mix[i%len(cfg.factor_mix)][0],
                             reversion_strength=cfg.factor_mix[i%len(cfg.factor_mix)][1]) for i,c in enumerate(bot_configs)]
    if cfg.threshold_mix:
        bot_configs=[replace(c,entry_threshold=cfg.threshold_mix[i%len(cfg.threshold_mix)]) for i,c in enumerate(bot_configs)]
    group_keys=[f'a={c.trend_strength:g},b={c.reversion_strength:g}' for c in bot_configs]
    group_moments={key:[[0.]*7 for _ in range(2)] for key in set(group_keys)} if cfg.factor_mix else {}
    bots=[ObservedTrader(Account(str(i),Decimal('1e12'),Decimal('2e11')),individual,moments,arrivals,group_moments.get(group_keys[i]))
          for i,individual in enumerate(bot_configs)]
    initial_gold=Decimal(str(cfg.gold)); initial_foreign=Decimal(str(cfg.foreign))
    sim=Simulation(Pool(initial_gold,initial_foreign,Decimal(str(cfg.fee_rate))),bots,
                   seed=cfg.seed,history_limit=max(cfg.trend_window,cfg.mean_window,*cfg.window_mix))
    sim.run(cfg.ticks,1)
    rejected=[r for r in sim.records if r.kind!='trade']
    assert sim.pool.gold+sim.pool.fees_gold+sum(b.account.gold for b in bots)==initial_gold+Decimal('1e12')*cfg.bots
    assert sim.pool.foreign+sim.pool.fees_foreign+sum(b.account.foreign for b in bots)==initial_foreign+Decimal('2e11')*cfg.bots
    prices=np.array([float(p.price) for p in sim.prices])
    flow=np.zeros(cfg.ticks+1); counts=np.zeros(cfg.ticks+1,dtype=np.int32)
    buy_counts=np.zeros(cfg.ticks+1,dtype=np.int32); volume=np.zeros(cfg.ticks+1)
    ledger={str(i):[Decimal('1e12'),Decimal('2e11')] for i in range(cfg.bots)}
    peaks={str(i):[Decimal(0),Decimal(0)] for i in range(cfg.bots)}
    total_position=Decimal(0);peak_total_position=Decimal(0);gross_foreign=Decimal(0)
    first_snapshot=None
    def account_rows():
        return [dict(actor=name,gold=str(balance[0]),foreign=str(balance[1]),
                     gold_change=str(balance[0]-Decimal('1e12')),
                     foreign_change=str(balance[1]-Decimal('2e11')),
                     min_foreign_change=str(peaks[name][0]),max_foreign_change=str(peaks[name][1]))
                for name,balance in ledger.items()]
    for r in sim.records:
        if r.kind!='trade':
            if first_snapshot is None:
                first_snapshot=dict(tick=r.time_seconds,price=str(r.price_before),accounts=account_rows())
            continue
        t=r.time_seconds; counts[t]+=1; volume[t]+=float(r.gold_volume)
        if r.side=='buy':
            buy_counts[t]+=1; flow[t]+=float(r.input_amount-r.fee_amount)
            ledger[r.actor][0]-=r.input_amount;ledger[r.actor][1]+=r.output_amount
            signed_foreign=r.output_amount
        else:
            flow[t]-=float(r.output_amount)
            ledger[r.actor][0]+=r.output_amount;ledger[r.actor][1]-=r.input_amount
            signed_foreign=-r.input_amount
        gross_foreign+=abs(signed_foreign);total_position+=signed_foreign
        peak_total_position=max(peak_total_position,abs(total_position))
        current=ledger[r.actor][1]-Decimal('2e11')
        peaks[r.actor][0]=min(peaks[r.actor][0],current)
        peaks[r.actor][1]=max(peaks[r.actor][1],current)
    for bot in bots:
        assert ledger[bot.account.name]==[bot.account.gold,bot.account.foreign]
    peak_individual=max(max(abs(x[0]),abs(x[1])) for x in peaks.values())
    if cfg.strategy in ('position','threshold','locked_position'):
        assert peak_individual<=Decimal(str(cfg.position_cap)), 'bounded inventory overshoot'
    gold=float(initial_gold)+np.cumsum(flow)
    assert np.isclose(gold[-1],float(sim.pool.gold),rtol=1e-10)
    assert np.isfinite(prices).all() and np.isfinite(gold).all() and (prices>0).all() and (gold>0).all()
    def factor_stats(moments):
        return {scope:{name:dict(mean=row[1+i]/row[0],std=float(np.sqrt(max(0,row[4+i]/row[0]-(row[1+i]/row[0])**2)))) if row[0] else None
                   for i,name in enumerate(('trend','reversion','noise'))}
             for scope,row in zip(('all_updates','submitted_orders' if cfg.strategy in ('position','threshold','locked_position') else 'arrivals'),moments)}
    factors=factor_stats(moments)
    allocation={str(w):sum(c.trend_window==w for c in bot_configs) for w in sorted({c.trend_window for c in bot_configs})}
    summary=dict(factor_allocation={key:group_keys.count(key) for key in sorted(set(group_keys))},
                 group_factors={key:factor_stats(value) for key,value in group_moments.items()},
                 window_allocation=allocation,trades=int(counts.sum()),buy_trades=int(buy_counts.sum()),
                 model_valid=not rejected,rejected_orders=len(rejected),
                 first_rejection=dict(tick=rejected[0].time_seconds,reason=rejected[0].reason,
                                      price=float(rejected[0].price_before)) if rejected else None,
                 initial_price=float(prices[0]),final_price=float(prices[-1]),
                 min_price=float(prices.min()),max_price=float(prices.max()),
                 range_pct=float((prices.max()/prices.min()-1)*100),
                 expected_orders_per_tick=cfg.bots*cfg.rate,
                 expected_nominal_gold_per_tick=cfg.bots*cfg.rate*cfg.order_size if cfg.strategy=='fixed' else None,
                 max_foreign_step_per_arrival=2*cfg.position_cap*cfg.adjustment if cfg.strategy in ('position','threshold') else None,
                 actual_gold_volume=float(volume.sum()),net_pool_gold_flow=float(flow.sum()),
                 arrivals=int(arrivals.sum()),gross_foreign_turnover=str(gross_foreign),
                 net_foreign_position=str(total_position),max_abs_foreign_position=str(peak_individual),
                 max_abs_total_foreign_position=str(peak_total_position),
                 fees_gold=str(sim.pool.fees_gold),fees_foreign=str(sim.pool.fees_foreign),
                 pool_foreign_final=str(sim.pool.foreign),accounts=account_rows(),
                 first_rejection_snapshot=first_snapshot,
                 factors=factors,conservation='both currencies including fees verified')
    if cfg.strategy=='threshold':
        entries=sum(b.trader.entries for b in bots); exits=sum(b.trader.exits for b in bots)
        summary['threshold_activity']=dict(entries=entries,exits=exits,
            mean_completed_hold_ticks=sum(b.trader.completed_hold_ticks for b in bots)/exits if exits else None,
            thresholds={str(t):sum(c.entry_threshold==t for c in bot_configs) for t in sorted({c.entry_threshold for c in bot_configs})})
    if cfg.strategy=='locked_position':
        holds=[t for b in bots for t in b.trader.hold_ticks]
        summary['locked_activity']=dict(entries=sum(b.trader.entries for b in bots),
            exits=sum(b.trader.exits for b in bots),open_positions=sum(b.trader.position!=0 for b in bots),
            mean_completed_hold_ticks=float(np.mean(holds)) if holds else None,
            median_completed_hold_ticks=float(np.median(holds)) if holds else None,
            p90_completed_hold_ticks=float(np.quantile(holds,.9)) if holds else None,
            max_completed_hold_ticks=max(holds) if holds else None,completed_hold_ticks=holds,
            entry_threshold=cfg.entry_threshold,exit_threshold=cfg.exit_threshold,
            entry_sizing='Q*tanh(sensitivity*entry_signal); one order; actual fill locked',
            decisions='Poisson arrivals; at most one entry or exit per tick; no gradual adjustment')
        summary['max_foreign_step_per_arrival']=cfg.position_cap
    return dict(prices=prices,gold=gold,counts=counts,buy_counts=buy_counts,volume=volume,arrivals=arrivals),summary


def source_hashes():
    return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')}


def run_one(cfg,output,bootstrap,shuffles,hashes):
    directory=Path(output)/cfg.identifier(); directory.mkdir(parents=True,exist_ok=True)
    start=perf_counter()
    metadata=dict(config=asdict(cfg),source_hashes=hashes,bootstrap=bootstrap,shuffle_repetitions=shuffles)
    status_path=directory/'result.json'
    if status_path.exists():
        previous=json.loads(status_path.read_text())
        if previous.get('status')=='complete' and previous.get('metadata')==metadata:
            return previous
    result=dict(id=cfg.identifier(),status='running',metadata=metadata)
    status_path.write_text(json.dumps(result,indent=2))
    try:
        series,summary=simulate(cfg)
        np.savez_compressed(directory/'series.npz',**series)
        result['simulation']=summary
        if not summary['model_valid']:
            result['status']='invalid_model_regime'
        else:
            diagnostics=analyze(series['prices'],series['gold'],burn_in=cfg.burn_in,bootstrap=bootstrap,
                                seed=cfg.seed,shuffle_repetitions=shuffles)
            (directory/'diagnostics.json').write_text(json.dumps(diagnostics,indent=2))
            result.update(status='complete',metrics=compact(diagnostics))
    except Exception as error:
        result.update(status='failed',error=repr(error),traceback=traceback.format_exc())
    result['seconds']=perf_counter()-start
    status_path.write_text(json.dumps(result,indent=2))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--bootstrap',type=int,default=0)
    parser.add_argument('--shuffles',type=int,default=1)
    args=parser.parse_args()
    if args.workers<1 or args.shuffles<1 or args.bootstrap<0:
        parser.error('positive workers/shuffles and nonnegative bootstrap required')
    configs=expand(json.loads(args.config.read_text()))
    args.output.mkdir(parents=True,exist_ok=True)
    hashes=source_hashes()
    (args.output/'manifest.json').write_text(json.dumps(dict(configs=[asdict(c) for c in configs],
        source_hashes=hashes,bootstrap=args.bootstrap,shuffles=args.shuffles),indent=2))
    results=[]
    print(f'{len(configs)} runs, workers={args.workers}, bootstrap={args.bootstrap}',flush=True)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        jobs=[pool.submit(run_one,c,str(args.output),args.bootstrap,args.shuffles,hashes) for c in configs]
        for future in as_completed(jobs):
            result=future.result();results.append(result)
            (args.output/'summary.json').write_text(json.dumps(results,indent=2))
            c=result['metadata']['config']
            print(f"{len(results)}/{len(configs)} {result['status']} {result['id']} "
                  f"a={c['trend_strength']} b={c['reversion_strength']} noise={c['noise_sigma']} "
                  f"L={c['trend_window']} N={c['bots']} size={c['order_size']} "
                  f"seed={c['seed']} seconds={result['seconds']:.1f}",flush=True)
    if any(r['status']=='failed' for r in results):
        raise SystemExit('Some runs failed: inspect individual result.json; no silent exclusions')


if __name__=='__main__':
    main()
