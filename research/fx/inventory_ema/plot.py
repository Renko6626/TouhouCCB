"""Render fixed-window paths and all-seed metrics from saved experiment outputs."""
import json
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from .run import OUT, MODES, SEEDS

LABELS = ['原版：600分钟滚动均值', 'EMA半衰期5分钟', 'EMA半衰期10分钟', 'EMA半衰期30分钟']
COLORS = ['#566370', '#168178', '#b86d24', '#7158a5']


def main():
    plt.rcParams.update({'font.family':'Noto Sans CJK SC', 'axes.unicode_minus':False, 'font.size':10})
    data=json.loads((OUT/'metrics.json').read_text())
    seed=SEEDS[0]  # Chosen before running, never selected for appearance.
    fig, axes=plt.subplots(4,1,figsize=(13,10),sharex=True,sharey=True,layout='constrained')
    for ax,(mode,_),label,color in zip(axes,MODES,LABELS,COLORS):
        with np.load(OUT/mode/f'seed{seed}'/'path.npz') as z:
            t=np.arange(4321)/720
            ref=z['slow_reference'][:4321].copy();ref[0]=5
            ax.plot(t,z['prices'][:4321],color=color,lw=.9,label='池价')
            ax.plot(t,ref,color='#30363b',ls='--',lw=.8,label='库存参考价')
        ax.set(title=label,ylabel='金币／外币',xlim=(0,6));ax.grid(alpha=.18)
        ax.legend(loc='upper right',ncol=2,fontsize=8)
    axes[-1].set_xlabel('模拟小时（固定展示前6小时，各图同一纵轴）')
    fig.suptitle(f'库存参考价对比｜预热版，其余参数不变｜预先固定种子 {seed}\n完整实验为4个种子 × 24小时；无玩家、无冲击、零手续费',fontsize=12)
    fig.savefig(OUT/'price-comparison.png',dpi=145);plt.close(fig)
    specs=[('cycle_minutes',1,'完整往返时长（分钟，1.5%反转）'),('up_gain',100,'上涨幅度（%）'),('fills_per_minute',1,'成交笔数／分钟'),
           ('inventory_volume_share',100,'库存账户成交外币量占比（%）'),('gap_rms',100,'库存参考价偏离RMS（对数%，越小越贴近）'),('return10m_std',100,'10分钟收益率标准差（%）')]
    fig, axes=plt.subplots(2,3,figsize=(14,8),layout='constrained')
    for ax,(key,factor,label) in zip(axes.flat,specs):
        matrix=np.array([[data[mode][str(seed)][key]*factor for mode,_ in MODES] for seed in SEEDS])
        for values in matrix: ax.plot(range(4),values,color='#a7adb5',lw=.8,alpha=.65)
        for j,color in enumerate(COLORS): ax.scatter(np.repeat(j,4),matrix[:,j],color=color,s=22,alpha=.8)
        ax.plot(range(4),matrix.mean(axis=0),color='#172b3a',marker='D',ms=5,lw=1.8,label='四种子等权均值')
        ax.set(title=label,xticks=range(4),xticklabels=['原版','5分','10分','30分']);ax.grid(axis='y',alpha=.15);ax.set_ylim(bottom=0)
    axes.flat[0].legend(fontsize=8)
    fig.suptitle('四种子配对比较｜剔除开始30分钟｜灰线连接相同种子；点表示各种子结果\n波段时长和涨幅先取每种子的中位数，再等权平均；灰线不是置信区间',fontsize=12)
    fig.savefig(OUT/'metrics-comparison.png',dpi=145);plt.close(fig)
    fig, axes=plt.subplots(4,1,figsize=(13,10),sharex=True,sharey=True,layout='constrained')
    for ax,seed in zip(axes,SEEDS):
        for (mode,_),label,color in zip(MODES,LABELS,COLORS):
            with np.load(OUT/mode/f'seed{seed}'/'path.npz') as z:
                price=z['prices'];ax.plot(np.arange(len(price))[::12]/720,(price[::12]/5-1)*100,color=color,lw=.7,label=label)
        ax.set(title=f'种子 {seed}',ylabel='相对初价变化（%）',xlim=(0,24));ax.grid(alpha=.15)
    axes[0].legend(ncol=4,fontsize=8);axes[-1].set_xlabel('模拟小时（按每分钟收盘抽样展示）')
    fig.suptitle('完整24小时价格路径｜各图同一纵轴｜初价5金币／外币',fontsize=12)
    fig.savefig(OUT/'full-paths.png',dpi=140);plt.close(fig)


if __name__=='__main__':main()
