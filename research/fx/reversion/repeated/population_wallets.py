"""Positions and marked wallet wealth on the saved 40/80-account paths."""
import csv
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from .population import OUTPUT


def write_csv(name,rows):
    with (OUTPUT/name).open('w') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)


def main():
    paths=[OUTPUT/f'n{n}'/name for n in [40,80] for name in ['control.npz','shock.npz','summary.json']]
    hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    groups=[];accounts=[];overview=[];loaded={}
    for n in [40,80]:
        summaries=json.loads((OUTPUT/f'n{n}'/'summary.json').read_text())
        for case in ['control','shock']:
            with np.load(OUTPUT/f'n{n}'/f'{case}.npz') as z:
                x={k:z[k] for k in ['prices','account_gold','account_foreign','account_wealth','account_trades']}
            loaded[n,case]=x
            g,f,w=x['account_gold'],x['account_foreign'],x['account_wealth']
            np.testing.assert_allclose(w,g+f*x['prices'][:,None],atol=1e-8,rtol=0)
            benchmark=5000+1000*x['prices'][-1]
            drawdown=1-w/np.maximum.accumulate(w,axis=0)
            s=summaries[case]
            overview.append(dict(accounts=n,case=case,final_price=float(x['prices'][-1]),
                final_total_wealth=float(w[-1].sum()),mean_wealth=float(w[-1].mean()),
                mean_wealth_change=float(w[-1].mean()/10000-1),initial_wallet_hold_value=float(benchmark),
                min_final_wealth=float(w[-1].min()),max_final_wealth=float(w[-1].max()),
                max_individual_drawdown=float(drawdown.max()),short_realized_pnl=s['behavior']['realized_pnl']))
            pending={int(e['account']):e for e in s['unfinished_episodes']}
            for a,b,role in [(0,n//2,'随机'),(n//2,3*n//4,'库存调节'),(3*n//4,n,'短线')]:
                groups.append(dict(accounts=n,case=case,role=role,count=b-a,
                    mean_foreign=float(f[-1,a:b].mean()),net_foreign_vs_initial=float(f[-1,a:b].mean()-1000),
                    min_foreign=float(f[-1,a:b].min()),max_foreign=float(f[-1,a:b].max()),
                    mean_gold=float(g[-1,a:b].mean()),mean_wealth=float(w[-1,a:b].mean()),
                    mean_wealth_change=float(w[-1,a:b].mean()/10000-1),
                    vs_initial_wallet_hold=float(w[-1,a:b].mean()-benchmark),
                    min_wealth=float(w[-1,a:b].min()),max_wealth=float(w[-1,a:b].max()),
                    empty_asset_at_end=int(np.any((g[-1,a:b]<=1e-8,f[-1,a:b]<=1e-8),axis=0).sum())))
                for i in range(a,b):
                    e=pending.get(i)
                    accounts.append(dict(accounts=n,case=case,account=i,role=role,
                        gold=float(g[-1,i]),foreign=float(f[-1,i]),net_foreign_vs_initial=float(f[-1,i]-1000),
                        marked_wealth=float(w[-1,i]),wealth_change=float(w[-1,i]/10000-1),
                        vs_initial_wallet_hold=float(w[-1,i]-benchmark),min_wealth=float(w[:,i].min()),
                        max_drawdown=float(drawdown[:,i].max()),total_trades=int(x['account_trades'][:,i].sum()),
                        pending_direction=e['direction'] if e else None,pending_age_minutes=e['age']/6 if e else None))
    write_csv('wallet_overview.csv',overview);write_csv('wallet_groups.csv',groups);write_csv('wallet_accounts.csv',accounts)
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False})
    fig,axes=plt.subplots(2,2,figsize=(15,9),sharex=True)
    colors=['#737e89','#146c9d','#b26b25']
    for col,n in enumerate([40,80]):
        for case in ['control','shock']:
            x=loaded[n,case];hours=np.arange(len(x['prices']))/360
            for (a,b,label),color in zip([(0,n//2,'随机'),(n//2,3*n//4,'库存调节'),(3*n//4,n,'短线')],colors):
                kw=dict(color=color,lw=1.1 if case=='shock' else .8,alpha=1 if case=='shock' else .5,
                        ls='-' if case=='shock' else '--',label=label+(' 连续冲击' if case=='shock' else ' 无冲击'))
                axes[0,col].plot(hours,x['account_foreign'][:,a:b].mean(axis=1)-1000,**kw)
                axes[1,col].plot(hours,(x['account_wealth'][:,a:b].mean(axis=1)/10000-1)*100,**kw)
            if case=='shock':
                axes[1,col].plot(hours,((5000+1000*x['prices'])/10000-1)*100,color='black',ls=':',lw=1,label='同路径持有初始钱包')
        axes[0,col].set_title(f'{n}人：各组每户平均持仓');axes[1,col].set_title(f'{n}人：各组每户平均净值变化')
        axes[0,col].set_ylabel('外币持仓相对初始1000');axes[1,col].set_ylabel('净值变化（%）');axes[1,col].set_xlabel('小时')
        for ax in axes[:,col]:
            ax.axhline(0,color='black',alpha=.4,lw=.5);ax.grid(alpha=.2);ax.legend(fontsize=8,ncol=2)
    fig.suptitle('40/80人持仓与净值：实线连续冲击、虚线无冲击；终点含最后一次−5%跳价')
    fig.tight_layout();fig.savefig(OUTPUT/'wallets.png',dpi=150);plt.close(fig)
    lines=['# 40/80人的持仓与净值','','复用已保存路径，不改变设置、不重跑。各账户初始5000金圆券+1000外币，初始价格5，初始市值10000。净值=现金+外币×当时池边际价，含浮动盈亏；不是全量清仓后的滑点所得。','','## 整体终态','','|人数|场景|期末价格|每户平均净值|净值变化|持有初始钱包的期末价值|个人期末净值范围|','|---|---|---:|---:|---:|---:|---:|']
    for row in overview:
        lines.append(f"|{row['accounts']}|{'连续冲击' if row['case']=='shock' else '无冲击'}|{row['final_price']:.4f}|{row['mean_wealth']:.2f}|{row['mean_wealth_change']:+.2%}|{row['initial_wallet_hold_value']:.2f}|{row['min_final_wealth']:.2f}～{row['max_final_wealth']:.2f}|")
    lines+=['','## 连续冲击下的三组终态','','|人数|类型|人数|每户平均现金|每户平均外币|每户平均净值|净值变化|相对初始钱包持有基准差额|','|---|---|---:|---:|---:|---:|---:|---:|']
    for row in groups:
        if row['case']=='shock':
            lines.append(f"|{row['accounts']}|{row['role']}|{row['count']}|{row['mean_gold']:.2f}|{row['mean_foreign']:.2f}|{row['mean_wealth']:.2f}|{row['mean_wealth_change']:+.2%}|{row['vs_initial_wallet_hold']:+.2f}|")
    lines+=['','基准H=5000+1000×同一路径期末价，差额W−H用来区分初始外币重估与持仓调整结果，仍含未实现盈亏；不是可复制超额收益或已实现利润。组均值会掩盖随机账户的大幅分化，逐人结果在wallet_accounts.csv。','','![持仓与净值](wallets.png)','','## 状态解释','','随机组容易走到接近单币持仓，连续冲击终点40人中的20个随机户有4户、80人中的40个随机户有5户某一资产为零；库存和短线户期末两币均有余额。空掉一种资产不等于净值为零，继续按可用余额裁剪。','','库存组在本路径上比持有初始钱包的基准多保留每户约350/504金圆券；短线组比基准少约198/174金圆券。短线已实现轮次盈亏分别为−1981.889804/−3359.329734金圆券，这与整个钱包的市值变化是不同口径。常态下库存组净值变化分别+0.97%/+3.22%，短线组−2.26%/−2.47%；这些是本种子的描述，不能推出一般策略排名。','','短线只交易相对初始钱包的一批200外币，未加仓叠仓，终态外币只在800、1000、1200三档。40人中的10个短线账户有4个买入待卖、4个卖出待回购、2个空闲；80人中的20个有8个买入待卖、5个卖出待回购、7个空闲。卖出待回购使用自有外币，不借贷做空。','','期末包括第10000tick的最后一次−5%事件，之后没有机器人反应或强制平仓，因此终态不是最后冲击完全回吐后的状态。前面观察到回归很强，并不意味着终点汇率必然回到初始5。两个场景的事件前历史和期末价格不同，不将净值差简单当作人数因果效果。','','[每户终态、持仓与最大回撤](wallet_accounts.csv)、[三组汇总](wallet_groups.csv)、[整体汇总](wallet_overview.csv)。逐tick个人完整矩阵保存在n40/n80的control.npz与shock.npz；来源与分析脚本哈希见wallet_sources.json。','']
    (OUTPUT/'wallet_report.md').write_text('\n'.join(lines))
    for path,digest in hashes.items():assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest
    hashes[__file__]=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    (OUTPUT/'wallet_sources.json').write_text(json.dumps(hashes,indent=2)+'\n')
    print('Saved 240 individual wallet summaries, 12 group rows, four scenario summaries and chart; input hashes unchanged.')


if __name__=='__main__':main()
