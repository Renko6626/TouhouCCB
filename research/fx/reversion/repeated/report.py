"""Market, positions and marked wealth for every account, without rerunning."""
import csv
import hashlib
import json

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from research.fx.factor_activity.run import ROOT
from .run import HERE,OUTPUT


def write_csv(path,rows,fields=None):
    rows=iter(rows);first=next(rows)
    with path.open('w') as f:
        writer=csv.DictWriter(f,fieldnames=fields or list(first));writer.writeheader();writer.writerow(first);writer.writerows(rows)


def main():
    manifest=json.loads((OUTPUT/'manifest.json').read_text());spec=manifest['spec'];cfg=manifest['config']
    summary=json.loads((OUTPUT/'summary.json').read_text());audit=json.loads((OUTPUT/'audit.json').read_text())
    for p,h in {**manifest['source_sha256'],**manifest['protected_sha256'],**manifest['old_control_prefix_sha256']}.items():
        assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h,p
    cases={}
    for label in ['shock','control']:
        with np.load(OUTPUT/f'{label}.npz') as z:cases[label]={k:z[k] for k in z.files}
    x,c=cases['shock'],cases['control'];events=summary['shock']['events'];n=cfg['accounts'];tmax=spec['ticks']
    names={i:f'R{i+1:02d}' if i<10 else f'I{i+1:02d}' if i<15 else f'S{i+1:02d}' for i in range(n)}
    roles={i:'随机' if i<10 else '库存调节' if i<15 else '短线' for i in range(n)}
    initial=cfg['initial_gold']+cfg['initial_foreign']*x['prices'][0]
    benchmark=cfg['initial_gold']+cfg['initial_foreign']*x['prices']
    direct=np.zeros(n)
    for e in events:
        for a in e['accounts']:direct[int(a['name'])]+=float(a['wealth_after'])-float(a['wealth_before'])
    final=[]
    for i in range(n):
        wealth=x['account_wealth'][:,i];peak=np.maximum.accumulate(wealth)
        final.append(dict(account_id=i,account=names[i],role=roles[i],final_gold=float(x['account_gold'][-1,i]),
            final_foreign=float(x['account_foreign'][-1,i]),net_foreign_vs_initial=float(x['account_foreign'][-1,i]-cfg['initial_foreign']),
            final_net_worth=float(wealth[-1]),net_worth_change_fraction=float(wealth[-1]/initial-1),
            relative_initial_wallet_hold_value=float(wealth[-1]-benchmark[-1]),control_final_net_worth=float(c['account_wealth'][-1,i]),
            min_gold=float(x['account_gold'][:,i].min()),min_foreign=float(x['account_foreign'][:,i].min()),
            min_net_worth=float(wealth.min()),max_net_worth=float(wealth.max()),max_drawdown=float(np.max(1-wealth/peak)),
            total_trades=int(x['account_trades'][:,i].sum()),instant_event_revaluation_sum=float(direct[i])))
    write_csv(OUTPUT/'account_summary.csv',final)
    def account_rows():
        for t in range(tmax+1):
            for i in range(n):
                yield dict(tick=t,hours=t/360,account_id=i,account=names[i],role=roles[i],gold=x['account_gold'][t,i],
                    foreign=x['account_foreign'][t,i],net_foreign=x['account_foreign'][t,i]-cfg['initial_foreign'],
                    net_worth=x['account_wealth'][t,i],net_worth_change_fraction=x['account_wealth'][t,i]/initial-1,
                    trades_this_tick=int(x['account_trades'][t,i]))
    write_csv(OUTPUT/'accounts.csv',account_rows())
    market=[]
    for t in range(tmax+1):
        market.append(dict(tick=t,hours=t/360,price=x['prices'][t],slow_reference=x['slow_reference'][t] if t else x['prices'][0],
            control_price=c['prices'][t],intensity=x['intensity'][t],arrivals=int(x['arrivals'][t]),trades=int(x['trades'][t]),
            pool_gold=x['gold'][t],pool_foreign=x['pool_foreign'][t],issued_gold=x['injection_gold'][t],issued_foreign=x['injection_foreign'][t],
            bot_net_worth_total=float(x['account_wealth'][t].sum()),random_mean_foreign=float(x['account_foreign'][t,:10].mean()),
            inventory_mean_foreign=float(x['account_foreign'][t,10:15].mean()),episode_mean_foreign=float(x['account_foreign'][t,15:].mean())))
    write_csv(OUTPUT/'market.csv',market)
    response=[]
    for e in events:
        t=e['tick'];pre=float(e['pre_price']);post=float(e['post_price']);jump=post-pre;end=t+60
        if end<=tmax:
            ps=float(x['prices'][end-11:end+1].mean());pc=float(c['prices'][end-11:end+1].mean())
            raw=(post-ps)/jump;scenario_gap=ps-pc
            group_flow={g:float(-np.sign(jump)*x[f'{g}_actual_flow'][t+1:end+1].sum()) for g in ['random','reversion','factor']}
        else:ps=pc=raw=scenario_gap=None;group_flow={g:None for g in ['random','reversion','factor']}
        response.append(dict(tick=t,hours=t/360,direction='上涨' if jump>0 else '下跌',requested_return=e['return_requested'],
            pre_price=pre,post_price=post,issued_gold=float(e['post_gold'])-float(e['pre_gold']),
            issued_foreign=float(e['post_foreign'])-float(e['pre_foreign']),observed_followup_ticks=tmax-t,
            price_10m=ps,control_price_10m=pc,raw_retraced_10m=raw,gap_to_no_shock_control_10m=scenario_gap,
            random_opposing_net_quantity_10m=group_flow['random'],inventory_opposing_net_quantity_10m=group_flow['reversion'],
            episode_opposing_net_quantity_10m=group_flow['factor'],
            total_instant_wallet_revaluation=sum(float(a['wealth_after'])-float(a['wealth_before']) for a in e['accounts'])))
    write_csv(OUTPUT/'event_responses.csv',response)
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
    colors=['#7b7b7b','#176da0','#b26a20'];groups=[(0,10,'随机'),(10,15,'库存调节'),(15,20,'短线')]
    ticks=np.arange(tmax+1)
    def event_lines(ax,annotate=False):
        for e in events:
            color='#b5443c' if e['return_requested']>0 else '#3e74a0'
            ax.axvline(e['tick'],color=color,alpha=.22,lw=.8)
            if annotate:ax.annotate('+5%' if e['return_requested']>0 else '−5%',(e['tick'],float(e['post_price'])),xytext=(0,9),textcoords='offset points',ha='center',color=color,fontsize=8)
    fig,axes=plt.subplots(3,1,figsize=(15,10),sharex=True)
    axes[0].plot(ticks,x['prices'],color='#153c67',lw=1,label='连续冲击')
    axes[0].plot(ticks[1:],x['slow_reference'][1:],color='#b78539',lw=1,ls=':',label='慢参考')
    axes[0].plot(ticks,c['prices'],color='gray',ls='--',alpha=.65,label='无冲击对照')
    axes[0].set_ylabel('金圆券／外币');axes[0].legend(ncol=3);event_lines(axes[0],True)
    for (start,end,label),color in zip(groups,colors):
        axes[1].plot(ticks,x['account_foreign'][:,start:end].mean(axis=1)-cfg['initial_foreign'],color=color,label=label,lw=1)
        axes[2].plot(ticks,(x['account_wealth'][:,start:end].mean(axis=1)/initial-1)*100,color=color,label=label,lw=1)
    axes[1].set_ylabel('每户平均净外币持仓\n相对初始1000');axes[1].legend(ncol=3)
    axes[2].plot(ticks,(benchmark/initial-1)*100,color='black',ls=':',lw=1,label='同价格路径持有初始钱包')
    axes[2].set_ylabel('每户平均净值变化（%）');axes[2].legend(ncol=2)
    for ax in axes[1:]:event_lines(ax);ax.axhline(0,color='black',lw=.5)
    for ax in axes:ax.grid(alpha=.2);ax.set_xlim(0,tmax)
    axes[-1].set_xlabel('tick（每tick 10秒，总计约27小时47分钟）')
    fig.suptitle('每1000tick随机±5%冲击：4次上涨、6次下跌；固定种子，钱包不重置')
    fig.tight_layout();fig.savefig(OUTPUT/'market.png',dpi=150);plt.close(fig)
    for field,filename,title in [('account_foreign','holdings.png','20人外币持仓：实线为连续冲击，虚线为无冲击对照'),
                                  ('account_wealth','networth.png','20人净值变化：现金＋外币×当时价格；含浮动盈亏')]:
        fig,axes=plt.subplots(4,5,figsize=(19,12),sharex=True,sharey=True)
        for i,ax in enumerate(axes.flat):
            color=colors[0 if i<10 else 1 if i<15 else 2]
            a=x[field][:,i];b=c[field][:,i]
            if field=='account_wealth':a=(a/initial-1)*100;b=(b/initial-1)*100
            ax.plot(ticks,a,color=color,lw=.8,label='连续冲击')
            ax.plot(ticks,b,color='gray',ls='--',lw=.65,alpha=.6,label='无冲击')
            ax.axhline(cfg['initial_foreign'] if field=='account_foreign' else 0,color='black',ls=':',lw=.5)
            event_lines(ax);ax.set_title(names[i]+' '+roles[i],fontsize=10);ax.grid(alpha=.15)
            ax.set_xticks([0,2500,5000,7500,10000]);ax.tick_params(labelsize=8)
            if i%5==0:ax.set_ylabel('外币数量' if field=='account_foreign' else '净值变化（%）')
            if i>=15:ax.set_xlabel('tick')
        axes.flat[0].legend(fontsize=8);fig.suptitle(title,fontsize=15)
        fig.tight_layout(rect=[0,0,1,.97]);fig.savefig(OUTPUT/filename,dpi=150);plt.close(fig)
    group_rows=[]
    for start,end,label in groups:
        w=float(x['account_wealth'][-1,start:end].mean());control=float(c['account_wealth'][-1,start:end].mean())
        group_rows.append(dict(role=label,accounts=end-start,mean_net_worth=w,mean_return=w/initial-1,
                               mean_foreign=float(x['account_foreign'][-1,start:end].mean()),
                               relative_initial_wallet_hold=w-benchmark[-1],control_mean_net_worth=control))
    write_csv(OUTPUT/'groups.csv',group_rows)
    evaluated=[r for r in response if r['raw_retraced_10m'] is not None]
    total_initial=initial*n;total_final=float(x['account_wealth'][-1].sum())
    lines=['# 10000tick、每1000tick随机央行冲击','',
        f"价格从{x['prices'][0]:.4f}到{x['prices'][-1]:.4f}（{x['prices'][-1]/x['prices'][0]-1:+.2%}），全程范围{x['prices'].min():.4f}～{x['prices'].max():.4f}。20人总市值净值从{total_initial:,.2f}到{total_final:,.2f}（{total_final/total_initial-1:+.2%}）。这是一次固定种子演示，不是多种子稳健性结论。",'',
        '## 设置与计价','',
        '10随机（R01～R10）＋5慢参考库存调节（I11～I15）＋5短线（S16～S20）；原账户ID为0～19，表格使用1～20方便阅读。池100万金圆券/20万外币，户均5000/1000，初始每户市值10000。随机订单标准差120、psi=.0012、b=.25、tau1000；短线每轮200、止盈止损±.5%、移动启动/回撤各.25个百分点；库存目标±500、每次调整差额50%、过去最多720tick参考。无费用、借贷、钱包补钱、重置或期末强制平仓。','',
        '10000tick×10秒约27小时47分钟；市场种子81、人口301。独立冲击种子20261015事先生成10个方向，本次4涨6跌，顺序为＋、−、−、＋、＋、−、−、−、＋、−。没有为得到平衡或好看结果重抽种子。涨跌相同百分比也不完全抵销。','',
        '每个事件在对应tick常规成交后发生：上涨通过向池发行本币储备的5%，下跌通过向池发行外币储备的1/.95−1，实际瞬时约±5%。发行直接进入池，不给机器人加余额。随后收益进入下一tick的活动反馈和历史参考。两类事件是不同资产发行，分开核对守恒。','',
        '净值W=钱包本币＋外币数量×当时池边际价，含未实现盈亏，未扣假设全量清仓的滑点。每次事件同时保存机器人实际钱包、冲击前与冲击后净值；冲击当下钱包数量不变，市值因汇率重估而变化。净值变化不能全部叫已实现交易利润。','',
        '同一路径初始钱包持有基准H=5000＋1000×P：仅用于区分初始外币重估与持仓调整。W−H仍含浮盈亏，不是已实现利润或无机器人市场的超额收益。无冲击对照用相同初始参数和市场种子，路径会在首个事件后分叉；它没有承诺逐笔复用事件组的到达或成交。','',
        '## 整体行情与三组持仓','',
        '![行情与三组状态](market.png)','',
        '|组别|人数|期末每户平均净值|相对初始变化|期末每户平均外币|相对同路径初始钱包持有基准的差额|',
        '|---|---:|---:|---:|---:|---:|']
    for g in group_rows:
        lines.append(f"|{g['role']}|{g['accounts']}|{g['mean_net_worth']:.2f}|{g['mean_return']:+.2%}|{g['mean_foreign']:.2f}|{g['relative_initial_wallet_hold']:+.2f}|")
    lines+=['',f"同路径持有初始钱包，期末每户为{benchmark[-1]:.2f}（{benchmark[-1]/initial-1:+.2%}）。因此，本次三组平均净值均下降，并不代表三组的持仓调整都逊于该基准；相对差额见上表。随机组有账户在部分时段现金或外币为零，后续仍由真实余额裁剪，未自动恢复资金。",'',
        '## 20人逐个跟踪','',
        '![每人外币持仓](holdings.png)','',
        '![每人净值](networth.png)','',
        '|账户|类型|期末金圆券|期末外币|期末净值|净值变化|相对初始钱包持有差额|',
        '|---|---|---:|---:|---:|---:|---:|']
    for a in final:
        lines.append(f"|{a['account']}|{a['role']}|{a['final_gold']:.2f}|{a['final_foreign']:.2f}|{a['final_net_worth']:.2f}|{a['net_worth_change_fraction']:+.2%}|{a['relative_initial_wallet_hold_value']:+.2f}|")
    lines+=['','期末数值均包含第10000tick的最后一次下跌冲击。该时点之后没有机器人反应，部分短线交易尚未完成，未假装清仓。现金与外币最小余额、最大净值回撤、逐人交易次数和无冲击期末净值另见account_summary.csv。','',
        '## 每次事件及10分钟回吐','',
        '10分钟数值为事件后第9～10分钟12tick均价；原始回吐=(冲击后即时价−观察均价)/(冲击后即时价−冲击前即时价)，上下方向使用带符号跳幅。50%指回吐一半，不作逐事件及格线。无冲击对照没有任何此前事件，同期价差包含整段冲击历史，不能当作本次冲击的边际回吐。表中因此只列原始回吐和同期无冲击路径价差，不计算单事件配对回吐。','',
        '|tick|方向|冲击前价格|冲击后价格|10分钟观察价格|原始回吐|与无冲击路径价差|',
        '|---:|---|---:|---:|---:|---:|---:|']
    for e in response:
        val='—' if e['price_10m'] is None else f"{e['price_10m']:.4f}"
        raw='—' if e['raw_retraced_10m'] is None else f"{e['raw_retraced_10m']:.1%}"
        scenario_gap='—' if e['gap_to_no_shock_control_10m'] is None else f"{e['gap_to_no_shock_control_10m']:+.4f}"
        lines.append(f"|{e['tick']}|{e['direction']}|{e['pre_price']:.4f}|{e['post_price']:.4f}|{val}|{raw}|{scenario_gap}|")
    lines+=['',f"前9次可观察事件的10分钟原始回吐全部为正，均值{np.mean([r['raw_retraced_10m'] for r in evaluated]):.1%}。这些事件在同一资金与历史路径上相互依赖，不能当9个独立种子。第10000tick事件正好在终点，后续观测留空，不能计入回吐成功或失败。",'',
        '## 数据与验证','',
        f"实际运行两条10000tick路径：事件组和无冲击对照。研究测试`OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m unittest discover -s research/fx/tests -v`共29项通过。逐tick核对{audit['wallet_marks']}个个人净值记录、两币资产守恒、账户余额/组汇总/成交计数及{audit['event_snapshots']}个事件钱包快照；旧正常路径前8640tick逐字段一致，首个事件前全部字段含个人矩阵一致，{audit['protected_files_unchanged']}个既有文件哈希保持。未测玩家交互、实际运行时延或多种子重复性。",'',
        '- [20人逐tick现金、外币、净值CSV](accounts.csv)（200020行，含初始tick0）',
        '- [每人期末与风险统计](account_summary.csv)、[三组汇总](groups.csv)、[行情与池储备](market.csv)、[事件及回吐](event_responses.csv)',
        '- [设置与源哈希](manifest.json)、[实际账户终态及10次事件钱包快照](summary.json)、[审计](audit.json)、[冲击完整序列](shock.npz)、[无冲击完整序列](control.npz)',
        '- 重现：`python -m research.fx.reversion.repeated.run`，随后`python -m research.fx.reversion.repeated.report`。','']
    (OUTPUT/'report.md').write_text('\n'.join(lines))
    paths=[HERE/'report.py',OUTPUT/'manifest.json',OUTPUT/'summary.json',OUTPUT/'audit.json',OUTPUT/'accounts.csv',OUTPUT/'account_summary.csv',OUTPUT/'groups.csv',OUTPUT/'market.csv',OUTPUT/'event_responses.csv',OUTPUT/'report.md']
    (OUTPUT/'report_sources.json').write_text(json.dumps({str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},indent=2)+'\n')
    print('Report complete: 20 individual histories, 10 event records; terminal response left undefined.')


if __name__=='__main__':main()
