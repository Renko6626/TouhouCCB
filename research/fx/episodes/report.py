"""Report the authorized exploratory behavior and shock scenarios without tuning."""
import csv
import hashlib
import json
from decimal import Decimal as D

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from backend.app.services.fx.amm import quote_sell,quote_buy_exact_out
from research.fx.factor_activity.run import ROOT
from .run import HERE,OUTPUT


def write_csv(path,rows):
    with path.open('w') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)


def main():
    manifest=json.loads((OUTPUT/'manifest.json').read_text());spec=manifest['spec']
    records=json.loads((OUTPUT/'summary.json').read_text());audit=json.loads((OUTPUT/'audit.json').read_text())
    assert len(records)==48 and all(r['status']=='complete' for r in records)
    normal=[r for r in records if r['stage']=='normal'];shock=[r for r in records if r['stage']=='shock']
    old_normal_dir=HERE.parent/'mixed/results/exploration';old_shock_dir=HERE.parent/'injection/results/gold100k'
    old_normal=json.loads((old_normal_dir/'summary.json').read_text())
    old_shock=[r for r in json.loads((old_shock_dir/'summary.json').read_text()) if r['seed'] in spec['market_seeds']]
    sources={}
    for directory in [old_normal_dir,old_shock_dir]:
        for path,h in json.loads((directory/'manifest.json').read_text())['source_sha256'].items():
            assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==h,path
            sources[path]=h
    reference_keys=['accounts','pool_gold','pool_foreign','initial_gold','initial_foreign','fee_rate',
                    'tau','psi','mean_arrivals','initial_z','order_sigma_foreign','max_pool_fraction',
                    'trend_window','mean_window','factor_mix','noise_sigma','sensitivity','position_cap','adjustment']
    for r in old_normal+old_shock:
        assert all(r['config'][k]==manifest['config'][k] for k in reference_keys)
    assert json.loads((old_normal_dir/'manifest.json').read_text())['profiles']==manifest['profiles']
    assert json.loads((old_shock_dir/'manifest.json').read_text())['profiles']==manifest['profiles']
    (OUTPUT/'controls.json').write_text(json.dumps(dict(normal_directory=str(old_normal_dir.relative_to(ROOT)),
        shock_directory=str(old_shock_dir.relative_to(ROOT)),market_seeds=spec['market_seeds'],
        sources=sources,normal_summary_sha256=hashlib.sha256((old_normal_dir/'summary.json').read_bytes()).hexdigest(),
        shock_summary_sha256=hashlib.sha256((old_shock_dir/'summary.json').read_bytes()).hexdigest()),indent=2)+'\n')

    def group(data,variant,count,b=.25):
        return [r for r in data if r.get('variant')==variant and r['random_count']==count and r['b']==b]

    def mean(values):
        return float(np.mean(values)) if values and all(v is not None for v in values) else None

    def fm(value,digits=5):
        return '未定义' if value is None else f'{value:.{digits}f}'

    def bh(rows,key):
        return mean([float(r['summary']['behavior'][key]) if r['summary']['behavior'][key] is not None else None for r in rows])

    def metric(rows,key):return mean([r['metrics'][key] for r in rows])

    nrows=[];segments=[];srows=[];windows=[]
    for r in normal:
        beh=r['summary']['behavior'];m=r['metrics']
        nrows.append(dict(variant=r['variant'],random_count=r['random_count'],b=r['b'],seed=r['seed'],
            **m,opened=beh['opened'],completed=beh['completed'],unfinished=beh['unfinished'],
            take_profit=beh['exits']['take_profit'],stop_loss=beh['exits']['stop_loss'],trailing=beh['exits']['trailing'],
            mean_completed_holding=beh['mean_completed_holding'],realized_episode_pnl=beh['realized_pnl'],
            unrealized_episode_pnl=beh['unrealized_pnl'],last_behavior_trade_tick=beh['last_behavior_trade_tick'],
            post_burn_entries=beh['post_burn_entries'],carry_in=beh['carry_in'],post_burn_completions=beh['post_burn_completions'],
            longest_no_trade_ticks=beh['longest_no_trade_ticks']))
        for s in r['segments']:segments.append(dict(variant=r['variant'],random_count=r['random_count'],b=r['b'],seed=r['seed'],
            segment=s['segment'],first_tick=s['first_tick'],last_tick=s['last_tick'],**s['metrics']))
    for r in shock:
        srows.append(dict(variant=r['variant'],random_count=r['random_count'],b=r['b'],seed=r['seed'],**r['response']['terminal']))
        for label in ['control','shock']:
            fills=[json.loads(line) for line in (OUTPUT/'shock'/r['name']/f'{label}-fills.jsonl').read_text().splitlines()]
            for width in [30,100,300,10000]:
                ff=[f for f in fills if spec['shock_tick']<f['tick']<=spec['shock_tick']+width]
                windows.append(dict(variant=r['variant'],random_count=r['random_count'],b=r['b'],seed=r['seed'],case=label,width=width,
                    entries=sum(f['action']=='open' for f in ff),exit_fills=sum(f['action']=='close' for f in ff),
                    behavior_net_bought=sum(f['actual_flow'] for f in ff),
                    long_take_profit_sold=sum(float(f['quantity']) for f in ff if f['action']=='close' and f['direction']==1 and f['reason']=='take_profit'),
                    inventory_stop_loss_bought=sum(float(f['quantity']) for f in ff if f['action']=='close' and f['direction']==-1 and f['reason']=='stop_loss')))
    for name,rows in [('normal.csv',nrows),('segments.csv',segments),('shock.csv',srows),('shock-actions.csv',windows)]:write_csv(OUTPUT/name,rows)

    plt.rcParams.update({'font.family':'Noto Sans CJK SC','font.size':10,
                         'axes.spines.top':False,'axes.spines.right':False})
    colors={'fixed':'#246395','trailing':'#b66b21','old':'#777777'}
    names={'fixed':'固定退出','trailing':'增加移动退出','old':'旧目标持仓'}
    seed=81
    fig,axes=plt.subplots(3,2,figsize=(13,11),constrained_layout=True)
    for col,count in enumerate([0,15]):
        for variant in ['fixed','trailing']:
            r=next(r for r in normal if r['variant']==variant and r['random_count']==count and r['b']==.25 and r['seed']==seed)
            with np.load(OUTPUT/'normal'/r['name']/'series.npz') as x:
                ii=np.arange(0,1001) if count==0 else np.arange(0,100001,50)
                axes[0,col].plot(ii,100*(x['prices'][ii]/5-1),color=colors[variant],label=names[variant],
                                 linestyle='-' if variant=='fixed' else '--',linewidth=1.4)
                axes[1,col].plot(np.arange(0,100001,50),np.cumsum(x['completions'])[::50],color=colors[variant],label=names[variant],
                                 linestyle='-' if variant=='fixed' else '--')
                axes[2,col].plot(np.arange(0,100001,50),x['holding_accounts'][::50],color=colors[variant],label=names[variant],
                                 linestyle='-' if variant=='fixed' else '--',linewidth=1.1)
        old=next(r for r in old_normal if r['random_count']==count and r['b']==.25 and r['seed']==seed)
        with np.load(old_normal_dir/old['name']/'series.npz') as x:
            axes[0,col].plot(ii,100*(x['prices'][ii]/5-1),color=colors['old'],label=names['old'],linewidth=1,alpha=.75)
        axes[0,col].set_title(f'{20-count}个行为账户＋{count}个随机账户',loc='left',fontsize=12)
        axes[0,col].set_ylabel('相对初价5的变化 / %');axes[1,col].set_ylabel('累计完成交易轮');axes[2,col].set_ylabel('正在持仓的行为账户数')
        axes[0,col].legend(frameon=False,fontsize=9)
        for row in range(3):
            axes[row,col].set_xlabel('tick');axes[row,col].grid(axis='y',alpha=.18)
    fig.suptitle('常态行情与交易过程：固定展示行情种子81、活动反馈开启\n左上放大初期1000tick；其他图覆盖10万tick；固定/移动退出在全行为组重合',fontsize=14)
    fig.savefig(OUTPUT/'normal.png',dpi=160);plt.close(fig)

    fig,axes=plt.subplots(2,2,figsize=(13,8),constrained_layout=True)
    for col,count in enumerate([0,15]):
        for variant in ['fixed','trailing']:
            r=next(r for r in shock if r['variant']==variant and r['random_count']==count and r['b']==.25 and r['seed']==seed)
            pre=r['response']['pre_price'];t0=spec['shock_tick']
            with np.load(OUTPUT/'shock'/r['name']/'shock.npz') as x,np.load(OUTPUT/'shock'/r['name']/'control.npz') as ctrl:
                for row,end in [(0,11000),(1,1500)]:
                    ii=np.arange(t0-100,end+1,10 if row==0 else 1)
                    axes[row,col].plot(ii-t0,100*(x['prices'][ii]/pre-1),color=colors[variant],label=names[variant]+'：注资',
                                       linestyle='-' if variant=='fixed' else '--',linewidth=1.5)
                    axes[row,col].plot(ii-t0,100*(ctrl['prices'][ii]/pre-1),color=colors[variant],alpha=.5,linestyle=':',linewidth=1,
                                       label=names[variant]+'：无注资')
        axes[0,col].set_title(f'{20-count}个行为账户＋{count}个随机账户',loc='left',fontsize=12)
        for row in range(2):
            axes[row,col].axvline(0,color='#777777',linestyle=':',linewidth=.8)
            axes[row,col].axhline(0,color='#777777',linewidth=.7)
            axes[row,col].set_ylabel('相对该路径注资前价格 / %');axes[row,col].set_xlabel('距注资 / tick')
            axes[row,col].grid(axis='y',alpha=.18)
        axes[0,col].legend(frameon=False,fontsize=8)
    fig.suptitle('10万金圆券直接注入池本币侧：固定展示种子81、活动反馈开启\n上排为后续1万tick，下排放大前500tick；其余种子结局在表格完整报告',fontsize=14)
    fig.savefig(OUTPUT/'shock.png',dpi=160);plt.close(fig)

    # First completed trailing episode, chosen by chronological completion, not by profit.
    r=next(r for r in normal if r['variant']=='trailing' and r['random_count']==15 and r['b']==.25 and r['seed']==seed)
    directory=OUTPUT/'normal'/r['name']
    episodes=json.loads((directory/'series-episodes.json').read_text())
    e=min((e for e in episodes if e['close_tick'] is not None and e['closing_reason']=='trailing'),key=lambda e:(e['close_tick'],e['id']))
    fills=[json.loads(line) for line in (directory/'series-fills.jsonl').read_text().splitlines()]
    final=next(f for f in fills if f['episode_id']==e['id'] and f['complete'])
    rates=[];times=list(range(e['entry_tick'],e['close_tick']))
    with np.load(directory/'series.npz') as x:
        for tick in times:
            if e['direction']==1:
                q=quote_sell(D(e['entry_quantity']),D(str(x['gold'][tick])),D(str(x['pool_foreign'][tick])),D('0'))
                rate=float((q.output_amount-D(e['entry_gold']))/D(e['entry_gold']))
            else:
                q=quote_buy_exact_out(D(e['entry_quantity']),D(str(x['gold'][tick])),D(str(x['pool_foreign'][tick])),D('0'))
                rate=float((D(e['entry_gold'])-q.input_amount)/D(e['entry_gold']))
            rates.append(rate*100)
    times.append(e['close_tick']);rates.append(float(e['realized_profit'])/float(e['entry_gold'])*100)
    fig,ax=plt.subplots(figsize=(10,4.5),constrained_layout=True)
    ax.plot(times,rates,color=colors['trailing'],linewidth=1.5)
    ax.scatter([e['close_tick']],[rates[-1]],color=colors['trailing'],s=45,zorder=3)
    ax.axhline(0,color='#777777',linewidth=.8)
    ax.axhline(1,color='#777777',linestyle=':',linewidth=.8);ax.axhline(-1,color='#777777',linestyle=':',linewidth=.8)
    ax.set_ylim(min(-1.1,min(rates)-.1),max(1.1,max(rates)+.1))
    ax.set_xlabel('tick');ax.set_ylabel('该轮净收益 / 开仓本币基准，%');ax.grid(axis='y',alpha=.18)
    ax.set_title(f"首个完成移动退出的交易轮：账户{e['account']}，持有{e['close_tick']-e['entry_tick']}tick\n实际开仓基准{float(e['entry_gold']):.2f}，实际已实现损益{float(e['realized_profit']):.2f}金圆券",loc='left',fontsize=12)
    fig.savefig(OUTPUT/'episode.png',dpi=160);plt.close(fig)
    (OUTPUT/'illustrated_episode.json').write_text(json.dumps(dict(episode=e,close_fill=final,
        selection='first completed trailing exit in seed81 normal trailing random15 b.25',
        curve='end-of-tick hypothetical liquidation quotes; last point is actual completed PnL'),ensure_ascii=False,indent=2)+'\n')

    lines=['# 一次建仓、持有后退出：常态与央行注资探索','',
        '**个人交易过程已经能运行，但结果分成两种情形：全行为组会停盘；混合组能持续完成交易轮。注资后仍没有可靠的旧价恢复。** 这轮没有据结果调整参数或补上强制退出机制。','',
        '## 设置与行为规则','',
        '用户确认[实验提案](../../../BEHAVIOR_PROPOSAL.md)后直接实施。20户双币钱包，池100万金圆券/20万外币，户均5000/1000，费用0。个人三因子沿用30/30窗口、人口301/spread=1及原两类权重；参数固定。仅在账户到达且空闲、abs(g)>=个人sigma时开一轮，意向100外币；正信号买入后卖出，负信号卖出已有外币后回补，无借贷。每户每tick最多一次实际成交；持有期间不加仓、不调整目标仓位。','',
        '固定退出：可兑现收益+1%止盈、−1%止损。移动退出：保留上述条件；观察到收益曾达+0.5%，随后较已观察最高收益回撤0.5个百分点时退出。仅在账户到达时观察/执行，不是逐tick自动挂单。触发后部分成交保留原退出意图，在后续到达处理剩余；没有自动补钱或强制最长持仓。','',
        '每轮基准为实际买入付出本币，或实际卖出自有外币取得本币；当前损益用生产AMM全剩余数量的平仓报价计算，含滑点和费用。全部退出可能因现金不足无法支付，实际执行仍裁剪钱包并保留未平数量。初始1000外币底仓不虚构成本；其市值变化与交易轮利润分开，已实现利润不是总钱包收益率。','',
        '共享活动反馈仍为a=exp(-1/1000)、z_next=a*z+sqrt(1-a²)*r/.0004、Lambda=(1-b)+b*z²；b=0/.25，20户分别Poisson(Lambda/20)到达并洗牌。常态24条×100000tick、前5000预热；注资24对×11000tick，在第1000tick常规成交后向池本币侧直接加10万，配对不注资。两种退出×两种构成（20行为或15随机+5行为）×开关×行情81/82/83，共72条路径、2928000tick。没有外部价格或基本价值。','',
        '旧目标持仓与v1对照复用相同人口/种子的既有结果，不重新跑旧模型。参数与来源检查见[controls.json](controls.json)。本次注资对照只取81–83三种子，不能与上一轮十种子均值直接比较。','',
        '## 常态：全行为组停在持仓等待','',
        '固定和移动退出、反馈开和关、三个种子共12条全行为路径，均只建20轮、完成0轮、期末20户仍持有。最后成交落在第118–267tick；预热后95000tick没有成交、价格完全不动。持仓在期末仍未结束，不能把它们的持有时长当作已完成样本。','',
        '这不是“涨跌相关降到零”的成功：常数收益的ACF、偏度和峰度没有定义，报告保留null。大家都等阈值、却没有其他人持续成交，行情就没有新的变动来源；热度规则只增加到达，不会强迫持仓者下单。','',
        '## 常态：15随机＋5行为的交易过程','',
        '以下均开启反馈。交易轮覆盖完整10万tick；市场指标随后使用去预热的95000tick。持有时长只对已完成轮次计算，未完成者单列；利润是5个行为账户的新交易轮已实现损益之和，再平均三个行情种子。','',
        '|退出规则|每路径完成轮数均值|已完成轮平均持有tick|三个种子合计止盈／止损／移动退出|每路径已实现损益均值|期末未完成轮数均值|',
        '|---|---:|---:|---:|---:|---:|']
    for variant in ['fixed','trailing']:
        gg=group(normal,variant,15)
        reasons=[sum(r['summary']['behavior']['exits'][k] for r in gg) for k in ['take_profit','stop_loss','trailing']]
        lines.append(f"|{names[variant]}|{bh(gg,'completed'):.1f}|{fm(bh(gg,'mean_completed_holding'),1)}|{'／'.join(str(z) for z in reasons)}|{bh(gg,'realized_pnl'):.2f}|{bh(gg,'unfinished'):.2f}|")
    lines+=['','![常态行情与交易过程](normal.png)','',
        '图示固定取行情种子81，展示一条实现；表格使用全部三个种子，未挑选漂亮路径。移动退出完成的轮次更多、持有更短，但两组已实现损益均值都为负；这不支持“加了退出就能稳定盈利”。未实现损益和全部逐路径结局见[normal.csv](normal.csv)。','',
        '![一轮真实退出](episode.png)','',
        '示例按时间选择种子81混合组首个完成移动退出的轮次。曲线为每tick收盘储备计算的假设全量平仓收益，仅用于展示；机器人高点只在实际到达时更新，最后一点为真实成交损益。曲线不能理解为每tick都在监控或按收盘价成交。原始记录见[illustrated_episode.json](illustrated_episode.json)。','',
        '## 常态：市场指标及成交占比','',
        'ACF对全部去预热tick的对数收益r、abs(r)、r²减均值后计算滞后乘积和/零滞后平方和；不作N/(N−lag)修正。区间为逐lag均值；方向R为lag1–1000收益ACF最大绝对值，不是显著性检验。表格是逐路径统计再等权平均，未拼接路径。超额峰度以正态为0，未作幂律尾或严格长记忆检验。','',
        '|模型（开启反馈）|abs(r) ACF 101–300|r² ACF 301–1000|方向R|行为成交量占比|每tick外币总成交量|',
        '|---|---:|---:|---:|---:|---:|']
    old15=[r for r in old_normal if r['random_count']==15 and r['b']==.25]
    old20=[r for r in old_normal if r['random_count']==20 and r['b']==.25]
    marketrows=[('旧目标持仓：15随机＋5三因子',old15,mean([1-r['metrics']['random_volume_share'] for r in old15]))]
    for variant in ['fixed','trailing']:
        gg=group(normal,variant,15)
        share=mean([r['metrics']['post_burn_behavior_quantity']/r['metrics']['executed_abs_quantity'] for r in gg])
        marketrows.append((names[variant]+'：15随机＋5行为',gg,share))
    marketrows.append(('纯随机v1：20随机',old20,None))
    for label,gg,share in marketrows:
        lines.append(f"|{label}|{fm(metric(gg,'abs_101_300'))}|{fm(metric(gg,'sq_301_1000'))}|{fm(metric(gg,'max_signed'))}|{'不适用' if share is None else format(share,'.2%')}|{metric(gg,'gross_quantity_per_tick'):.3f}|")
    lines+=['',
        '**混合组保留了中尺度波动相关，收益方向相关与纯随机参照处于相近量级。**但行为账户仅贡献约3.65%/5.55%的成交量，低于旧目标持仓组约12.52%；统计变化同时包含入场、下单量、交易时机和持有方式变化，不能全归功于退出算法，也不能称为策略生态已涌现。只有固定与移动版本之间是单独增加移动退出的比较。','',
        '|混合退出规则|关闭反馈 r² ACF 301–1000|开启反馈 r² ACF 301–1000|开启反馈超额峰度|开启反馈平均预定强度|',
        '|---|---:|---:|---:|---:|']
    for variant in ['fixed','trailing']:
        on=group(normal,variant,15);off=group(normal,variant,15,0)
        lines.append(f"|{names[variant]}|{fm(metric(off,'sq_301_1000'))}|{fm(metric(on,'sq_301_1000'))}|{fm(metric(on,'excess_kurtosis'),2)}|{fm(metric(on,'mean_intensity'),3)}|")
    lines+=['','三等分来自同一预热后序列，边界5001–36667、36668–68334、68335–100000；不重置状态。所有72段结果见[segments.csv](segments.csv)。','',
        '## 央行注资：会触发退出，旧价恢复仍不可靠','',
        '第1000tick成交完成后直接向池本币侧增加10万，约造成10%直接上涨；不算机器人成交。下一tick活动包含这次跳价，最高观察强度列为有限样本峰值。主表使用最后500tick均价，相对实际注资前即时价格P_minus；原始回落=(P_plus−末段均价)/(P_plus−P_minus)，对照校正回落=1−(注资末段均价−无注资末段均价)/(P_plus−P_minus)。原始回落100%表示回到注资前旧价，负值表示末段高于注资后即时价格；对照校正回落100%表示与同期无注资对照同价，负值表示两条路径价差超过最初跳幅。对照自身可能漂移，两项不能混用。各时点原始值保存在result.json。','',
        '|行为规则／构成（开启反馈）|末段仍高于旧价|原始涨幅回落比例|对照校正回落|行为账户净卖出外币均值|最大预定强度|',
        '|---|---:|---:|---:|---:|---:|']
    for count in [0,15]:
        for variant in ['fixed','trailing']:
            gg=group(shock,variant,count);tt=[r['response']['terminal'] for r in gg]
            lines.append(f"|{names[variant]}，{20-count}行为＋{count}随机|{np.mean([t['price_change_from_pre'] for t in tt]):.2%}|{np.mean([t['retraced_fraction'] for t in tt]):.1%}|{np.mean([t['paired_retraced_fraction'] for t in tt]):.1%}|{np.mean([t['factor_net_sold'] for t in tt]):.1f}|{max(t['peak_post_intensity'] for t in tt):.2f}|")
    lines+=['','![注资后的价格响应](shock.png)','',
        '全行为组注资后每条路径完成20轮退出，随后重新建仓，再次停止成交；固定和移动版本此时相同。注资确实让个人成本阈值产生动作，但卖出已有外币的账户在上涨后止损回补，会形成买单，不能把所有止损都理解成下跌卖出。','',
        '|规则／构成（开启反馈）|注资后100tick内新入场数均值|退出成交数均值|买入轮止盈卖出量均值|减持轮止损回补量均值|该时段行为净买入量均值|',
        '|---|---:|---:|---:|---:|---:|']
    for count in [0,15]:
        for variant in ['fixed','trailing']:
            ww=[w for w in windows if w['variant']==variant and w['random_count']==count and w['b']==.25 and w['case']=='shock' and w['width']==100]
            lines.append(f"|{names[variant]}，{20-count}行为＋{count}随机|"+'|'.join(f'{np.mean([w[k] for w in ww]):.1f}' for k in ['entries','exit_fills','long_take_profit_sold','inventory_stop_loss_bought','behavior_net_bought'])+'|')
    lines+=['',
        '全部24条注资路径（含关闭反馈）未触及旧价，末500tick均价也未回到旧价±1%。全行为组每户仅一轮100外币，总净偏离底仓约±2000，注资前后最大可卖出差额约4000，而恢复旧价需近1万：这一组完全恢复在所选容量下本就不可达。混合组没有同样的总容量限制，但本轮仍未出现可靠恢复。不能据此推断所有成本退出模型都不能回落。','',
        '|混合退出规则|关闭反馈末段仍高于旧价|开启反馈末段仍高于旧价|开启反馈三个种子末段涨幅范围|',
        '|---|---:|---:|---:|']
    for variant in ['fixed','trailing']:
        on=[r['response']['terminal']['price_change_from_pre'] for r in group(shock,variant,15)]
        off=[r['response']['terminal']['price_change_from_pre'] for r in group(shock,variant,15,0)]
        lines.append(f"|{names[variant]}|{np.mean(off):.2%}|{np.mean(on):.2%}|{min(on):.2%}～{max(on):.2%}|")
    lines+=['','## 当前取舍与范围','',
        '这轮支持把成本和回撤作为可理解的持有/退出依据，也暴露了独立运行时的成交停滞。15随机＋5行为可以作为继续研究的背景候选；行为账户占成交量很少，尚未验证玩家参与时的竞争、公平性或可利用性。移动退出增加交易轮数，未证明提高收益或价格修复能力。','',
        '本轮不新增强制超时、补钱、价格锚或更多性格，不事后改1%阈值。下一步需要决定是否接受随机背景承担大部分成交，以及如何评价这些交易轮对玩家的意义；不为避免停盘自动追加策略。所有结论仅限一个参数人口、三个行情种子和已跑时段，未经过真人行为校准或跨人口稳健性验证。','',
        '## 验证与复现','',
        f"已有19项加5项关键行为检查，共24项通过：`OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m unittest discover -s research/fx/tests -v`。保存序列审计覆盖{audit['paths']}路径、{audit['episodes']}轮、{audit['behavior_fills']}笔行为成交、{audit['entry_signal_checks']}个入场信号、{audit['acf_windows']}个统计窗口及{audit['response_checks']}项响应。逐笔重放生产报价，核对成本分配、真实盈亏、残量、单tick限制、逐tick发行守恒和下一tick反馈。原{audit['old_files_unchanged']}个文件哈希未变。",'',
        '实际模拟没有出现部分平仓；现金不足时保留残量的行为由关键单元检查覆盖。零行为账户时的原随机控制，在反馈开关及注资开关下与原循环34个共同字段逐点一致（实际字段数见compatibility.json）；常态前11000tick与注资场景的无注资控制完全相同，注资前所有字段完全相同。报告保留常数ACF未定义，未运行游戏UI或玩家交互测试。','',
        '- [配置、个人参数和模拟来源哈希](manifest.json)、[原始序列核对](audit.json)、[原文件哈希快照](preexisting_sha256.json)、[随机控制兼容](compatibility.json)',
        '- [全部常态行为及市场指标](normal.csv)、[三等分](segments.csv)、[注资主要结局](shock.csv)、[注资前后行为动作](shock-actions.csv)',
        '- [全部结果及账户状态](summary.json)，各路径目录含npz、交易轮JSON和逐笔行为成交JSONL。',
        '- 重现：`python -m research.fx.episodes.run`，随后`python -m research.fx.episodes.audit`、`python -m research.fx.episodes.report`。缓存仅在配置与来源哈希匹配时复用。','']
    (OUTPUT/'report.md').write_text('\n'.join(lines))
    (OUTPUT/'report_sources.json').write_text(json.dumps({str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [HERE/'report.py',HERE/'audit.py',OUTPUT/'summary.json',OUTPUT/'audit.json',OUTPUT/'controls.json',OUTPUT/'preexisting_sha256.json']},indent=2)+'\n')
    print('Report written:',OUTPUT/'report.md')


if __name__=='__main__':main()
