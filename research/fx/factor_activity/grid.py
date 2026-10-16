"""Predeclared factorial search and held-out validation of unchanged models."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
from itertools import product
import json
from pathlib import Path
import time
import traceback

import numpy as np

from research.fx.common.analysis import return_diagnostics, state_diagnostics
from research.fx.common.diagnostics import acf, describe
from research.fx.common.series import block_mean, correlation
from .run import HERE, ROOT, clean_json, hashes, load_config, simulate

OUTPUT=HERE/'results/grid'
VOL_KEYS=('abs_101_300','abs_301_1000','sq_101_300','sq_301_1000')


def load_spec():
    return json.loads((HERE/'grid.json').read_text())


def parameter_grid(base,spec):
    cells=[]
    axes=[spec[k] for k in ('trend_multipliers','reversion_multipliers','noise_sigmas',
                            'window_pairs','adjustments')]
    for index,(trend,mean,noise,windows,adjustment) in enumerate(product(*axes)):
        cfg={**base,'factor_mix':[[round(a*trend,12),round(b*mean,12)] for a,b in base['factor_mix']],
             'noise_sigma':noise,'trend_window':windows[0],'mean_window':windows[1],
             'adjustment':adjustment}
        params=dict(trend_multiplier=trend,reversion_multiplier=mean,noise_sigma=noise,
                    trend_window=windows[0],mean_window=windows[1],adjustment=adjustment)
        cells.append(dict(id=f'g{index:03d}',parameters=params,config=cfg))
    return cells


def select_candidates(rows,baseline_id,per_ranking=2):
    valid=[r for r in rows if r['status']=='complete']
    criteria=[('volatility',lambda r:r['volatility_min']),
              ('paired_gain',lambda r:r['feedback_gain_min']),
              ('volatility_per_direction',lambda r:r['volatility_min']/(r['max_signed']+1e-6))]
    picked={}
    for reason,score in criteria:
        for row in sorted(valid,key=lambda r:(-score(r),r['id']))[:per_ranking]:
            picked.setdefault(row['id'],[]).append(reason)
    picked.setdefault(baseline_id,[]).append('original_parameters')
    return [dict(id=key,reasons=reasons) for key,reasons in picked.items()]


def experiment_hashes():
    sources=hashes()
    for path in [HERE/'grid.py',HERE/'grid.json']:
        sources[str(path.relative_to(ROOT))]=hashlib.sha256(path.read_bytes()).hexdigest()
    return sources


def compact_metrics(x,summary,cfg):
    sample=slice(cfg['burn_in']+1,None)
    r=x['returns'][sample];lam=x['intensity'][sample]
    curves={name:acf(values,1000) for name,values in
            [('signed',r),('abs',abs(r)),('sq',r*r)]}
    metrics=describe(r)
    metrics.update(mean_intensity=float(lam.mean()),peak_intensity=summary['peak_intensity'],
                   max_signed=float(np.max(abs(curves['signed'][1:1001]))))
    for name,curve in curves.items():
        for lo,hi in [(1,1),(101,300),(301,1000)]:
            metrics[f'{name}_{lo}_{hi}']=float(curve[lo:hi+1].mean())
    for width in [100,240,1000]:
        a,b=block_mean(lam,width),block_mean(r*r,width)
        metrics[f'next_{width}']=correlation(a[:-1],b[1:])
    for field in ['arrivals','trades','raw_abs_demand','executed_abs_quantity',
                  'clipped_orders','skipped_orders']:
        metrics[field]=float(x[field][sample].sum())
    metrics['gross_quantity_per_tick']=metrics['executed_abs_quantity']/len(r)
    metrics['clipped_fraction']=metrics['clipped_orders']/metrics['arrivals'] if metrics['arrivals'] else None
    metrics['wallet_gold_min']=float(x['wallet_gold_min'][sample].min())
    metrics['wallet_foreign_min']=float(x['wallet_foreign_min'][sample].min())
    return metrics


def task_config(cell,stage,spec):
    return {**cell['config'],**{key:spec[stage][key] for key in ['ticks','burn_in','seeds']}}


def run_task(task):
    stage,cell,case,seed,cfg,sources=task
    directory=OUTPUT/stage/f"{cell['id']}-{case}-seed{seed}"
    directory.mkdir(parents=True,exist_ok=True)
    record=dict(stage=stage,id=cell['id'],case=case,seed=seed,parameters=cell['parameters'],
                config=cfg,source_sha256=sources)
    start=time.perf_counter()
    try:
        x,summary=simulate(cfg,case,seed)
        metrics=compact_metrics(x,summary,cfg)
        record.update(status='complete',summary=summary,metrics=metrics)
        if stage=='validation':
            r=x['returns'][cfg['burn_in']+1:]
            record['diagnostics']=return_diagnostics(r)
            record['diagnostics']['activity']=state_diagnostics(x['intensity'][cfg['burn_in']+1:],r)
        np.savez_compressed(directory/'series.npz',**x)
    except Exception as exc:
        record.update(status='failed',error=f'{type(exc).__name__}: {exc}',traceback=traceback.format_exc())
    record['seconds']=time.perf_counter()-start
    record=clean_json(record)
    (directory/'result.json').write_text(json.dumps(record,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    return record


def aggregate(records,cells,seeds):
    rows=[]
    for cell in cells:
        group=[r for r in records if r['id']==cell['id']]
        by_key={(r['case'],r['seed']):r for r in group}
        row=dict(id=cell['id'],parameters=cell['parameters'],status='incomplete')
        complete=all(by_key.get((case,seed),{}).get('status')=='complete'
                     for case in ['factor_fixed','factor_activity'] for seed in seeds)
        if complete:
            for case,tag in [('factor_fixed','off'),('factor_activity','on')]:
                values=[by_key[case,seed]['metrics'] for seed in seeds]
                row[tag]={k:float(np.mean([v[k] for v in values]))
                          if all(v.get(k) is not None for v in values) else None for k in values[0]}
            row.update(status='complete',volatility_min=min(row['on'][k] for k in VOL_KEYS),
                       feedback_gain_min=min(row['on'][k]-row['off'][k] for k in VOL_KEYS),
                       max_signed=row['on']['max_signed'],
                       all_four_positive=sum(all(by_key['factor_activity',seed]['metrics'][k]>0
                                                 for k in VOL_KEYS) for seed in seeds),
                       all_four_gain_positive=sum(all(by_key['factor_activity',seed]['metrics'][k]>
                                                      by_key['factor_fixed',seed]['metrics'][k]
                                                      for k in VOL_KEYS) for seed in seeds))
        rows.append(row)
    return rows


def execute(stage,cells,spec,sources):
    tasks=[];records=[]
    for cell in cells:
        cases=['activity_v1'] if cell['id']=='activity_v1' else ['factor_fixed','factor_activity']
        cfg=task_config(cell,stage,spec)
        for case,seed in product(cases,spec[stage]['seeds']):
            path=OUTPUT/stage/f"{cell['id']}-{case}-seed{seed}"/'result.json'
            if path.exists():
                record=json.loads(path.read_text())
                if record['config']!=cfg or record['source_sha256']!=sources:
                    raise ValueError(f'existing result differs from current configuration/source: {path}')
                records.append(record)
            else:
                tasks.append((stage,cell,case,seed,cfg,sources))
    print(json.dumps(dict(stage=stage,cached=len(records),pending=len(tasks))),flush=True)
    with ProcessPoolExecutor(max_workers=spec['workers']) as executor:
        futures=[executor.submit(run_task,t) for t in tasks]
        for i,future in enumerate(as_completed(futures),1):
            r=future.result();records.append(r)
            if i%12==0 or r['status']!='complete' or i==len(tasks):
                print(json.dumps(dict(stage=stage,done=i,total=len(tasks),id=r['id'],
                                      status=r['status'],seconds=round(r['seconds'],2))),flush=True)
    records.sort(key=lambda r:(r['id'],r['case'],r['seed']))
    (OUTPUT/f'{stage}-summary.json').write_text(json.dumps(clean_json(records),ensure_ascii=False,indent=2)+'\n')
    return records


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--stage',choices=['screen','validation','all'],default='all')
    args=parser.parse_args();spec=load_spec();base=load_config();cells=parameter_grid(base,spec)
    OUTPUT.mkdir(parents=True,exist_ok=True)
    sources=experiment_hashes()
    frozen=json.loads((HERE.parent/'activity/baseline-v1.json').read_text())['sha256']
    assert all(hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest for path,digest in frozen.items())
    manifest=dict(spec=spec,cells=cells,source_sha256=sources,
                  frozen_baseline_verified_files=len(frozen),
                  baseline_id=next(c['id'] for c in cells if c['config']==base))
    manifest_path=OUTPUT/'manifest.json'
    if manifest_path.exists():
        assert json.loads(manifest_path.read_text())==manifest,'existing grid manifest changed'
    else:
        manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    if args.stage in ['screen','all']:
        screen=execute('screen',cells,spec,sources)
        rows=aggregate(screen,cells,spec['screen']['seeds'])
        (OUTPUT/'screen-aggregates.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
        selected=select_candidates(rows,manifest['baseline_id'],spec['selection_per_ranking'])
        selection=dict(screen_summary_sha256=hashlib.sha256((OUTPUT/'screen-summary.json').read_bytes()).hexdigest(),
                       candidates=selected)
        selection_path=OUTPUT/'selection.json'
        if selection_path.exists():assert json.loads(selection_path.read_text())==selection,'candidate selection changed'
        else:selection_path.write_text(json.dumps(selection,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(selection),flush=True)
    if args.stage in ['validation','all']:
        selection=json.loads((OUTPUT/'selection.json').read_text())
        assert selection['screen_summary_sha256']==hashlib.sha256((OUTPUT/'screen-summary.json').read_bytes()).hexdigest()
        ids=[r['id'] for r in selection['candidates']]
        candidates=[c for c in cells if c['id'] in ids]
        reference=dict(id='activity_v1',parameters={},config=base)
        records=execute('validation',candidates+[reference],spec,sources)
        rows=aggregate(records,candidates,spec['validation']['seeds'])
        (OUTPUT/'validation-aggregates.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
    print('Finished requested grid stages.',flush=True)


if __name__=='__main__':main()
