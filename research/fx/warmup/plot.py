"""Plot saved warmup paths; no new simulation or production writes."""
import argparse
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def main():
    root=Path(__file__).resolve().parents[3]
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',type=Path,default=root/'output/fx-warmup-24h-final-20261016')
    args=p.parse_args()
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
    paths=sorted(args.data.glob('seed*/trace.npz'))
    if not paths:p.error('No saved seed*/trace.npz paths in --data')
    fig,axes=plt.subplots(len(paths),1,figsize=(14,2.75*len(paths)),sharex=True,layout='constrained',squeeze=False)
    for ax,path in zip(axes[:,0],paths):
        with np.load(path) as z:
            prices=z['prices'];ref=z['slow_reference'].copy()
        ref[0]=np.nan;t=np.arange(len(prices))/720
        ax.plot(t,prices,color='#176997',lw=.8,label='池价');ax.plot(t,ref,color='#555555',lw=.8,ls='--',label='库存参考价')
        ax.set(title=path.parent.name,ylabel='金币／外币',xlim=(0,t[-1]));ax.grid(alpha=.18);ax.legend(fontsize=8,ncol=2)
    axes[-1,0].set_xlabel('模拟小时')
    fig.suptitle('预热预设：完整模拟路径；每行独立价格坐标；无玩家、无冲击、零费率')
    fig.savefig(args.data/'full-24h.png',dpi=145);plt.close(fig)


if __name__=='__main__':main()
