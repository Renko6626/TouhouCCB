"""Recheck intervention timing, issue accounting and recovery from saved arrays."""
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from research.fx.factor_activity.run import ROOT
from .run import OUTPUT


def main():
    manifest = json.loads((OUTPUT/'manifest.json').read_text())
    records = json.loads((OUTPUT/'summary.json').read_text())
    cfg, spec = manifest['config'], manifest['spec']
    t0 = spec['shock_tick']
    assert len(records)==100 and all(r['status']=='complete' for r in records)
    for p,h in manifest['source_sha256'].items():
        assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h
    recovery_checks, arrays = 0, 0
    for r in records:
        with np.load(OUTPUT/r['name']/'control.npz') as control, np.load(OUTPUT/r['name']/'shock.npz') as shock:
            for case,x,summary,issue_amount in [('control',control,r['control_summary'],0),
                                                ('shock',shock,r['shock_summary'],spec['injection_gold'])]:
                for field in x.files:
                    assert len(x[field])==cfg['ticks']+1 and np.isfinite(x[field]).all()
                    arrays+=1
                np.testing.assert_allclose(x['returns'][1:], np.log(x['prices'][1:]/x['prices'][:-1]),atol=1e-15)
                a = math.exp(-1/cfg['tau'])
                np.testing.assert_allclose(x['z_pre'][2:],a*x['z_pre'][1:-1]+math.sqrt(1-a*a)/cfg['psi']*x['returns'][1:-1],rtol=1e-12,atol=1e-13)
                np.testing.assert_allclose(x['intensity'][1:],1-r['b']+r['b']*x['z_pre'][1:]**2,rtol=1e-12)
                assert x['injection_gold'].sum()==issue_amount
                for field in ['arrivals','trades','raw_abs_demand','executed_abs_quantity','actual_flow','clipped_orders']:
                    np.testing.assert_allclose(x[field],x['random_'+field]+x['factor_'+field],rtol=1e-12,atol=1e-9)
                for group in ['random','factor']:
                    np.testing.assert_allclose(x[group+'_net_position'][1:]-x[group+'_net_position'][:-1],x[group+'_actual_flow'][1:],atol=1e-8)
                np.testing.assert_allclose(x['pool_foreign'],cfg['pool_foreign']-x['random_net_position']-x['factor_net_position'],atol=1e-7,rtol=1e-12)
                assert x['wallet_gold_min'].min()>=0 and x['wallet_foreign_min'].min()>=0
                total_gold=Decimal(summary['pool_gold'])+sum(Decimal(w['gold']) for w in summary['accounts'])
                total_foreign=Decimal(summary['pool_foreign'])+sum(Decimal(w['foreign']) for w in summary['accounts'])
                assert total_gold==Decimal(str(cfg['pool_gold']))+cfg['accounts']*Decimal(str(cfg['initial_gold']))+issue_amount
                assert total_foreign==Decimal(str(cfg['pool_foreign']))+cfg['accounts']*Decimal(str(cfg['initial_foreign']))
                info=summary['issue']
                assert Decimal(info['post_gold'])-Decimal(info['pre_gold'])==issue_amount
                assert info['post_foreign']==info['pre_foreign']
                assert Decimal(info['post_k'])-Decimal(info['pre_k'])==issue_amount*Decimal(info['pre_foreign'])
                np.testing.assert_allclose(x['prices'][t0],float(info['post_price']),atol=1e-14)
            for field in control.files:
                np.testing.assert_array_equal(control[field][:t0],shock[field][:t0])
            for field in ['trades','gold_volume','actual_flow','executed_abs_quantity','arrivals']:
                assert shock[field][t0]==control[field][t0]
            info=r['shock_summary']['issue'];pre=float(info['pre_price']);post=float(info['post_price']);delta=post-pre
            assert control['prices'][t0]==pre
            assert abs(delta/pre-float(Decimal(spec['injection_gold'])/Decimal(info['pre_gold'])))<1e-14

            def check_response(record,ps,pc):
                expected={'price':ps,'control_price':pc,'price_change_from_pre':ps/pre-1,
                          'retraced_fraction':(post-ps)/delta,'paired_retraced_fraction':1-(ps-pc)/delta}
                for key,val in expected.items():
                    assert abs(record[key]-val)<1e-10,(r['name'],key)

            for h in r['response']['horizons']:
                end=t0+h['horizon'];width=h['trailing_width']
                check_response(h['point'],shock['prices'][end],control['prices'][end])
                check_response(h['trailing'],np.mean(shock['prices'][end-width+1:end+1]),np.mean(control['prices'][end-width+1:end+1]))
                recovery_checks+=2
            width=spec['terminal_window'];end=r['response']['terminal']
            check_response(end,np.mean(shock['prices'][-width:]),np.mean(control['prices'][-width:]))
            assert end['within_pre_one_percent']==(abs(end['price_change_from_pre'])<=.01)
            for name,limit in [('half',pre+.5*delta),('full',pre)]:
                hits=np.flatnonzero(shock['prices'][t0+1:]<=limit)
                assert end['first_'+name+'_hit']==(int(hits[0]+1) if len(hits) else None)
            recovery_checks+=1
    before_path=Path('/tmp/sunyunbo/fx-injection-before.json')
    protected=None
    if before_path.exists():
        before=json.loads(before_path.read_text())
        assert all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in before.items())
        protected=len(before)
    result=dict(pairs=len(records),paths=2*len(records),finite_arrays=arrays,recovery_checks=recovery_checks,
                pre_issue_prefixes_exact=True,issue_not_counted_as_trade=True,asset_conservation_with_issue=True,
                feedback_timing=True,group_flows_and_inventories=True,nonnegative_recorded_wallets=True,
                old_files_unchanged=protected,source_hashes=True)
    (OUTPUT/'audit.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__=='__main__':main()
