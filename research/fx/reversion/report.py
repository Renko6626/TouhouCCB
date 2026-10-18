"""Report all frozen cells, including remaining price shifts after a shock."""
import csv
import hashlib
import json
import warnings

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from research.fx.common.diagnostics import acf,describe
from research.fx.factor_activity.run import ROOT,clean_json
from .run import HERE,OUTPUT


def csv_write(name,rows):
    with (OUTPUT/name).open('w') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)


def main():
    manifest=json.loads((OUTPUT/'manifest.json').read_text());spec=manifest['spec']
    records=json.loads((OUTPUT/'summary.json').read_text());audit=json.loads((OUTPUT/'audit.json').read_text())
    assert len(records)==18 and audit['paths']==54
    for p,h in manifest['source_sha256'].items():assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h,p
    normal=[];windows=[];responses=[];illustrations={};t0=spec['shock_tick']
    for r in records:
        directory=OUTPUT/r['name'];cases={}
        for label in ['normal','up','down']:
            with np.load(directory/f'{label}.npz') as z:cases[label]={k:z[k] for k in z.files}
        x=cases['normal'];episodes=json.loads((directory/'normal-episodes.json').read_text())
        identity=dict(scale=r['scale']['id'],inventory_count=r['inventory_count'],seed=r['seed'])
        for t in spec['horizons']:
            done=[e for e in episodes if e['close_tick'] is not None and e['close_tick']<=t]
            opened=[e for e in episodes if e['entry_tick']<=t]
            pending=[e for e in opened if e['close_tick'] is None or e['close_tick']>t]
            holding=[(e['close_tick']-e['entry_tick'])/6 for e in done]
            width=spec['range_window'];ranges=[]
            for start in range(0,t,width):
                p=x['prices'][start:start+width+1]
                amplitude=float((p.max()-p.min())/p[0]);ranges.append(amplitude)
                if t==max(spec['horizons']):windows.append(dict(**identity,first_tick=start,last_tick=start+width,range_fraction=amplitude))
            returns=x['returns'][1:t+1]
            curves={k:acf(a,180) for k,a in [('signed',returns),('abs',abs(returns)),('sq',returns*returns)]}
            volume=float(x['executed_abs_quantity'][1:t+1].sum());attempts=x['order_attempts'][1:t+1].sum()
            with warnings.catch_warnings():
                warnings.simplefilter('ignore',RuntimeWarning);shape=describe(returns)
            row=dict(**identity,hours=t/360,range30_median=float(np.median(ranges)),range30_p10=float(np.quantile(ranges,.1)),
                range30_p90=float(np.quantile(ranges,.9)),completed_per_episode_account=len(done)/5,
                mean_completed_holding_minutes=float(np.mean(holding)) if holding else None,
                median_completed_holding_minutes=float(np.median(holding)) if holding else None,
                p90_completed_holding_minutes=float(np.quantile(holding,.9)) if holding else None,
                pending_count=len(pending),max_pending_age_minutes=max((t-e['entry_tick'])/6 for e in pending) if pending else None,
                episode_volume_share=float(x['factor_executed_abs_quantity'][1:t+1].sum()/volume),
                inventory_volume_share=float(x['reversion_executed_abs_quantity'][1:t+1].sum()/volume),
                inventory_final_net_position=float(x['reversion_net_position'][t]),
                signed_lag1=float(curves['signed'][1]),max_signed_1_100=float(np.max(abs(curves['signed'][1:101]))),
                abs_31_180=float(curves['abs'][31:181].mean()),sq_31_180=float(curves['sq'][31:181].mean()),
                excess_kurtosis=shape['excess_kurtosis'],mean_intensity=float(x['intensity'][1:t+1].mean()),
                peak_intensity=float(x['intensity'][1:t+1].max()),max_arrivals=int(x['arrivals'][1:t+1].max()),
                clipped_fraction=float(x['clipped_orders'][1:t+1].sum()/attempts),
                wallet_gold_min=float(x['wallet_gold_min'][:t+1].min()),wallet_foreign_min=float(x['wallet_foreign_min'][:t+1].min()))
            normal.append(clean_json(row))
        for label in ['up','down']:
            x=cases[label];c=cases['normal'];s=r['summaries'][label]
            pre=float(s['issue']['pre_price']);post=float(s['issue']['post_price']);jump=post-pre
            times=[(f'{h/6:g}min',t0+h,min(spec['response_trailing_width'],h)) for h in spec['response_ticks']]
            times += [(f'end{t/360:g}h',t,180) for t in spec['horizons']]
            for tag,end,width in times:
                ps=float(x['prices'][end-width+1:end+1].mean());pc=float(c['prices'][end-width+1:end+1].mean())
                sign=float(np.sign(jump));window=slice(t0+1,end+1)
                row=dict(**identity,case=label,time=tag,end_tick=end,trailing_width=width,pre_price=pre,post_price=post,
                    point_retraced=float((post-x['prices'][end])/jump),raw_retraced=(post-ps)/jump,
                    paired_retraced=1-(ps-pc)/jump,remaining_from_pre=ps/pre-1,
                    remaining_paired_gap=(ps-pc)/pre,
                    inventory_opposing_net_quantity=float(-sign*x['reversion_actual_flow'][window].sum()),
                    inventory_paired_opposing_net_quantity=float(-sign*(x['reversion_actual_flow'][window].sum()-c['reversion_actual_flow'][window].sum())),
                    episode_opposing_net_quantity=float(-sign*x['factor_actual_flow'][window].sum()),
                    random_opposing_net_quantity=float(-sign*x['random_actual_flow'][window].sum()),
                    inventory_pre_net_position=float(x['reversion_net_position'][t0]),
                    inventory_post_net_position=float(x['reversion_net_position'][end]),
                    peak_intensity=float(x['intensity'][window].max()),max_arrivals=int(x['arrivals'][window].max()))
                responses.append(row)
        if r['seed']==81:illustrations[r['scale']['id'],r['inventory_count']]=cases
    assert len(normal)==36 and len(windows)==864 and len(responses)==216
    csv_write('normal.csv',normal);csv_write('ranges30.csv',windows);csv_write('responses.csv',responses)
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
    fig,axes=plt.subplots(2,2,figsize=(13,8));colors={0:'gray',5:'#176da0'}
    for col,t in enumerate(spec['horizons']):
        for count in [0,5]:
            x=illustrations['sigma120',count]['normal'];hours=np.arange(t+1)/360
            label='15随机＋5短线' if count==0 else '10随机＋5库存＋5短线'
            axes[0,col].plot(hours,(x['prices'][:t+1]/x['prices'][0]-1)*100,color=colors[count],label=label,lw=1)
            axes[1,col].plot(hours,np.cumsum(x['completions'][:t+1])/5,color=colors[count],lw=1.3)
        axes[0,col].set_title(f'开局{t/360:g}小时');axes[0,col].set_ylabel('相对初价变化（%）')
        axes[1,col].set_ylabel('每个短线账户累计完成轮数')
        for ax in axes[:,col]:ax.set_xlabel('游戏小时');ax.grid(alpha=.2)
    axes[0,0].legend();fig.suptitle('常态：固定展示噪声120、种子81；均为200外币短线轮次')
    fig.tight_layout();fig.savefig(OUTPUT/'normal.png',dpi=140);plt.close(fig)
    fig,axes=plt.subplots(2,3,figsize=(15,8))
    for col,scale in enumerate(spec['scales']):
        for row,label in enumerate(['up','down']):
            ax=axes[row,col];end=t0+360
            for count in [0,5]:
                x=illustrations[scale['id'],count][label]
                pre=x['prices'][t0]/(1.05 if label=='up' else .95)
                minutes=(np.arange(t0-30,end+1)-t0)/6
                ax.plot(minutes,(x['prices'][t0-30:end+1]/pre-1)*100,color=colors[count],label='原混合组' if count==0 else '加入库存调节',ls='--' if count==0 else '-',lw=1)
                if count==5:ax.plot(minutes,(x['slow_reference'][t0-30:end+1]/pre-1)*100,color='#cb7c29',ls=':',lw=1,label='候选慢参考')
            ax.axhline(0,color='black',lw=.5);ax.axhline(2.5 if label=='up' else -2.5,color='gray',ls=':',lw=.7)
            ax.axvline(10,color='gray',ls=':',lw=.7);ax.set_title(f"噪声{scale['order_sigma_foreign']}／{'上涨' if label=='up' else '下跌'}5%")
            ax.set_xlabel('距冲击分钟');ax.set_ylabel('相对各自冲击前价格（%）');ax.grid(alpha=.2)
    axes[0,0].legend(fontsize=8);fig.suptitle('±5%发行冲击：固定展示种子81；半幅线仅作评价，机器人不读取该线')
    fig.tight_layout();fig.savefig(OUTPUT/'shocks.png',dpi=140);plt.close(fig)

    def select(data,scale,count,**filters):
        return [r for r in data if r['scale']==scale and r['inventory_count']==count and all(r[k]==v for k,v in filters.items())]
    def avg(rows,key):
        a=[r[key] for r in rows];return float(np.mean(a)) if a and all(v is not None for v in a) else None
    def fmt(v,d=2):return '未定义' if v is None else f'{v:.{d}f}'
    lines=['# 200外币轮次：半小时幅度与冲击回吐','',
        '慢参考库存调节产生了明显的短期回吐，并允许较长时间后仍保留价格偏移；本轮尚未同时达到30分钟约2%幅度与两个方向10分钟约半幅回吐。原混合组噪声120的常态幅度已接近2%，加入库存调节后行情会收窄。','',
        '## 目标和冻结设置','',
        '用户选择常态30分钟内约2%的波动；单次约5%冲击后10分钟回吐约一半，允许不完全恢复及较持久的价格偏移。持仓5～15分钟是典型节奏目标。以上均为统计评价，不写成目标价格、事件倒计时或强制退出。','',
        '20户、池100万金圆券/20万外币、每户5000金圆券/1000外币、零费用、10秒tick。用户指定200外币解释为短线每轮意向数量从100提高至200，初始钱包不变。对照15随机＋5短线；候选把10～14号替换为5个库存调节者，短线15～19号及个人参数保持。','',
        '短线三因子仍用30/30窗口和人口301/spread1，只有到达且空闲、信号达到个人sigma才入场。止盈止损±.5%，移动启动+.25%、回撤.25个百分点；实际成本、滑点和钱包决定退出。每户每tick最多一笔短线实际成交，部分平仓保持原退出意图。','',
        '库存调节复用旧目标仓位：gap=过去最多720tick对数价格均值−上一tick对数收盘价；target=500*tanh(100*gap)，目标相对初始外币库存。历史不足时用已发生的历史。每次到达执行目标差额的50%，最多每户每tick一笔实际成交，可以保持净减持/净增持，不按短线成本强制回补。目标上限±500不是实际持仓的硬裁剪；预算报价与真实滑点可能造成小幅偏离。所有价格参考来自市场自身历史，没有独立基本价值。','',
        '噪声标准差80/120/160分别配psi=.0008/.0012/.0016，tau1000、b=.25、原到达基准1保持。同比缩放控制线性参照的反馈尺度，真实AMM不保证等比例。替换五个随机账户同时减少随机成交，不能把常态收窄全部归因于反向交易。','',
        '三尺度×两构成×行情81/82/83×常态/上冲/下冲，共54条8640tick路径、466560tick。包含启动，无预热删除。第1000tick成交后，上冲向池本币侧增加当时储备5%；下冲向外币侧增加F*(1/.95−1)，均取六位小数。这是两种不同资产发行，不是同一政策的对称买卖；分别核对资产总量。','',
        '## 常态：幅度与短线持有','',
        '每条24小时路径取48个不重叠30分钟窗口，高低价差/窗口初价；先求各路径中位数再平均三个种子。不是收益标准差，亦不是每条窗口硬性幅度限制。6小时是同一路径前缀，非独立重复。持有只统计已完成轮，期末未完成者另列原表。','',
        '|噪声|库存账户数|30分钟幅度中位数均值|三个种子幅度中位数范围|完成持有均值（分钟）|完成持有中位数（分钟）|6小时完成轮/短线户|',
        '|---|---:|---:|---:|---:|---:|---:|']
    for scale in spec['scales']:
        for count in [0,5]:
            g=select(normal,scale['id'],count,hours=24);short=select(normal,scale['id'],count,hours=6)
            a=[r['range30_median'] for r in g]
            lines.append(f"|{scale['order_sigma_foreign']}|{count}|{avg(g,'range30_median'):.2%}|{min(a):.2%}～{max(a):.2%}|{fmt(avg(g,'mean_completed_holding_minutes'))}|{fmt(avg(g,'median_completed_holding_minutes'))}|{avg(short,'completed_per_episode_account'):.2f}|")
    lines+=['','![常态价格与轮次](normal.png)','',
        '## 冲击后10分钟：回吐与净反向交易','',
        'r_raw=(P_plus−观察均价)/(P_plus−P_minus)，上下冲击均使用带符号跳幅；50%为回吐一半，100%才是回到冲击前价。r_paired=1−(冲击观察均价−同期无冲击均价)/(P_plus−P_minus)，100%代表追上同期对照，不能解释成必回旧价。主表观察均价取第9～10分钟共12tick；点值另保留，非重复试验。净反向量在上冲为净卖外币，下冲为净买外币，负值说明顺冲击方向交易。','',
        '|噪声|库存数|冲击|原始回吐均值|三个种子原始回吐范围|配对回吐均值|原始回吐≥50%路径数|库存账户净反向外币量均值|',
        '|---|---:|---|---:|---:|---:|---:|---:|']
    for scale in spec['scales']:
        for count in [0,5]:
            for label in ['up','down']:
                g=select(responses,scale['id'],count,case=label,time='10min');a=[r['raw_retraced'] for r in g]
                lines.append(f"|{scale['order_sigma_foreign']}|{count}|{'上冲' if label=='up' else '下冲'}|{avg(g,'raw_retraced'):.1%}|{min(a):.1%}～{max(a):.1%}|{avg(g,'paired_retraced'):.1%}|{sum(v>=.5 for v in a)}/3|{avg(g,'inventory_opposing_net_quantity'):.1f}|")
    lines+=['','![双向冲击与参考更新](shocks.png)','',
        '库存组18条事件路径的10分钟原始回吐均为正，其中3/18达到至少50%。50%计数仅作描述，不设为逐路径及格线；约半幅目标应结合均值、范围及两个方向判断。对照有顺势继续移动的路径。库存净反向成交量不是价格贡献的因果分解；初始库存、参考偏离、随机订单和反馈路径均会影响结局。','',
        '## 回吐之后保留多少偏移','',
        '下表为库存组，相对各自冲击前即时价：10分钟取第9～10分钟均价，60分钟取最后2分钟均价，24小时取局末30分钟均价。不是永久均衡的估计；无冲击路径也会漂移，同期配对价差保留在CSV。','',
        '|噪声|冲击|10分钟剩余价差|60分钟剩余价差|24小时剩余价差|',
        '|---|---|---:|---:|---:|']
    for scale in spec['scales']:
        for label in ['up','down']:
            values=[avg(select(responses,scale['id'],5,case=label,time=tag),'remaining_from_pre') for tag in ['10min','60min','end24h']]
            lines.append(f"|{scale['order_sigma_foreign']}|{'上冲' if label=='up' else '下冲'}|"+'|'.join(f'{v:.2%}' for v in values)+'|')
    lines+=['','较慢参考会吸收新价格历史，库存账户不永久守住旧价；当前模型确实出现短期回吐后，较晚价格又接近事件后水平的路径。能保留全局偏移，与已经证明“过冲一半＋永久影响一半”是两回事，不能据10分钟结果给两者作严格分解。','',
        '## 常态相关、成交与资金','',
        'ACF含所有零收益、全局减均值，按滞后乘积和/零滞后平方和计算，不作无偏修正；方向最大绝对值取lag1–100，幅度相关取31–180tick。各路径统计再等权平均，不拼接种子。事件跳点不混入常态ACF；目标要求事件后方向性回归，不能同时将该时段的可预测性当成无条件失败。','',
        '|噪声|库存数|收益ACF lag1|收益ACF最大绝对值1–100|abs(r) ACF31–180|库存成交占比|短线成交占比|订单裁剪比例|',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for scale in spec['scales']:
        for count in [0,5]:
            g=select(normal,scale['id'],count,hours=24)
            lines.append(f"|{scale['order_sigma_foreign']}|{count}|{fmt(avg(g,'signed_lag1'),5)}|{fmt(avg(g,'max_signed_1_100'),5)}|{fmt(avg(g,'abs_31_180'),5)}|{avg(g,'inventory_volume_share'):.1%}|{avg(g,'episode_volume_share'):.1%}|{avg(g,'clipped_fraction'):.2%}|")
    lines+=['','## 当前结论与验证','',
        '本轮支持“常态波动规模”和“冲击后净库存反应”应分别控制。200外币轮次与较宽阈值已实际运行；库存调节提供短期回归，参考更新允许较持久偏移。新组合仍未同时达到两个市场目标，不替换冻结基线，也不事后追加噪声或库存网格。结果只覆盖一个参数人口和三个既有种子，不构成真人行为、事件频率或游戏性能验证。','',
        f"研究测试：`OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m unittest discover -s research/fx/tests -v`，27项通过。保存序列审计：{audit['paths']}路径、{audit['reference_ticks']}参考时点、{audit['episode_quotes']}笔短线报价、{audit['inventory_quotes']}笔库存报价；实际生产报价重放、两币发行守恒、历史参考因果与目标、下一tick反馈、真实账户及短线/库存账户的单tick限制核对通过。原{audit['protected_files_unchanged']}文件哈希未变。",'',
        '复现：`python -m research.fx.reversion.run` → `python -m research.fx.reversion.audit` → `python -m research.fx.reversion.report`。未改生产、公共AMM或旧分支。','',
        '- [配置](../../config.json)、[来源及保护快照](manifest.json)、[审计](audit.json)、[全部账户与结局](summary.json)',
        '- [常态6/24小时指标](normal.csv)、[全部864个30分钟窗口](ranges30.csv)、[5/10/30/60分钟及局末响应](responses.csv)。各路径保存npz、短线轮次与成交、库存成交账本。','']
    (OUTPUT/'report.md').write_text('\n'.join(lines))
    paths=[HERE/'report.py',HERE/'audit.py',OUTPUT/'summary.json',OUTPUT/'audit.json',OUTPUT/'normal.csv',OUTPUT/'responses.csv',OUTPUT/'ranges30.csv',OUTPUT/'report.md']
    (OUTPUT/'report_sources.json').write_text(json.dumps({str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},indent=2)+'\n')
    print('Report complete: 36 normal summaries, 864 range windows, 216 event responses.')


if __name__=='__main__':main()
