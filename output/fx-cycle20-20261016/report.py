"""Selected candidate, one new-seed check, and chart against frozen baseline."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import hashlib
import json
import runpy
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from research.fx.episodes.run import save_trace

OUT=Path(__file__).resolve().parent
H=runpy.run_path(str(OUT/'run.py'))
NAME='memory2_noise72_exit2_rate07'
VALIDATION_SEED=20261101


def main():
    frozen=json.loads(H['SNAPSHOT'].read_text());original_hash=hashlib.sha256(H['SNAPSHOT'].read_bytes()).hexdigest()
    cfg,multiplier=H['setup'](frozen,H['CANDIDATES'][NAME])
    selected=dict(name='mixed20-cycle20-candidate',status='experimental_not_frozen_not_applied_to_production',
        source_frozen_name=frozen['name'],source_frozen_sha256=original_hash,
        parameters=cfg,profiles=frozen['profiles'],activity_b=frozen['activity_b'],
        trend_multiplier=multiplier,tick_seconds=5,policy=frozen['policy'],
        differences={k:{'before':v,'after':cfg[k]} for k,v in frozen['parameters'].items() if cfg[k]!=v},
        selection='Prefer72 over66 foreign random sigma: three exploratory cycle medians22.6-23.8min, versus16.8-31.5min for66; neither was a prespecified independent comparison.',
        target='Around20min full cycles, around10min up legs, preserve about5.2 actual trades/min.',
        evidence='Three7h exploratory seeds; one subsequent7h validation seed; no players/shocks/fees.')
    H['write'](OUT/'candidate.json',selected)
    folder=OUT/'validation'/f'seed{VALIDATION_SEED}';folder.mkdir(parents=True,exist_ok=True)
    print('Validating selected candidate on additional seed',VALIDATION_SEED,flush=True)
    params={**cfg,'seeds':[VALIDATION_SEED]}
    x,s,e,f,_,_=H['simulate'](params,frozen['profiles'],3,frozen['activity_b'],VALIDATION_SEED,{},
        frozen['policy'],{'trend_multiplier':multiplier,'episode_start':13})
    H['check'](x,cfg)
    validation=H['summarize'](x,e,7)
    save_trace(folder,'trace',x,e,f);H['write'](folder/'summary.json',s)
    H['write'](folder/'metrics.json',validation)
    H['write'](OUT/'validation.json',dict(seed=VALIDATION_SEED,selected_before_validation=True,
        metrics=validation,compact=H['compact']([validation])))
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
    fig,axes=plt.subplots(3,2,figsize=(14,10),sharex=True,layout='constrained')
    for row,seed in enumerate(H['SEEDS']):
        with np.load(ROOT/f'output/fx-intraday-frozen-20261016/seed{seed}/trace.npz') as z:baseline={k:z[k] for k in z.files}
        with np.load(OUT/NAME/f'seed{seed}/trace.npz') as z:candidate={k:z[k] for k in z.files}
        paths=[baseline,candidate];start,end=360,1361
        low=min(v['prices'][start:end].min() for v in paths);high=max(v['prices'][start:end].max() for v in paths);pad=(high-low)*.08
        for col,(trace,label,color) in enumerate(zip(paths,['冻结版：往返约9分钟','新候选：往返约23分钟'],['#176997','#b27a16'])):
            ax=axes[row,col];t=np.arange(start,end)
            ax.plot(t,trace['prices'][start:end],lw=.9,color=color,label='池价')
            ax.plot(t,trace['slow_reference'][start:end],color='#555555',ls='--',lw=.8,label='库存参考价')
            ax.set(title=f'{label}\nseed {seed}',xlabel='tick（5秒）',ylabel='金币／外币',xlim=(start,end-1),ylim=(low-pad,high+pad))
            ax.grid(alpha=.18);ax.legend(fontsize=8,ncol=2)
    fig.suptitle('成交保持约5笔／分钟，延长实际波段；热身后1000tick（83分20秒）；无玩家、无冲击、零费率',fontsize=12)
    fig.savefig(OUT/'comparison-1000ticks.png',dpi=145);plt.close(fig)
    assert hashlib.sha256(H['SNAPSHOT'].read_bytes()).hexdigest()==original_hash
    H['write'](OUT/'selected-audit.json',dict(selected_candidate=NAME,additional_validation_paths=1,
        finite_series=True,assets_conserved=True,wallets_nonnegative=True,
        wealth_and_trade_counts_verified=True,original_frozen_config_unchanged=True))
    print(json.dumps(H['compact']([validation]),ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
