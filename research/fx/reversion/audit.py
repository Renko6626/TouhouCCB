"""Independent saved-quote and state checks for the new inventory role."""
from decimal import Decimal as D
import hashlib
import json
import math

import numpy as np

from research.fx.common.amm import Account,Pool,Order
from research.fx.common.execution import execute
from research.fx.factor_activity.run import ROOT
from .run import OUTPUT


def main():
    manifest=json.loads((OUTPUT/'manifest.json').read_text());rs=json.loads((OUTPUT/'summary.json').read_text())
    assert len(rs)==18 and all(r['status']=='complete' for r in rs)
    for p,h in {**manifest['source_sha256'],**manifest['protected_sha256']}.items():assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h,p
    counts=dict(paths=0,episode_quotes=0,inventory_quotes=0,reference_ticks=0)
    for r in rs:
        cfg=r['config'];n=cfg['ticks'];w=cfg['inventory_window'];directory=OUTPUT/r['name']
        cases={}
        for label in ['normal','up','down']:
            with np.load(directory/f'{label}.npz') as z:x={k:z[k] for k in z.files}
            cases[label]=x;counts['paths']+=1
            logs=np.log(x['prices']);sums=np.r_[0,np.cumsum(logs)];ticks=np.arange(1,n+1);starts=np.maximum(0,ticks-w)
            ref=(sums[ticks]-sums[starts])/(ticks-starts)
            np.testing.assert_allclose(np.log(x['slow_reference'][1:]),ref,atol=5e-13,rtol=0)
            np.testing.assert_allclose(x['slow_gap'][1:],ref-logs[:-1],atol=5e-13,rtol=0)
            if r['inventory_count']:
                np.testing.assert_allclose(x['inventory_target'][1:],cfg['inventory_position_cap']*np.tanh(cfg['inventory_sensitivity']*x['slow_gap'][1:]),atol=1e-10,rtol=0)
            a=math.exp(-1/cfg['tau'])
            np.testing.assert_allclose(x['z_pre'][2:],a*x['z_pre'][1:-1]+math.sqrt(1-a*a)/cfg['psi']*x['returns'][1:-1],atol=1e-12,rtol=1e-12)
            np.testing.assert_allclose(x['intensity'][1:],.75+.25*x['z_pre'][1:]**2,atol=1e-12,rtol=1e-12)
            np.testing.assert_allclose(x['pool_foreign']+x['random_net_position']+x['factor_net_position']+x['reversion_net_position'],200000+np.cumsum(x['injection_foreign']),atol=1e-8,rtol=0)
            np.testing.assert_allclose(x['gold']+x['random_gold']+x['factor_gold']+x['reversion_gold'],1100000+np.cumsum(x['injection_gold']),atol=1e-8,rtol=0)
            counts['reference_ticks']+=n
            ledgers={}
            for kind in ['fills','inventory']:
                fills=[json.loads(line) for line in (directory/f'{label}-{kind}.jsonl').read_text().splitlines()]
                assert len(fills)==len({(f['account'],f['tick']) for f in fills})
                ledgers[kind]=fills
                for f in fills:
                    pool=Pool(D(f['pool_before']['gold']),D(f['pool_before']['foreign']),D(str(cfg['fee_rate'])))
                    account=Account(str(f['account']),D(f['before_gold']),D(f['before_foreign']))
                    if kind=='fills':
                        execute(pool,account,f['wanted'],cfg['max_pool_fraction']);counts['episode_quotes']+=1
                    else:
                        delta=D(f['after_foreign'])-D(f['before_foreign'])
                        order=Order('sell',-delta) if delta<0 else Order('buy',-D(f['gold_change']))
                        pool.trade(account,order);counts['inventory_quotes']+=1
                        assert abs(float(f['target'])-x['inventory_target'][f['tick']])<1e-9
                        assert abs(f['gap']-x['slow_gap'][f['tick']])<1e-13
                        wanted=D(str(cfg['inventory_adjustment']))*(D(f['target'])-(D(f['before_foreign'])-D(str(cfg['initial_foreign']))))
                        assert abs(float(wanted)-f['wanted'])<1e-12
                    assert account.gold==D(f['after_gold']) and account.foreign==D(f['after_foreign'])
                    assert pool.gold==D(f['pool_after']['gold']) and pool.foreign==D(f['pool_after']['foreign'])
            episodes=json.loads((directory/f'{label}-episodes.json').read_text())
            assert len(episodes)==int(x['entries'].sum())
            assert sum(e['close_tick'] is not None for e in episodes)==int(x['completions'].sum())
            s=r['summaries'][label]
            assert D(s['pool_gold'])+sum(D(a['gold']) for a in s['accounts'])==D('1100000')+D(s['issued_gold'])
            assert D(s['pool_foreign'])+sum(D(a['foreign']) for a in s['accounts'])==D('220000')+D(s['issued_foreign'])
        t0=manifest['spec']['shock_tick']
        for label,jump in [('up',.05),('down',-.05)]:
            x=cases[label];c=cases['normal'];s=r['summaries'][label]
            assert all(np.array_equal(x[k][:t0],c[k][:t0]) for k in x)
            assert x['slow_reference'][t0]==c['slow_reference'][t0]
            observed=float(s['issue']['post_price'])/float(s['issue']['pre_price'])-1
            assert abs(observed-jump)<1e-11
    result={**counts,'protected_files_unchanged':len(manifest['protected_sha256']),
            'causal_references':True,'both_issued_assets_conserved':True,'actual_quotes_replayed':True}
    (OUTPUT/'audit.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))


if __name__=='__main__':main()
