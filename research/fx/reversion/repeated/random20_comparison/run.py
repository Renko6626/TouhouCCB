"""Fixed 12-path comparison. Run from repository root; no production writes."""
import csv, hashlib, importlib.util, json, difflib
from pathlib import Path
from decimal import Decimal
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from backend.app.services.fx.amm import quote_sell
from research.fx.reversion.repeated.random20_comparison.model import simulate
from research.fx.reversion.repeated.timing_metrics import price_ranges,event_response
from research.fx.common.diagnostics import acf
ROOT=Path.cwd(); HERE=Path(__file__).parent; BASE=HERE.parent

def dump(path,value):path.write_text(json.dumps(value,indent=2,ensure_ascii=False,allow_nan=False)+'\n')
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def run():
 f=json.loads((BASE/'FROZEN_CONFIG.json').read_text()); cfg=f['research_reproduction']['selected_simulator_config']; schedule={int(k):v for k,v in f['research_reproduction']['synthetic_shock_schedule'].items()}
 protected={str(p.relative_to(ROOT)):digest(p) for p in (ROOT/'research/fx').rglob('*') if p.is_file() and HERE not in p.parents and '__pycache__' not in p.parts}
 sources=[ROOT/'backend/app/services/fx/amm.py',ROOT/'backend/app/services/fx/quantize.py']
 protected.update({str(p.relative_to(ROOT)):digest(p) for p in sources})
 manifest=dict(config=cfg,profiles=f['profiles'],roles={'mixed_10_5_5':[10,5,5],'random_20':[20,0,0]},seeds=[81,82,83],scenarios=['normal','shock'],schedule=schedule,activity_b=f['activity_b'],policy=f['policy'],trend_multiplier=f['trend_multiplier'],protected_sha256=protected,driver_sha256=digest(Path(__file__)),model_sha256=digest(HERE/'model.py'),notebook_dependencies={x:importlib.util.find_spec(x) is not None for x in ['nbformat','nbclient']},metrics_definitions={'trades_per_minute':'all filled orders / (10000*5/60)','foreign_volume_per_minute':'sum absolute executed foreign / minutes','range30':'median across 27 complete nonoverlap 360-tick windows of (high-low)/window initial price; then equal seed mean','wallet_exhaustion':'balance <= 1e-6, account-tick fraction including tick0 and final counts','below3':'actual cash + foreign*marginal price <3; no production death status','attempt_ratios':'clipped and skipped / order_attempts; no-intent / arrivals separately','vs_hodl':'final wallet marginal MTM minus (5000+1000*same-path final price)','liquidation':'per-account independent sale of all foreign into final pool plus cash; simultaneous aggregate sale separately; no realized PnL','shock_response':'existing event_response 120 ticks, mean last12 closes relative instant shock; terminal excluded; 9 correlated events/path,3 independent seeds','returns':'shock instant jump removed using pre-injection close; std and signed ACF1,12,60'},limits=['zero fees','no players/funding/reset','13h53m horizon','same seed is not paired endogenous price/intensity after divergence'])
 dump(HERE/'manifest.json',manifest)
 (HERE/'model.diff').write_text(''.join(difflib.unified_diff((BASE/'entry_model.py').read_text().splitlines(True),(HERE/'model.py').read_text().splitlines(True),fromfile='frozen/entry_model.py',tofile='random20_comparison/model.py')))
 rows=[]; validations=[]
 for arm,random_n,inv_n in [('mixed_10_5_5',10,5),('random_20',20,0)]:
  for scenario in ['normal','shock']:
   for seed in [81,82,83]:
    x,s,episodes,fills,inventory,decisions=simulate(cfg,f['profiles'],inv_n,f['activity_b'],seed,schedule if scenario=='shock' else {},f['policy'],{'trend_multiplier':f['trend_multiplier']},random_count=random_n)
    directory=HERE/f'{arm}-{scenario}-seed{seed}';directory.mkdir(exist_ok=True)
    np.savez_compressed(directory/'trace.npz',**x)
    for name,v in [('summary',s),('episodes',episodes),('fills',fills),('inventory',inventory),('decisions',decisions)]:dump(directory/f'{name}.json',v)
    assert all(np.isfinite(v).all() for v in x.values())
    assert x['account_gold'].min()>=0 and x['account_foreign'].min()>=0
    np.testing.assert_allclose(x['account_wealth'],x['account_gold']+x['account_foreign']*x['prices'][:,None],atol=1e-8,rtol=0)
    for pool,wallet,initial,injection in [('gold','account_gold',1100000,'injection_gold'),('pool_foreign','account_foreign',220000,'injection_foreign')]:np.testing.assert_allclose(x[pool]+x[wallet].sum(axis=1),initial+np.cumsum(x[injection]),atol=1e-8,rtol=0)
    np.testing.assert_array_equal(x['account_trades'].sum(axis=1),x['trades'])
    baseline=False
    if arm=='mixed_10_5_5':
     with np.load(BASE/f'results/short-exit-tuning/profit_trail04-{scenario}-seed{seed}/trace.npz') as old:
      assert set(old.files)==set(x)
      for k in x:np.testing.assert_array_equal(x[k],old[k],err_msg=k)
     baseline=True
    if scenario=='shock':
     with np.load(HERE/f'{arm}-normal-seed{seed}/trace.npz') as normal:
      for k in x:np.testing.assert_array_equal(x[k][:1000],normal[k][:1000],err_msg=k)
    r=x['returns'][1:].copy();events=[]
    for i,e in enumerate(s['events']):
     r[e['tick']-1]=np.log(float(e['pre_price'])/x['prices'][e['tick']-1]);end=s['events'][i+1]['tick']-1 if i+1<len(s['events']) else cfg['ticks'];events.append(event_response(x['prices'],e,end,5))
    observed=[e for e in events if e['retraced_10m'] is not None]; attempts=x['order_attempts'].sum(); arrivals=x['arrivals'].sum(); ac=acf(r,60)
    cash=x['account_gold'];fx=x['account_foreign'];wealth=x['account_wealth'];price=x['prices'];mins=cfg['ticks']/12
    per_liq=[]
    for a in s['accounts']:
     q=Decimal(a['foreign']);per_liq.append(float(Decimal(a['gold'])+(quote_sell(q,Decimal(s['pool_gold']),Decimal(s['pool_foreign']),Decimal('0')).output_amount if q>0 else Decimal('0'))))
    totalfx=sum((Decimal(a['foreign']) for a in s['accounts']),Decimal('0'));totalcash=sum((Decimal(a['gold']) for a in s['accounts']),Decimal('0'))
    aggregate_liq=float(totalcash+quote_sell(totalfx,Decimal(s['pool_gold']),Decimal(s['pool_foreign']),Decimal('0')).output_amount)
    row=dict(arm=arm,scenario=scenario,seed=seed,trades_per_minute=float(x['trades'].sum()/mins),foreign_volume_per_minute=float(x['executed_abs_quantity'].sum()/mins),range30=price_ranges(price,5)['median'],return_std=float(r.std()),**{f'signed_acf{k}':float(ac[k]) for k in [1,12,60]},clipped_attempt_ratio=float(x['clipped_orders'].sum()/attempts),skipped_attempt_ratio=float(x['skipped_orders'].sum()/attempts),no_intent_arrival_ratio=float(x['idle_arrivals'].sum()/arrivals),zero_cash_buy_attempt_ratio=float(x['cash_saturated'].sum()/attempts),zero_foreign_sell_attempt_ratio=float(x['foreign_saturated'].sum()/attempts),cash_exhausted_fraction=float((cash<=1e-6).mean()),foreign_exhausted_fraction=float((fx<=1e-6).mean()),final_cash_exhausted=int((cash[-1]<=1e-6).sum()),final_foreign_exhausted=int((fx[-1]<=1e-6).sum()),below3_account_tick_fraction=float((wealth<3).mean()),final_below3=int((wealth[-1]<3).sum()),final_total_mtm=float(wealth[-1].sum()),final_total_vs_hodl=float((wealth[-1]-(5000+1000*price[-1])).sum()),final_mean_vs_hodl=float((wealth[-1]-(5000+1000*price[-1])).mean()),per_account_vs_hodl=(wealth[-1]-(5000+1000*price[-1])).tolist(),per_account_liquidation=per_liq,sum_independent_liquidations=sum(per_liq),aggregate_liquidation=aggregate_liq,mean_retraced_10m=float(np.mean([e['retraced_10m'] for e in observed])) if observed else None,up_retraced_10m=float(np.mean([e['retraced_10m'] for e in observed if e['jump']>0])) if observed else None,down_retraced_10m=float(np.mean([e['retraced_10m'] for e in observed if e['jump']<0])) if observed else None,observed_events=len(observed),events=events,short_holding_median_minutes=s['behavior']['median_completed_holding']/12 if inv_n else None,short_realized_per_account=float(s['behavior']['realized_pnl'])/5 if inv_n else None)
    rows.append(row);dump(HERE/'metrics.json',rows);validations.append(dict(arm=arm,scenario=scenario,seed=seed,frozen_baseline_exact=baseline,wallet_conservation_mtm_trade_counts=True,pre1000_prefix=True if scenario=='shock' else None));print('COMPLETE',arm,scenario,seed,flush=True)
 for p,h in protected.items():assert digest(ROOT/p)==h,p
 dump(HERE/'validation.json',dict(paths=len(rows),checks=validations,protected_files_unchanged=len(protected),all_checks_passed=True))
 scalar=[k for k,v in rows[0].items() if not isinstance(v,(list,dict))]
 with (HERE/'comparison.csv').open('w') as out:
  writer=csv.DictWriter(out,fieldnames=scalar);writer.writeheader();writer.writerows({k:r[k] for k in scalar} for r in rows)
 summary=[]
 for scenario in ['normal','shock']:
  for arm in ['mixed_10_5_5','random_20']:
   group=[r for r in rows if r['arm']==arm and r['scenario']==scenario];summary.append(dict(arm=arm,scenario=scenario,**{k:float(np.mean([r[k] for r in group])) for k in scalar if k not in ['arm','scenario','seed'] and all(r[k] is not None for r in group)}))
 dump(HERE/'summary.json',summary)
 plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False});fig,axes=plt.subplots(1,2,figsize=(11,4))
 for ax,key,title in zip(axes,['range30','final_mean_vs_hodl'],['常态30分钟波幅中位数','常态每户末值相对同路径持有']):
  for seed in [81,82,83]:
   vals=[next(r[key] for r in rows if r['arm']==a and r['scenario']=='normal' and r['seed']==seed) for a in ['mixed_10_5_5','random_20']];ax.plot([0,1],vals,'o-',label=str(seed))
  ax.set(xticks=[0,1],xticklabels=['10随机+5库存+5短线','20随机'],title=title);ax.grid(alpha=.2)
 axes[0].legend(title='种子');fig.tight_layout();fig.savefig(HERE/'comparison.png',dpi=150);plt.close(fig)
 print('VALIDATED',len(rows),'paths',len(protected),'unchanged files',flush=True)
if __name__=='__main__':run()
