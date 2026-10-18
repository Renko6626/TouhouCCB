"""Independent saved-ledger checks for cost basis, exits and market responses."""
from decimal import Decimal as D
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve

from backend.app.services.fx.amm import quote_buy_exact_out,quote_sell
from research.fx.common.amm import Account,Pool
from research.fx.common.execution import execute
from research.fx.factor_activity.run import ROOT
from .run import OUTPUT


def main():
    manifest=json.loads((OUTPUT/'manifest.json').read_text());spec=manifest['spec'];profiles=manifest['profiles']
    records=json.loads((OUTPUT/'summary.json').read_text())
    assert len(records)==48 and all(r['status']=='complete' for r in records)
    for path,h in manifest['source_sha256'].items():
        assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==h,path
    counters=dict(paths=0,episodes=0,behavior_fills=0,entry_signal_checks=0,acf_windows=0,response_checks=0)
    for r in records:
        cfg=r['config'];directory=OUTPUT/r['stage']/r['name'];count=r['random_count'];n=cfg['accounts']
        labels=['series'] if r['stage']=='normal' else ['control','shock']
        for label in labels:
            s=r['summary'] if label=='series' else r[label+'_summary']
            episodes=json.loads((directory/f'{label}-episodes.json').read_text())
            fills=[json.loads(line) for line in (directory/f'{label}-fills.jsonl').read_text().splitlines()]
            counters['paths']+=1;counters['episodes']+=len(episodes);counters['behavior_fills']+=len(fills)
            with np.load(directory/f'{label}.npz') as x:
                assert all(len(x[k])==cfg['ticks']+1 and np.isfinite(x[k]).all() for k in x.files)
                np.testing.assert_allclose(x['returns'][1:],np.log(x['prices'][1:]/x['prices'][:-1]),atol=1e-15)
                a=math.exp(-1/cfg['tau'])
                np.testing.assert_allclose(x['z_pre'][2:],a*x['z_pre'][1:-1]+math.sqrt(1-a*a)/cfg['psi']*x['returns'][1:-1],atol=1e-13,rtol=1e-12)
                np.testing.assert_allclose(x['intensity'][1:],1-r['b']+r['b']*x['z_pre'][1:]**2,rtol=1e-12)
                np.testing.assert_allclose(x['gold']+x['random_gold']+x['factor_gold'],
                    cfg['pool_gold']+n*cfg['initial_gold']+np.cumsum(x['injection_gold']),atol=1e-8,rtol=0)
                np.testing.assert_allclose(x['pool_foreign']+x['random_net_position']+x['factor_net_position'],cfg['pool_foreign'],atol=1e-8,rtol=0)
                np.testing.assert_allclose(x['factor_net_position'],x['long_quantity']-x['short_quantity'],atol=1e-9)
                np.testing.assert_allclose(x['factor_gold']-(n-count)*cfg['initial_gold'],
                    x['realized_pnl']-x['long_basis']+x['short_basis'],atol=1e-8,rtol=0)
                assert x['wallet_gold_min'].min()>=0 and x['wallet_foreign_min'].min()>=0
                for field in ['arrivals','trades','raw_abs_demand','executed_abs_quantity','actual_flow','clipped_orders']:
                    np.testing.assert_allclose(x[field],x['random_'+field]+x['factor_'+field],atol=1e-8,rtol=1e-12)
                for group in ['random','factor']:
                    np.testing.assert_allclose(np.cumsum(x[group+'_actual_flow']),x[group+'_net_position'],atol=1e-7,rtol=0)
                per_ep={e['id']:[] for e in episodes};seen=set();raw_pnl=np.zeros(cfg['ticks']+1)
                raw_flow=np.zeros(cfg['ticks']+1);raw_fills=np.zeros(cfg['ticks']+1);opened=np.zeros(cfg['ticks']+1);closed=np.zeros(cfg['ticks']+1)
                logs=np.log(x['prices']);noise={}
                for i in range(count,n):
                    noise[i]=np.random.default_rng(r['seed']*1000003+i*101+2).normal(size=cfg['ticks'])
                for f in fills:
                    i,t=f['account'],f['tick'];assert (i,t) not in seen;seen.add((i,t))
                    assert i>=count
                    tmp=Pool(D(f['pool_before']['gold']),D(f['pool_before']['foreign']),D(str(cfg['fee_rate'])))
                    acc=Account(str(i),D(f['before_gold']),D(f['before_foreign']))
                    q=execute(tmp,acc,f['wanted'],cfg['max_pool_fraction'])
                    assert q is not None and str(acc.gold)==f['after_gold'] and str(acc.foreign)==f['after_foreign']
                    assert str(tmp.gold)==f['pool_after']['gold'] and str(tmp.foreign)==f['pool_after']['foreign']
                    raw_flow[t]+=float(D(f['after_foreign'])-D(f['before_foreign']));raw_fills[t]+=1
                    raw_pnl[t]+=float(f['realized_profit']);per_ep[f['episode_id']].append(f)
                    if f['action']=='open':
                        opened[t]+=1;p=profiles[i];tw,mw=cfg['trend_window'],cfg['mean_window']
                        trend=(logs[t-1]-logs[t-tw])/tw if t>=tw else 0
                        mean=np.mean(logs[t-mw:t])-logs[t-1] if t>=mw else 0
                        g=p['trend']*trend+p['reversion']*mean+p['sigma']*noise[i][t-1]
                        assert abs(g)>=p['sigma']-1e-12 and f['direction']==(1 if g>0 else -1)
                        counters['entry_signal_checks']+=1
                    elif f['complete']:closed[t]+=1
                np.testing.assert_allclose(raw_flow,x['factor_actual_flow'],atol=1e-9)
                np.testing.assert_allclose(raw_fills,x['factor_trades'],atol=0)
                np.testing.assert_allclose(np.cumsum(raw_pnl),x['realized_pnl'],atol=1e-8)
                np.testing.assert_array_equal(opened,x['entries']);np.testing.assert_array_equal(closed,x['completions'])
                pnl_total=D('0')
                for e in episodes:
                    ff=per_ep[e['id']];first=ff[0];assert first['action']=='open' and len([f for f in ff if f['action']=='open'])==1
                    assert first['tick']==e['entry_tick'] and D(first['quantity'])==D(e['entry_quantity'])
                    assert abs(D(first['gold_change']))==D(e['entry_gold'])
                    remaining,basis=D(e['entry_quantity']),D(e['entry_gold']);ep_pnl=D('0');latched=None
                    for f in ff[1:]:
                        assert f['action']=='close';qty=D(f['quantity']);allocated=D(f['allocated_basis'])
                        assert 0<qty<=remaining and f['direction']==e['direction']
                        if e['direction']==1:
                            mark=quote_sell(remaining,D(f['pool_before']['gold']),D(f['pool_before']['foreign']),D(str(cfg['fee_rate'])))
                            rate=float((mark.output_amount-basis)/basis)
                            actual_profit=D(f['gold_change'])-allocated
                        else:
                            mark=quote_buy_exact_out(remaining,D(f['pool_before']['gold']),D(f['pool_before']['foreign']),D(str(cfg['fee_rate'])))
                            rate=float((basis-mark.input_amount)/basis)
                            actual_profit=allocated+D(f['gold_change'])
                        assert abs(rate-f['mark_return'])<1e-12
                        if latched is None:
                            if f['reason']=='take_profit':assert rate>=cfg['take_profit']
                            elif f['reason']=='stop_loss':assert rate<=-cfg['stop_loss']
                            else:
                                assert r['variant']=='trailing' and f['reason']=='trailing'
                                assert f['peak_return']>=cfg['trail_arm'] and f['peak_return']-rate>=cfg['trail_drawdown']
                            latched=f['reason']
                        assert f['reason']==latched and actual_profit==D(f['realized_profit'])
                        assert abs(allocated-(basis if qty==remaining else basis*qty/remaining))<D('1e-18')
                        remaining-=qty;basis-=allocated;ep_pnl+=actual_profit
                        assert remaining==D(f['remaining']) and abs(basis-D(f['basis']))<D('1e-18')
                    assert remaining==D(e['remaining']) and abs(basis-D(e['basis']))<D('1e-18')
                    assert ep_pnl==D(e['realized_profit']);pnl_total+=ep_pnl
                    assert (remaining==0)==(e['close_tick'] is not None)
                    if remaining==0:assert ff[-1]['complete'] and ff[-1]['tick']==e['close_tick']
                assert pnl_total==D(s['behavior']['realized_pnl'])
                assert len(episodes)==s['behavior']['opened']
                assert sum(e['close_tick'] is not None for e in episodes)==s['behavior']['completed']
                if label=='series':
                    windows=[(cfg['burn_in']+1,cfg['ticks'],r['metrics'])]+[(z['first_tick'],z['last_tick'],z['metrics']) for z in r['segments']]
                    for low,high,m in windows:
                        returns=x['returns'][low:high+1]
                        assert abs(np.std(returns,ddof=1)-m['std'])<1e-15
                        for tag,values in [('signed',returns),('abs',abs(returns)),('sq',returns*returns)]:
                            centered=values-values.mean();den=float(centered@centered)
                            if den==0:
                                assert all(m[f'{tag}_{lo}_{hi}'] is None for lo,hi in [(1,1),(101,300),(301,1000)])
                                if tag=='signed':assert m['max_signed'] is None
                                continue
                            curve=fftconvolve(centered,centered[::-1],mode='full')[len(values)-1:len(values)+1000]/den
                            for lo,hi in [(1,1),(101,300),(301,1000)]:assert abs(curve[lo:hi+1].mean()-m[f'{tag}_{lo}_{hi}'])<1e-12
                            if tag=='signed':assert abs(np.max(abs(curve[1:]))-m['max_signed'])<1e-12
                        counters['acf_windows']+=1
        if r['stage']=='shock':
            t0=spec['shock_tick']
            normal=OUTPUT/'normal'/r['name']/'series.npz'
            with np.load(directory/'control.npz') as control,np.load(directory/'shock.npz') as shock,np.load(normal) as full:
                for key in control.files:
                    np.testing.assert_array_equal(control[key][:t0],shock[key][:t0])
                    np.testing.assert_array_equal(control[key],full[key][:len(control[key])])
                for key in ['trades','actual_flow','gold_volume','factor_trades']:assert control[key][t0]==shock[key][t0]
                info=r['shock_summary']['issue'];pre=float(info['pre_price']);post=float(info['post_price']);jump=post-pre
                assert abs(control['prices'][t0]-pre)<1e-14 and abs(shock['prices'][t0]-post)<1e-14
                assert shock['injection_gold'].sum()==spec['injection_gold'] and control['injection_gold'].sum()==0
                for label,x,s,amount in [('control',control,r['control_summary'],0),('shock',shock,r['shock_summary'],spec['injection_gold'])]:
                    assert D(s['pool_gold'])+sum(D(a['gold']) for a in s['accounts'])==D(str(cfg['pool_gold']+n*cfg['initial_gold']+amount))
                    assert D(s['pool_foreign'])+sum(D(a['foreign']) for a in s['accounts'])==D(str(cfg['pool_foreign']+n*cfg['initial_foreign']))
                for item in r['response']['horizons']:
                    end=t0+item['horizon'];width=item['trailing_width']
                    for mode,ps,pc in [('point',shock['prices'][end],control['prices'][end]),
                                       ('trailing',shock['prices'][end-width+1:end+1].mean(),control['prices'][end-width+1:end+1].mean())]:
                        assert abs(item[mode]['retraced_fraction']-(post-ps)/jump)<1e-12
                        assert abs(item[mode]['paired_retraced_fraction']-(1-(ps-pc)/jump))<1e-12
                        counters['response_checks']+=1
                width=spec['terminal_window'];terminal=r['response']['terminal']
                ps=shock['prices'][-width:].mean();pc=control['prices'][-width:].mean()
                assert abs(terminal['price_change_from_pre']-(ps/pre-1))<1e-12
                assert abs(terminal['retraced_fraction']-(post-ps)/jump)<1e-12
                assert abs(terminal['paired_retraced_fraction']-(1-(ps-pc)/jump))<1e-12
                counters['response_checks']+=1
    before=json.loads((OUTPUT/'preexisting_sha256.json').read_text())
    assert all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in before.items())
    result=dict(**counters,old_files_unchanged=len(before),per_tick_issued_asset_conservation=True,
                inventory_basis_and_cash_reconciled=True,real_quote_fills_replayed=True,
                constant_acf_preserved_undefined=True,normal_control_prefixes_exact=True,shock_prefixes_exact=True,
                future_free_entry_signals=True,source_hashes=True)
    (OUTPUT/'audit.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__=='__main__':main()
