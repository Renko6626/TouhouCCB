"""Recompute diagnostics from saved arrays; never rerun a valid simulation."""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path

import numpy as np

from .analysis import analyze, compact
from .experiments import Config, run_one, source_hashes


def update(directory, bootstrap, shuffles, repair, hashes):
    directory=Path(directory)
    result=json.loads((directory/'result.json').read_text())
    if result['status']=='failed' and repair:
        return run_one(Config(**result['metadata']['config']),str(directory.parent),bootstrap,shuffles,hashes)
    if result['status']!='complete':
        return result
    cfg=result['metadata']['config']
    with np.load(directory/'series.npz') as arrays:
        diagnostics=analyze(arrays['prices'],arrays['gold'],burn_in=cfg['burn_in'],seed=cfg['seed'],
                            bootstrap=bootstrap,shuffle_repetitions=shuffles)
    (directory/'diagnostics.json').write_text(json.dumps(diagnostics,indent=2))
    result['metrics']=compact(diagnostics)
    result['analysis_metadata']=dict(source_hashes=hashes,bootstrap=bootstrap,shuffle_repetitions=shuffles,
        series_hash=hashlib.sha256((directory/'series.npz').read_bytes()).hexdigest())
    (directory/'result.json').write_text(json.dumps(result,indent=2))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--bootstrap',type=int,default=0)
    parser.add_argument('--shuffles',type=int,default=1)
    parser.add_argument('--repair-failed',action='store_true')
    args=parser.parse_args()
    directories=sorted(p.parent for p in args.input.glob('*/result.json'))
    if not directories:
        parser.error('no result directories')
    results=[]; hashes=source_hashes()
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        jobs=[executor.submit(update,str(p),args.bootstrap,args.shuffles,args.repair_failed,hashes) for p in directories]
        for future in as_completed(jobs):
            result=future.result();results.append(result)
            print(f"{len(results)}/{len(directories)} {result['status']} {result['id']}",flush=True)
    (args.input/'summary.json').write_text(json.dumps(results,indent=2))
    if any(r['status']=='failed' for r in results):
        raise SystemExit('Unexpected failures remain')


if __name__=='__main__':
    main()
