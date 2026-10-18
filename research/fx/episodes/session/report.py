"""Read saved paths at actual game horizons; never tune or rerun the model."""
import csv
import hashlib
import json
from decimal import Decimal

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from research.fx.common.diagnostics import acf
from research.fx.episodes.run import longest_empty
from research.fx.factor_activity.run import ROOT, clean_json
from .run import HERE, OUTPUT


def write_csv(name, rows):
    with (OUTPUT/name).open('w') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)


def main():
    manifest=json.loads((OUTPUT/'manifest.json').read_text());spec=manifest['spec']
    records=json.loads((OUTPUT/'summary.json').read_text())
    assert len(records)==30 and all(r['status']=='complete' for r in records)
    for p,h in {**manifest['source_sha256'],**manifest['protected_sha256']}.items():
        assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h,p
    rows=[];responses=[];trace={};ledger_checks=0
    for r in records:
        count=r['random_count'];n=20-count;directory=OUTPUT/r['name']
        cases={}
        for label in ['control','shock']:
            with np.load(directory/f'{label}.npz') as z: x={k:z[k] for k in z.files}
            cases[label]=x
            episodes=json.loads((directory/f'{label}-episodes.json').read_text())
            fills=[json.loads(s) for s in (directory/f'{label}-fills.jsonl').read_text().splitlines()]
            for t in spec['horizons']:
                opened=[e for e in episodes if e['entry_tick']<=t]
                done=[e for e in opened if e['close_tick'] is not None and e['close_tick']<=t]
                done_ids={e['id'] for e in done}
                pending=[e for e in opened if e['id'] not in done_ids]
                per=[sum(e['account']==str(i) for e in done) for i in range(count,20)]
                holdings=[(e['close_tick']-e['entry_tick'])*10/60 for e in done]
                ages=[(t-e['entry_tick'])*10/60 for e in pending]
                fs=[f for f in fills if f['tick']<=t]
                assert len(done)==int(x['completions'][1:t+1].sum())
                assert len(opened)==int(x['entries'][1:t+1].sum())
                assert abs(float(sum((Decimal(f['realized_profit']) for f in fs),Decimal(0)))-x['realized_pnl'][t])<1e-8
                by_episode={}
                for f in fs:by_episode.setdefault(f['episode_id'],[]).append(f)
                for e in opened:
                    ef=by_episode[e['id']]
                    residual=Decimal(e['entry_quantity'])-sum((Decimal(f['quantity']) for f in ef if f['action']=='close'),Decimal(0))
                    assert (residual==0)==(e['id'] in done_ids)
                    ledger_checks+=1
                q=x['executed_abs_quantity'][1:t+1].sum()
                active=np.flatnonzero(x['trades'][1:t+1])
                a=x['returns'][1:t+1]
                curves={k:acf(v,360) for k,v in [('signed',a),('abs',abs(a)),('sq',a*a)]} if label=='control' else {}
                row=dict(cell=r['cell']['id'],label=r['cell']['label'],random_count=count,seed=r['seed'],
                         case=label,hours=t*10/3600,completed=len(done),completed_per_bot=len(done)/n,
                         zero_completed_accounts=sum(v==0 for v in per),unfinished=len(pending),
                         mean_completed_holding_minutes=float(np.mean(holdings)) if holdings else None,
                         median_completed_holding_minutes=float(np.median(holdings)) if holdings else None,
                         p90_completed_holding_minutes=float(np.quantile(holdings,.9)) if holdings else None,
                         max_pending_age_minutes=max(ages) if ages else None,
                         last_trade_tick=int(active[-1]+1) if len(active) else None,
                         longest_no_trade_minutes=longest_empty(x['trades'][1:t+1])/6,
                         behavior_volume_share=float(x['factor_executed_abs_quantity'][1:t+1].sum()/q) if q else None,
                         realized_episode_pnl=float(x['realized_pnl'][t]),
                         arrivals=int(x['arrivals'][1:t+1].sum()),max_arrivals=int(x['arrivals'][1:t+1].max()),
                         peak_intensity=float(x['intensity'][1:t+1].max()),
                         clipped_per_attempt=float(x['clipped_orders'][1:t+1].sum()/x['order_attempts'][1:t+1].sum()),
                         wallet_gold_min=float(x['wallet_gold_min'][:t+1].min()),wallet_foreign_min=float(x['wallet_foreign_min'][:t+1].min()),
                         signed_lag1=float(curves['signed'][1]) if curves else None,
                         max_signed_1_100=float(np.max(abs(curves['signed'][1:101]))) if curves else None,
                         abs_31_180=float(curves['abs'][31:181].mean()) if curves else None,
                         sq_31_180=float(curves['sq'][31:181].mean()) if curves else None)
                rows.append(clean_json(row))
            if r['seed']==81:trace[r['cell']['id'],count,label]=x
        t0=spec['shock_tick'];pre=float(r['shock_summary']['issue']['pre_price']);post=float(r['shock_summary']['issue']['post_price'])
        assert all(np.array_equal(cases['shock'][k][:t0],cases['control'][k][:t0]) for k in cases['control'])
        for t in spec['horizons']:
            x,c=cases['shock'],cases['control'];w=spec['terminal_window']
            ps=x['prices'][t-w+1:t+1].mean();pc=c['prices'][t-w+1:t+1].mean()
            hits=np.flatnonzero(x['prices'][t0+1:t+1]<=pre)
            responses.append(dict(cell=r['cell']['id'],label=r['cell']['label'],random_count=count,seed=r['seed'],hours=t/360,
                terminal_price_change=float(ps/pre-1),raw_retraced=float((post-ps)/(post-pre)),
                paired_retraced=float(1-(ps-pc)/(post-pre)),first_full_hit_ticks=int(hits[0]+1) if len(hits) else None,
                post_peak_intensity=float(x['intensity'][t0+1:t+1].max()),max_post_arrivals=int(x['arrivals'][t0+1:t+1].max()),
                behavior_net_sold=float(-x['factor_actual_flow'][t0+1:t+1].sum())))
    assert len(rows)==120 and len(responses)==60
    write_csv('sessions.csv',rows);write_csv('responses.csv',responses)
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
    colors=plt.get_cmap('tab10').colors
    fig,axes=plt.subplots(2,2,figsize=(13,8))
    for j,t in enumerate(spec['horizons']):
        for i,cell in enumerate(spec['cells']):
            x=trace[cell['id'],15,'control'];hours=np.arange(t+1)/360
            axes[0,j].plot(hours,(x['prices'][:t+1]/x['prices'][0]-1)*100,color=colors[i],label=cell['label'],lw=1)
            axes[1,j].plot(hours,np.cumsum(x['completions'][:t+1])/5,color=colors[i],label=cell['label'],lw=1.4)
        axes[0,j].set_title(f'开局至{t/360:g}小时');axes[0,j].set_ylabel('相对初价变化（%）')
        axes[1,j].set_ylabel('每个行为账户累计完成轮数')
        for ax in axes[:,j]:ax.set_xlabel('游戏小时');ax.grid(alpha=.2)
    axes[0,0].legend(fontsize=8);fig.suptitle('15随机＋5行为：固定展示种子81；6小时是24小时路径的前缀')
    fig.tight_layout();fig.savefig(OUTPUT/'normal.png',dpi=140);plt.close(fig)
    fig,axes=plt.subplots(2,2,figsize=(13,8))
    for j,t in enumerate(spec['horizons']):
        for i,cell in enumerate(spec['cells']):
            x=trace[cell['id'],15,'shock']
            record=next(r for r in records if r['seed']==81 and r['random_count']==15 and r['cell']['id']==cell['id'])
            pre=float(record['shock_summary']['issue']['pre_price'])
            hours=np.arange(t0-30,t+1)/360
            axes[0,j].plot(hours,(x['prices'][t0-30:t+1]/pre-1)*100,color=colors[i],label=cell['label'],lw=1)
            axes[1,j].plot(hours,x['intensity'][t0-30:t+1],color=colors[i],lw=1)
        axes[0,j].set_title(f'注资后观察至第{t/360:g}小时');axes[0,j].set_ylabel('相对各自注资前价格（%）')
        axes[1,j].set_ylabel('预定到达强度（次/tick）')
        for ax in axes[:,j]:ax.axvline(t0/360,color='gray',ls=':');ax.set_xlabel('游戏小时');ax.grid(alpha=.2)
    axes[0,0].legend(fontsize=8);fig.suptitle('10万注资：混合组、种子81；表格另列配对对照校正，图示为实际注资路径')
    fig.tight_layout();fig.savefig(OUTPUT/'shock.png',dpi=140);plt.close(fig)

    def selected(data,cell,count,hours,case=None):
        return [r for r in data if r['cell']==cell and r['random_count']==count and r['hours']==hours and (case is None or r['case']==case)]
    def avg(group,key):
        a=[r[key] for r in group]
        return float(np.mean(a)) if a and all(v is not None for v in a) else None
    def fmt(v,d=2):return '未定义' if v is None else f'{v:.{d}f}'
    lines=['# 10秒tick下的6小时与24小时节奏探索','',
        '退出阈值减半明显加快混合组交易轮；只缩短热度记忆改善较小，且放大注资后的瞬时活动。全行为组在24小时结束前仍全部停止成交。参数候选尚未替换旧版本。','',
        '## 冻结设置与口径','',
        '沿用现有模拟器，20户、池100万金圆券/20万外币、户均5000/1000、零费用、每轮100外币；个人三因子30/30窗口与人口301/spread1固定，到达规则与反馈b=.25保持。仅使用原移动退出版。五组参数、范围与限制见[实验说明](../README.md)和[配置](../config.json)。原版止盈止损±1%，移动启动+.5%、回撤.5个百分点；减半/四分之一同时同比缩放这四项。tau1000/180分别约2小时47分钟/30分钟，只代表z的衰减参数。','',
        '行情81/82/83，两个人口构成、五组参数，共30对/60路径、518400tick。每条运行8640tick，在2160/8640截取；含启动阶段，不丢5000tick预热。6小时与24小时来自同一路径，不是独立重复。到达时才观察与退出；最多每户每tick一笔真实成交，不追加最长持仓或补钱。持有时长仅统计已完成轮，未完成单独保留。','',
        '表中先逐路径计算再等权平均三个行情种子，持有时长为三个路径均值的平均。未作正式显著性检验或未见种子验证，不能借短样本宣称长记忆、不可预测性或已适合玩家竞争。','',
        '## 混合组常态：每个行为账户的节奏','',
        '|参数|6小时完成轮数/户|24小时完成轮数/户|24小时完成轮平均持有分钟|24小时行为成交占比|',
        '|---|---:|---:|---:|---:|']
    for cell in spec['cells']:
        a=selected(rows,cell['id'],15,6,'control');b=selected(rows,cell['id'],15,24,'control')
        lines.append(f"|{cell['label']}|{avg(a,'completed_per_bot'):.2f}|{avg(b,'completed_per_bot'):.2f}|{fmt(avg(b,'mean_completed_holding_minutes'))}|{avg(b,'behavior_volume_share'):.1%}|")
    lines+=['','![混合组价格与完成轮数](normal.png)','',
        '阈值减半把典型已完成持仓缩到约半小时至三刻钟，四分之一约15分钟。更快不等于利润更好或更像真实人类；行为成交占比上升同时改变市场反馈，不能把效果线性外推到更低阈值。','',
        '## 全行为组：加快退出仍未消除停止成交','',
        '|参数|6小时完成轮数/户|24小时完成轮数/户|三个种子最后成交时间范围（开局后小时）|',
        '|---|---:|---:|---:|']
    for cell in spec['cells']:
        a=selected(rows,cell['id'],0,6,'control');b=selected(rows,cell['id'],0,24,'control')
        last=[r['last_trade_tick']/360 for r in b]
        lines.append(f"|{cell['label']}|{avg(a,'completed_per_bot'):.2f}|{avg(b,'completed_per_bot'):.2f}|{min(last):.2f}～{max(last):.2f}|")
    lines+=['','四分之一阈值让部分全行为路径维持更久，但三条都在24小时结束前停止成交；不能把短暂循环当成解决了等待结构。混合组保留随机背景，解释时需保留其成交占比。','',
        '## 注资：本局结束前的反应','',
        '第1000tick（2小时46分40秒）常规成交后向池本币侧加10万；只看本局剩余时段。主结局为局末180tick/30分钟均价，相对实际注资前即时价。原始回落=(注资后即时价−末段均价)/初始跳幅；配对回落=1−(注资末段−同期无注资末段)/初始跳幅。前者100%才是回旧价，后者100%是追上同期对照，两者不能混用。','',
        '|混合组参数|6小时末仍高于旧价|24小时末仍高于旧价|24小时配对回落|最大预定强度|最大单tick到达数|',
        '|---|---:|---:|---:|---:|---:|']
    for cell in spec['cells']:
        a=selected(responses,cell['id'],15,6);b=selected(responses,cell['id'],15,24)
        lines.append(f"|{cell['label']}|{avg(a,'terminal_price_change'):.2%}|{avg(b,'terminal_price_change'):.2%}|{avg(b,'paired_retraced'):.1%}|{max(r['post_peak_intensity'] for r in b):.1f}|{max(r['max_post_arrivals'] for r in b)}|")
    lines+=['','![注资价格和活动响应](shock.png)','',
        '较短tau同时增加sqrt(1−exp(−2/tau))的冲击系数。同一次大跳价会更强地抬高下一tick的z和Lambda；这解释了缩短记忆时更高的活动峰值，未给强度添加上限。实际到达仍为泊松数，预定强度与实际次数分开。没有测定游戏运行时延，不能据峰值断言会卡顿。买入轮上涨止盈卖出、减持轮上涨止损买回的双向作用仍保留。','',
        '## 常态短样本市场诊断','',
        '以下为24小时混合组。ACF含零收益、全局去均值后用滞后乘积和/零滞后平方和，不作无偏修正；方向最大值只取lag1–100。幅度/平方区间31–180tick约5至30分钟，是本轮局长口径，不能与旧报告101–300/301–1000数值直接比较。常数序列记未定义。','',
        '|参数|收益ACF lag1|收益ACF最大绝对值 lag1–100|abs(r) ACF 31–180|r² ACF 31–180|',
        '|---|---:|---:|---:|---:|']
    for cell in spec['cells']:
        g=selected(rows,cell['id'],15,24,'control')
        lines.append('|'+cell['label']+'|'+'|'.join(fmt(avg(g,k),5) for k in ['signed_lag1','max_signed_1_100','abs_31_180','sq_31_180'])+'|')
    lines+=['','## 范围与复现','',
        '先保留tau1000、退出减半作为较快节奏候选；四分之一是更短线的比较组。这个取舍针对节奏，不宣称已验证真人参与或最佳参数。用户随后要求继续缩短持有，后续更短线试验单独保存，不覆盖本轮结果。','',
        '运行与检查：`OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m research.fx.episodes.session.run`；`python -m research.fx.episodes.session.report`。已有研究测试`python -m unittest discover -s research/fx/tests -v`，24项通过。没有修改生产或公共执行代码，没有新增永久测试。','',
        f'60路径已核对注资前逐字段一致、逐tick资产守恒、基准成本与现金关系、单tick成交限制及钱包非负；6条原参数常态路径与上一轮保存序列前缀逐字段一致。报告另核对{ledger_checks}个截断交易轮的入场、完成、残量和已实现现金损益，校验来源与{len(manifest["protected_sha256"])}个既有文件哈希。没有把路径终点之后的平仓纳入6小时统计。','',
        '- [逐路径6/24小时行为及市场指标](sessions.csv)、[注资配对结果](responses.csv)、[全部账户终态](summary.json)、[配置与来源哈希](manifest.json)。各路径目录保存全部序列及行为账本。','']
    (OUTPUT/'report.md').write_text('\n'.join(lines))
    paths=[HERE/'report.py',OUTPUT/'summary.json',OUTPUT/'manifest.json',OUTPUT/'sessions.csv',OUTPUT/'responses.csv',OUTPUT/'report.md']
    (OUTPUT/'report_sources.json').write_text(json.dumps({str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},indent=2)+'\n')
    print('Report complete: 120 horizon rows, 60 response rows;',ledger_checks,'ledger-prefix checks.')


if __name__=='__main__':main()
