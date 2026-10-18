"""One predeclared repeated-shock path and a same-seed no-shock control."""
from pathlib import Path
import hashlib
import json

import numpy as np

from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import ROOT,clean_json
from .model import simulate

HERE=Path(__file__).parent
OUTPUT=HERE/'results'


def main():
    spec=json.loads((HERE/'config.json').read_text())
    old_manifest_path=HERE.parent/'results/exploration/manifest.json'
    old=json.loads(old_manifest_path.read_text())
    cfg={**old['config'],'ticks':spec['ticks'],'burn_in':0,'seeds':[spec['market_seed']],
         'order_sigma_foreign':spec['order_sigma_foreign'],'psi':spec['psi']}
    event_ticks=list(range(spec['event_interval'],spec['ticks']+1,spec['event_interval']))
    rng=np.random.default_rng(spec['shock_seed'])
    signs=rng.choice([-1,1],size=len(event_ticks))
    schedule={t:float(sign*spec['event_abs_return']) for t,sign in zip(event_ticks,signs)}
    sources={**old['source_sha256']}
    for p in [HERE/'config.json',HERE/'run.py',HERE/'model.py']:
        sources[str(p.relative_to(ROOT))]=hashlib.sha256(p.read_bytes()).hexdigest()
    protected={**old['protected_sha256'],**old['source_sha256'],
               str(old_manifest_path.relative_to(ROOT)):hashlib.sha256(old_manifest_path.read_bytes()).hexdigest()}
    for p,h in {**sources,**protected}.items():assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h,p
    OUTPUT.mkdir(parents=True,exist_ok=True)
    manifest=dict(spec=spec,config=cfg,profiles=old['profiles'],schedule=schedule,
                  source_sha256=sources,protected_sha256=protected)
    (OUTPUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    summaries={};cases={};checks=dict(paths=0,wallet_marks=0,event_snapshots=0)
    for label,events in [('control',{}),('shock',schedule)]:
        x,s,episodes,fills,inventory_fills=simulate(cfg,old['profiles'],spec['inventory_count'],spec['b'],spec['market_seed'],events)
        assert x['account_gold'].shape==(10001,20)
        np.testing.assert_allclose(x['account_wealth'],x['account_gold']+x['account_foreign']*x['prices'][:,None],atol=1e-8,rtol=0)
        np.testing.assert_allclose(x['gold']+x['account_gold'].sum(axis=1),1100000+np.cumsum(x['injection_gold']),atol=1e-8,rtol=0)
        np.testing.assert_allclose(x['pool_foreign']+x['account_foreign'].sum(axis=1),220000+np.cumsum(x['injection_foreign']),atol=1e-8,rtol=0)
        np.testing.assert_allclose(x['account_gold'].sum(axis=1),x['random_gold']+x['factor_gold']+x['reversion_gold'],atol=1e-8,rtol=0)
        np.testing.assert_allclose(x['account_trades'].sum(axis=1),x['trades'],atol=0,rtol=0)
        assert x['account_gold'].min()>=0 and x['account_foreign'].min()>=0
        assert all(np.isfinite(a).all() for a in x.values())
        if label=='control':
            prefix_path=HERE.parent/'results/exploration/sigma120-inventory5-seed81/normal.npz'
            with np.load(prefix_path) as z:
                assert all(np.array_equal(x[k][:len(z[k])],z[k]) for k in z.files)
            manifest['old_control_prefix_sha256']={str(prefix_path.relative_to(ROOT)):hashlib.sha256(prefix_path.read_bytes()).hexdigest()}
        else:
            first=event_ticks[0]
            assert all(np.array_equal(x[k][:first],cases['control'][k][:first]) for k in x)
            assert len(s['events'])==10 and s['events'][-1]['tick']==10000
            for event in s['events']:
                tick=event['tick'];pre=float(event['pre_price']);post=float(event['post_price'])
                assert abs(post/pre-1-schedule[tick])<1e-11
                for a in event['accounts']:
                    i=int(a['name']);foreign=float(a['foreign'])
                    assert foreign==x['account_foreign'][tick,i] and float(a['gold'])==x['account_gold'][tick,i]
                    assert abs((float(a['wealth_after'])-float(a['wealth_before']))-foreign*(post-pre))<1e-8
                    checks['event_snapshots']+=1
        for i,a in enumerate(s['accounts']):
            assert float(a['gold'])==x['account_gold'][-1,i] and float(a['foreign'])==x['account_foreign'][-1,i]
        save_trace(OUTPUT,label,x,episodes,fills)
        (OUTPUT/f'{label}-inventory.jsonl').write_text(''.join(json.dumps(f,ensure_ascii=False)+'\n' for f in inventory_fills))
        summaries[label]=s;cases[label]=x
        checks['paths']+=1;checks['wallet_marks']+=x['account_wealth'].size
        print('Complete',label,'final price',x['prices'][-1],flush=True)
    for p,h in {**sources,**protected,**manifest['old_control_prefix_sha256']}.items():
        assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h,p
    checks.update(protected_files_unchanged=len(protected),both_assets_conserved=True,
                  old_control_prefix_exact=True,first_event_prefix_exact=True,
                  exact_requested_shocks=True,terminal_event_preserved=True)
    (OUTPUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    (OUTPUT/'summary.json').write_text(json.dumps(clean_json(summaries),ensure_ascii=False,indent=2)+'\n')
    (OUTPUT/'audit.json').write_text(json.dumps(checks,indent=2)+'\n')
    print(json.dumps(checks),flush=True)


if __name__=='__main__':main()
