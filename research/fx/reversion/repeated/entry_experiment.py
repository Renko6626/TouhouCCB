"""Four predeclared entry policies on three frozen 40-account no-shock seeds."""
import argparse
import csv
import hashlib
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import ROOT, clean_json
from .entry_model import simulate
from .population import OUTPUT as BASE
from .sensitivity import measure


HERE=Path(__file__).parent
OUTPUT=HERE/'results/entry-policies'
LABELS=dict(original='原版',weak_trend='趋势权重×¼',confirmed_trend='成交确认',random_direction='随机方向')


def run_case(seed,policy,cfg,profiles,b,settings):
    cfg={**cfg,'seeds':[seed]}
    x,s,episodes,fills,inventory_fills,decisions=simulate(cfg,profiles,10,b,seed,{},policy,settings)
    assert s['events']==[] and s['issued_gold']=='0' and s['issued_foreign']=='0'
    assert all(np.isfinite(a).all() for a in x.values())
    assert x['account_gold'].min()>=0 and x['account_foreign'].min()>=0
    np.testing.assert_allclose(x['gold']+x['account_gold'].sum(axis=1),1200000,atol=1e-8,rtol=0)
    np.testing.assert_allclose(x['pool_foreign']+x['account_foreign'].sum(axis=1),240000,atol=1e-8,rtol=0)
    np.testing.assert_allclose(x['account_wealth'],x['account_gold']+x['account_foreign']*x['prices'][:,None],atol=1e-8,rtol=0)
    np.testing.assert_array_equal(x['account_trades'].sum(axis=1),x['trades'])
    if seed==81 and policy=='original':
        with np.load(BASE/'n40/control.npz') as old:
            for key in old.files:np.testing.assert_array_equal(x[key],old[key],err_msg=key)
        prior=json.loads((BASE/'n40/summary.json').read_text())['control']
        assert s==prior
    directory=OUTPUT/f'{policy}-seed{seed}';directory.mkdir(parents=True,exist_ok=True)
    save_trace(directory,'normal',x,episodes,fills)
    (directory/'summary.json').write_text(json.dumps(clean_json(s),indent=2)+'\n')
    for name,records in [('inventory',inventory_fills),('decisions',decisions)]:
        (directory/f'{name}.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
    d,_=measure(x,s,[])
    start=30;done=[e for e in episodes if e['close_tick'] is not None]
    short=x['account_wealth'][:,start:];benchmark=5000+1000*x['prices'][-1]
    opens={r['episode_id']:r for r in decisions if r['action']=='open'}
    assert len(opens)==s['behavior']['opened']
    gross=sum(float(e['realized_profit']) for e in done)
    assert abs(gross-float(s['behavior']['realized_pnl']))<1e-7
    breakdown={}
    for dominant in ['trend','reversion','noise']:
        selected=[e for e in done if opens[e['id']]['dominant']==dominant]
        breakdown[dominant]=dict(completed=len(selected),pnl=sum(float(e['realized_profit']) for e in selected),
            wins=sum(float(e['realized_profit'])>0 for e in selected))
    d.update(seed=seed,policy=policy,short_accounts=10,short_mean_wealth=float(short[-1].mean()),
        short_mean_wealth_change=float(short[-1].mean()/10000-1),
        short_mean_vs_hold=float(short[-1].mean()-benchmark),
        short_realized_per_account=gross/10,short_completed=len(done),short_completed_per_account=len(done)/10,
        short_pnl_per_completed=gross/len(done) if done else None,
        short_win_fraction=float(np.mean([float(e['realized_profit'])>0 for e in done])) if done else None,
        short_max_drawdown=float((1-short/np.maximum.accumulate(short,axis=0)).max()),
        candidates=len(decisions),vetoes=sum(r['action']=='veto' for r in decisions),
        trend_candidates=sum(r['dominant']=='trend' for r in decisions),
        trend_passed=sum(r['dominant']=='trend' and r['accepted'] for r in decisions),
        completed_by_dominant=breakdown)
    print(f"Complete {policy} seed={seed}: short realized/account={d['short_realized_per_account']:+.2f}, vs hold={d['short_mean_vs_hold']:+.2f}, wins={d['short_win_fraction']:.1%}",flush=True)
    return d


def run():
    settings=json.loads((HERE/'entry_config.json').read_text())
    old=json.loads((BASE/'manifest.json').read_text())
    cfg={**old['cases']['40']['config'],'ticks':settings['ticks']}
    profiles=old['cases']['40']['profiles'];b=old['base_spec']['b']
    protected={**old['protected_sha256'],**old['source_sha256']}
    for p in [BASE/'manifest.json',BASE/'n40/control.npz',BASE/'n40/summary.json']:
        protected[str(p.relative_to(ROOT))]=hashlib.sha256(p.read_bytes()).hexdigest()
    for path,digest in protected.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    OUTPUT.mkdir(exist_ok=True)
    manifest=dict(settings=settings,config=cfg,profiles=profiles,b=b,protected_sha256=protected,
        source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [HERE/'entry_config.json',HERE/'entry_model.py',HERE/'entry_experiment.py']})
    (OUTPUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    results=[]
    with ProcessPoolExecutor(max_workers=3) as pool:
        jobs=[pool.submit(run_case,seed,policy,cfg,profiles,b,settings)
              for seed in settings['market_seeds'] for policy in settings['policies']]
        for future in as_completed(jobs):
            results.append(future.result())
            (OUTPUT/'metrics.json').write_text(json.dumps(results,indent=2,allow_nan=False)+'\n')
    results.sort(key=lambda r:(r['seed'],settings['policies'].index(r['policy'])))
    (OUTPUT/'metrics.json').write_text(json.dumps(results,indent=2,allow_nan=False)+'\n')
    for path,digest in {**protected,**manifest['source_sha256']}.items():
        assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    (OUTPUT/'audit.json').write_text(json.dumps(dict(paths=len(results),wallet_marks=12*10001*40,
        no_external_issuance=True,both_assets_conserved=True,wallets_nonnegative=True,
        per_account_marks_and_fills_verified=True,original_seed81_full_trace_exact=True,
        protected_files_unchanged=len(protected)),indent=2)+'\n')


def report():
    settings=json.loads((OUTPUT/'manifest.json').read_text())['settings']
    results=json.loads((OUTPUT/'metrics.json').read_text())
    rows=[]
    keys=['short_realized_per_account','short_mean_vs_hold','short_pnl_per_completed','short_win_fraction',
          'short_completed_per_account','holding_median_minutes','short_mean_wealth','vetoes','trend_candidates','trend_passed']
    for r in results:
        rows.append(dict(seed=r['seed'],policy=r['policy'],**{k:r[k] for k in keys},
            range30=r['range30_all']['median'],signed_acf1=r['returns']['acf']['signed']['1_1']))
    with (OUTPUT/'comparison.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    aggregate={policy:{key:dict(mean=float(np.mean([r[key] for r in rows if r['policy']==policy])),
        min=float(min(r[key] for r in rows if r['policy']==policy)),max=float(max(r[key] for r in rows if r['policy']==policy)))
        for key in [*keys,'range30','signed_acf1']} for policy in settings['policies']}
    (OUTPUT/'aggregate.json').write_text(json.dumps(aggregate,indent=2,allow_nan=False)+'\n')
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False})
    fig,axes=plt.subplots(1,2,figsize=(13,5))
    colors=['#718393','#2b8c70','#286c9f','#b87835'];positions=np.arange(4)
    for ax,key,title in zip(axes,['short_realized_per_account','short_mean_vs_hold'],
        ['每位短线账户的已实现交易盈亏','期末净值相对同路径持有初始钱包的差额']):
        for i,policy in enumerate(settings['policies']):
            values=[r[key] for r in rows if r['policy']==policy]
            ax.bar(i,np.mean(values),color=colors[i],alpha=.6,width=.55)
            ax.scatter(i+np.linspace(-.12,.12,len(values)),values,color=colors[i],edgecolor='black',s=35,zorder=3)
        ax.set(xticks=positions,xticklabels=[LABELS[p] for p in settings['policies']],ylabel='金圆券／每户',title=title)
        ax.axhline(0,color='black',lw=.6);ax.grid(axis='y',alpha=.2)
    fig.suptitle('40人无冲击，三个种子：柱为种子均值，点为各路径')
    fig.tight_layout();fig.savefig(OUTPUT/'comparison.png',dpi=150);plt.close(fig)
    lines=['# 短线入场规则四组对照','','## 固定设置与假设','','40人=20随机+10库存+10短线，10000tick×10秒约27小时47分钟，无外部冲击、手续费、补钱、重置、期末强制退出。市场种子预先固定81/82/83，人口参数为原301分布按角色复制；每户5000金圆券+1000外币，池100万/20万。psi=.0008、tau1000、总强度2×(.75+.25z²)。','','四组：原版三因子；仅趋势项系数×.25后重新求和和判断入场；原版信号中绝对贡献最大的项为趋势时才要求成交确认；随机方向保留原版信号幅度与abs(signal)>=个人sigma资格，在每次合格空闲到达独立随机买卖方向。随机方向使用独立SeedSequence([市场种子,账户ID,20261015,511])，不消耗原预测随机流。所有组仍为每轮200外币、相同钱包约束、到达查看与退出规则。','','成交确认只看截至上一tick的过去30tick：排除自己，按每个其他账户的净外币变动统计方向，至少3个同向账户，且同向占具有非零净方向账户至少2/3；前15tick和后15tick的其他账户合计净流入都需同向。净额绝对值≤.000001视为零；不是逐笔买入人数统计，也不要求这些账户拥有独立信息。前30tick历史不全时趋势入场不通过，其他主导项不受门槛限制。买卖完全对称。','','每组分别改变内生市场，不能视为在同一外生价格上逐笔回放。相同种子只固定初始化与随机流规则，成交数量、钱包、到达路径会分叉。这里的对照支持整个入场方案效果，不能单独分离由市场路径变化造成的影响。','','## 三个种子等权均值','','|指标|原版|趋势权重×¼|成交确认|随机方向|','|---|---:|---:|---:|---:|']
    fields=[('每位短线已实现盈亏','short_realized_per_account','+.2f'),
        ('每位短线相对初始钱包持有差额','short_mean_vs_hold','+.2f'),
        ('每个完成轮次平均盈亏','short_pnl_per_completed','+.2f'),
        ('完成轮次盈利占比','short_win_fraction','.1%'),('每位短线完成轮次','short_completed_per_account','.1f'),
        ('短线持仓中位数/分钟','holding_median_minutes','.2f'),('短线每户期末净值','short_mean_wealth','.2f'),
        ('30分钟市场波幅中位数','range30','.2%'),('市场收益ACF lag1','signed_acf1','.5f')]
    for label,key,fmt in fields:
        lines.append('|'+label+'|'+'|'.join(format(aggregate[p][key]['mean'],fmt) for p in settings['policies'])+'|')
    lines+=['','均值逐路径先计算，再按种子等权；胜率不是把种子交易拼接后算。已实现盈亏只含已实际平仓部分；期末仍有未平轮次，净值包含它们的浮动盈亏。持有基准H=5000+1000×本方案自己的期末价；相对差额仍含浮盈亏，不是同一价格路径上的策略净效应。30分钟波幅=(高−低)/期初价，55个完整非重叠180tick窗口，尾部不足丢弃。','','## 每个种子的短线结果','','|种子|方案|每户已实现盈亏|相对持有差额|盈利轮次占比|每户完成轮次|','|---|---|---:|---:|---:|---:|']
    for r in rows:lines.append(f"|{r['seed']}|{LABELS[r['policy']]}|{r['short_realized_per_account']:+.2f}|{r['short_mean_vs_hold']:+.2f}|{r['short_win_fraction']:.1%}|{r['short_completed_per_account']:.1f}|")
    lines+=['','## 成交确认与交易选择','','|种子|趋势项主导合格候选|通过确认|被否决|已完成确认趋势轮次|这些轮次合计盈亏|','|---|---:|---:|---:|---:|---:|']
    for r in results:
        if r['policy']=='confirmed_trend':
            c=r['completed_by_dominant']['trend'];lines.append(f"|{r['seed']}|{r['trend_candidates']}|{r['trend_passed']}|{r['vetoes']}|{c['completed']}|{c['pnl']:+.2f}|")
    lines+=['','候选按空闲且信号达标的到达事件计，可含同一账户不同时间的重复候选；通过不必然成交。门槛更少交易可能减少总损失，因此同时报告每轮盈亏、胜率、交易次数与确认趋势子集，不能把少做等同于识别到了有利趋势。','','![三种子对照](comparison.png)','','## 验证与复现','','原版seed81全部10001点序列及账户终态摘要与已保存40人无冲击路径完全一致。12条路径逐tick核对两币守恒、钱包非负、个人净值、账户成交计数；已完成盈亏与账本摘要一致。确认函数已有排除自己、当tick/未来数据及前后段方向测试，退出仍调用原BehaviorTrader。源哈希与固定参数见manifest.json，验证见audit.json。','','本轮只测40人、无冲击和三个种子，没有证明新门槛在大事件、80人或玩家参与场景中的表现；确认阈值未搜索或事后调整。','','运行：`python -m research.fx.reversion.repeated.entry_experiment`；仅重建报告加`--report-only`。','','[种子级CSV](comparison.csv)、[全部指标与子集](metrics.json)、[种子间均值与范围](aggregate.json)、[设置及来源](manifest.json)。每条路径目录包含normal.npz个人钱包矩阵、短线轮次/成交、库存成交及入场候选decisions.jsonl。','']
    (OUTPUT/'report.md').write_text('\n'.join(lines))
    print('Report complete:',OUTPUT/'report.md',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--report-only',action='store_true');args=parser.parse_args()
    if not args.report_only:run()
    report()
