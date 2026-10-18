"""One prespecified, cold-start 1000-tick illustration of the frozen v1."""
import csv
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from research.fx.activity.model import load_config
from research.fx.activity.run import simulate, source_hashes


def main():
    here=Path(__file__).resolve().parents[1]
    output=Path(__file__).parent/'seed106-1000ticks'
    output.mkdir(parents=True,exist_ok=True)
    cfg={**load_config(),'ticks':1000,'burn_in':0}
    b,seed=.25,106
    hashes=source_hashes()
    frozen=json.loads((here/'baseline-v1.json').read_text())['sha256']
    assert all(frozen[name]==digest for name,digest in hashes.items())
    x,summary=simulate(cfg,'amm',b,seed)
    assert len(x['prices'])==1001 and summary['conserved'] and summary['nonnegative_wallets']
    np.testing.assert_allclose(x['returns'][1:],np.log(x['prices'][1:]/x['prices'][:-1]),atol=1e-15)
    np.savez_compressed(output/'series.npz',**x)
    with (output/'series.csv').open('w',newline='') as file:
        writer=csv.writer(file)
        writer.writerow(['tick','price_gold_per_foreign','return','pre_intensity','arrivals','trades'])
        for tick in range(1,1001):
            writer.writerow([tick,*[x[k][tick] for k in ['prices','returns','intensity','arrivals','trades']]])
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':11})
    fig,axes=plt.subplots(3,1,figsize=(12,8),sharex=True,
                          gridspec_kw={'height_ratios':[2.5,1,1.1]},constrained_layout=True)
    ticks=np.arange(1001)
    axes[0].plot(ticks,x['prices'],color='#167a73',lw=1.5)
    axes[0].axhline(x['prices'][0],color='#999999',lw=.8,ls='--',label='初始价格 5')
    axes[0].set(ylabel='价格（金币 / 外币）',title='活动反馈 v1：1000 tick 真实 AMM 模拟')
    axes[0].ticklabel_format(axis='y',style='plain',useOffset=False)
    axes[0].legend(loc='best',fontsize=9)
    returns_pct=x['returns'][1:]*100
    axes[1].vlines(ticks[1:],0,returns_pct,color=np.where(returns_pct>=0,'#167a73','#bd6642'),lw=.7)
    axes[1].axhline(0,color='#777777',lw=.6)
    axes[1].set(ylabel='单 tick 对数收益（%）')
    axes[2].plot(ticks[1:],x['intensity'][1:],color='#8054a1',lw=1.2,label='期初预定到达强度')
    axes[2].set(xlabel='tick（从初始状态开始，无预热）',ylabel='预期到达次数 / tick',xlim=(0,1000))
    axes[2].legend(loc='best',fontsize=9)
    for ax in axes:
        ax.grid(alpha=.18)
        ax.spines[['top','right']].set_visible(False)
    fig.suptitle('种子 106  |  b=0.25，τ=1000  |  20 个有限钱包账户，零手续费',fontsize=12)
    fig.savefig(output/'trajectory.png',dpi=160)
    plt.close(fig)
    metadata=dict(config=cfg,summary=summary,start_price=float(x['prices'][0]),
        end_price=float(x['prices'][-1]),minimum_price=float(x['prices'].min()),
        maximum_price=float(x['prices'].max()),price_change_pct=float((x['prices'][-1]/x['prices'][0]-1)*100),
        note='Prespecified seed106; exactly1000 ticks, cold start z=0, no burn-in or path selection. Illustration only; not a statistical validation.',
        source_sha256=hashes,plot_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    (output/'metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in metadata.items() if k not in ('summary','source_sha256','config')},ensure_ascii=False))
    print(f"Arrivals={summary['arrivals']}; trades={summary['trades']}; conserved={summary['conserved']}")
    print(output/'trajectory.png')


if __name__=='__main__':
    main()
