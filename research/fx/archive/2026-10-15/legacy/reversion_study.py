"""Paired medium-length runs changing only the mean-reversion coefficient."""

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from ..amm import Account, Pool
from ..diagnostics import acf, describe
from ..expectation import ExpectationTrader
from ..simulation import Simulation


class ObservedTrader(ExpectationTrader):
    def __init__(self, account, moments, strength, window=30):
        super().__init__(account, reversion_strength=strength,
                         trend_window=window, mean_window=window)
        self.moments = moments

    def decide(self, view, rng):
        orders = super().decide(view, rng)
        if view.tick >= max(self.trend_window, self.mean_window):
            values = (self.trend_component, self.reversion_component, self.noise_component)
            for row, weight in zip(self.moments, (1, len(orders))):
                row[0] += weight
                for i, value in enumerate(values):
                    row[1+i] += weight*value
                    row[4+i] += weight*value*value
        return orders


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticks", type=int, default=20000)
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    parser.add_argument("--strengths", type=float, nargs="+", default=[.1, 1.0])
    parser.add_argument("--window", type=int, default=30)
    parser.add_argument("--output", type=Path, default=Path("research/fx/results/reversion-comparison"))
    args = parser.parse_args()
    if args.window < 1 or args.ticks - args.window < 1000:
        parser.error("need a positive window and at least 1000 post-window ticks")
    args.output.mkdir(parents=True, exist_ok=True)
    params = dict(ticks=args.ticks, seeds=args.seeds, strengths=args.strengths,
                  bots=20, gold_size=200, rate_per_tick=.05, fee_rate=.0005,
                  gold=1000000, foreign=200000, window=args.window, trend_strength=30,
                  noise_sigma=.005, sensitivity=100, burn_in=args.window,
                  source_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in Path(__file__).parent.glob("*.py")})
    (args.output/'parameters.json').write_text(json.dumps(params,indent=2))
    results=[]
    paths={}
    for seed in args.seeds:
        reference_arrivals=None
        for strength in args.strengths:
            print(f"run seed={seed} reversion={strength} ticks={args.ticks}",flush=True)
            moments=[[0.0]*7 for _ in range(2)]
            bots=[ObservedTrader(Account(str(i),Decimal('1e12'),Decimal('2e11')),moments,strength,args.window)
                  for i in range(20)]
            sim=Simulation(Pool(Decimal('1000000'),Decimal('200000')),bots,
                           seed=seed,history_limit=args.window)
            sim.run(args.ticks,1)
            assert all(record.kind=='trade' for record in sim.records)
            assert sim.pool.gold+sim.pool.fees_gold+sum(b.account.gold for b in bots)==Decimal('20000001000000')
            assert sim.pool.foreign+sim.pool.fees_foreign+sum(b.account.foreign for b in bots)==Decimal('4000000200000')
            arrivals=[(r.time_seconds,r.actor) for r in sim.records]
            if reference_arrivals is None:
                reference_arrivals=arrivals
            else:
                assert arrivals==reference_arrivals, 'paired arrival process changed'
            prices=np.array([float(p.price) for p in sim.prices])
            returns=np.diff(np.log(prices))[args.window:]
            normalized=returns*np.sqrt(prices[:-1][args.window:]/5)
            # sqrt(P/5) approximates relative pool depth for a conserved-k pool;
            # six-place output rounding can cause tiny deviations.
            directory=args.output/f'seed-{seed}-b-{strength:g}'
            directory.mkdir(exist_ok=True)
            np.savez_compressed(directory/'series.npz',prices=prices)
            sim.write_chart(directory)
            contribution={}
            for scope,row in zip(('all_updates','order_arrivals'),moments):
                n=row[0]
                contribution[scope]={name:float(np.sqrt(max(0,row[4+i]/n-(row[1+i]/n)**2)))
                                     for i,name in enumerate(('trend','reversion','noise'))}
            result=dict(seed=seed,reversion_strength=strength,trades=len(sim.records),
                        final_price=float(prices[-1]),min_price=float(prices.min()),max_price=float(prices.max()),
                        range_pct=float((prices.max()/prices.min()-1)*100),
                        tick_return=describe(returns),
                        acf_lag1=float(acf(returns,100)[1]),
                        normalized_abs_acf_100=float(acf(abs(normalized),100)[100]),
                        factor_std=contribution)
            results.append(result); paths[(seed,strength)]=prices
            (args.output/'summary.json').write_text(json.dumps(results,indent=2))
            print(f"done: trades={len(sim.records)} final={prices[-1]:.5f} "
                  f"range={result['range_pct']:.3f}%",flush=True)
    fig,axes=plt.subplots(len(args.seeds),1,figsize=(11,3*len(args.seeds)),
                          constrained_layout=True,squeeze=False)
    palette=plt.cm.Blues(np.linspace(.5,.95,len(args.strengths)))
    for axis,seed in zip(axes[:,0],args.seeds):
        for i,strength in enumerate(args.strengths):
            prices=paths[(seed,strength)]
            index=np.linspace(0,args.ticks,min(4000,args.ticks+1)).astype(int)
            axis.plot(index,prices[index],label=f'b={strength:g}',color=palette[i])
        axis.set(title=f'Seed {seed}: window={args.window}, only mean reversion changes',ylabel='Gold / foreign',xlabel='Tick')
        axis.legend(); axis.spines[['top','right']].set_visible(False)
    fig.savefig(args.output/'comparison.png',dpi=150); plt.close(fig)
    print(f'saved {args.output}',flush=True)


if __name__=='__main__':
    main()
