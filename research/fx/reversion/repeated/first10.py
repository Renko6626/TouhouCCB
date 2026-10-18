"""Read-only pre/post ten-tick activity summaries for saved event windows."""
import csv
import hashlib
import json
import math

import numpy as np

from .run import OUTPUT


def main():
    paths = [OUTPUT/name for name in ['shock.npz', 'summary.json', 'manifest.json']]
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    summary = json.loads(paths[1].read_text())
    manifest = json.loads(paths[2].read_text())
    config = manifest['config']
    b = manifest['spec']['b']
    rows = []
    with np.load(paths[0]) as x:
        for event in summary['shock']['events']:
            t = event['tick']
            if t+10 >= len(x['prices']):
                continue
            before, after = slice(t-9,t+1), slice(t+1,t+11)
            account_fills = x['account_trades'][after].sum(axis=0)
            fills = int(x['trades'][after].sum())
            group_fills = {g: int(x[g+'_trades'][after].sum()) for g in ['random','reversion','factor']}
            assert int(account_fills.sum()) == sum(group_fills.values()) == fills
            rows.append(dict(tick=t, jump=event['return_requested'],
                before10_mean_intensity=float(x['intensity'][before].mean()),
                after10_mean_intensity=float(x['intensity'][after].mean()),
                before10_arrivals=int(x['arrivals'][before].sum()),
                after10_arrivals=int(x['arrivals'][after].sum()),
                before10_trades=int(x['trades'][before].sum()), after10_trades=fills,
                distinct_traders=int(np.count_nonzero(account_fills)),
                active_ticks=int(np.count_nonzero(x['trades'][after])),
                **{g+'_trades':count for g,count in group_fills.items()}))
    a = math.exp(-1/config['tau'])
    scale = math.sqrt(1-a*a)/config['psi']
    neutral = {str(jump): (1-b)*config['mean_arrivals']+
               b*(scale*math.log1p(jump))**2 for jump in [.05,-.05]}
    result = dict(events=rows, means={k:float(np.mean([r[k] for r in rows])) for k in rows[0] if k not in ['tick','jump']},
        standalone_jump_intensity_from_zero_z=neutral, sources_sha256=hashes,
        definition='Before: ticks t-9..t (ordinary trades precede issuance); after: t+1..t+10 inclusive. Ten ticks=100 seconds. Last event has no follow-up and is excluded. Arrivals can repeat a person; trades count fills, distinct traders count accounts. Pre/post descriptive comparison on one dependent path, no b=0 causal ablation.')
    with (OUTPUT/'first10.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    (OUTPUT/'first10.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    for p in paths:
        assert hashlib.sha256(p.read_bytes()).hexdigest() == hashes[p.name]
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
