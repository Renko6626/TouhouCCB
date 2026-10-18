"""Three modest exit-parameter candidates; existing 20-account baseline stays frozen."""
import argparse
import csv
import hashlib
import json
from concurrent.futures import ProcessPoolExecutor,as_completed
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import ROOT,clean_json
from .entry_model import simulate
from .population_time_metrics import metrics


HERE=Path(__file__).parent
BASE=HERE/'results/population-5s'
OUTPUT=HERE/'results/short-exit-tuning'
LABELS=dict(baseline='原参数',profit04='止盈0.4%',profit_trail04='止盈0.4%＋移动0.2%',symmetric04='止盈止损0.4%＋移动0.2%')


def describe_run(x,s,episodes,decisions,settings):
    d=metrics(x,s,episodes,5);done=[e for e in episodes if e['close_tick'] is not None]
    minutes=d['hours']*60;holding=[(e['close_tick']-e['entry_tick'])/12 for e in done]
    exits={reason:dict(count=sum(e['closing_reason']==reason for e in done),
        pnl=sum(float(e['realized_profit']) for e in done if e['closing_reason']==reason))
        for reason in ['take_profit','stop_loss','trailing']}
    assert abs(sum(e['pnl'] for e in exits.values())-float(s['behavior']['realized_pnl']))<1e-7
    d.update(short_completed=len(done),short_completed_per_account=len(done)/5,
        holding_p90_minutes=float(np.quantile(holding,.9)),
        short_pnl_per_completed=float(s['behavior']['realized_pnl'])/len(done),
        stop_loss_fraction=exits['stop_loss']['count']/len(done),
        stop_loss_per_account_hour=exits['stop_loss']['count']/5/d['hours'],
        trades_per_minute=d['trades']/minutes,short_trades_per_minute=float(x['factor_trades'][1:].sum()/minutes),
        exits=exits,unfinished_max_age_minutes=max(((10000-e['entry_tick'])/12 for e in episodes if e['close_tick'] is None),default=None))
    return d


def run_case(variant,scenario,seed,cfg,profiles,b,settings,schedule):
    cfg={**cfg,'seeds':[seed]}
    x,s,episodes,fills,inventory,decisions=simulate(cfg,profiles,5,b,seed,schedule if scenario=='shock' else {},
        'weak_trend',settings)
    assert all(np.isfinite(a).all() for a in x.values())
    assert x['account_gold'].min()>=0 and x['account_foreign'].min()>=0
    np.testing.assert_allclose(x['account_wealth'],x['account_gold']+x['account_foreign']*x['prices'][:,None],atol=1e-8,rtol=0)
    np.testing.assert_allclose(x['gold']+x['account_gold'].sum(axis=1),1100000+np.cumsum(x['injection_gold']),atol=1e-8,rtol=0)
    np.testing.assert_allclose(x['pool_foreign']+x['account_foreign'].sum(axis=1),220000+np.cumsum(x['injection_foreign']),atol=1e-8,rtol=0)
    np.testing.assert_array_equal(x['account_trades'].sum(axis=1),x['trades'])
    if scenario=='normal':assert not s['events']
    else:
        assert len(s['events'])==10 and s['events'][-1]['tick']==10000
        for e in s['events']:assert abs(float(e['post_price'])/float(e['pre_price'])-1-schedule[e['tick']])<1e-11
    directory=OUTPUT/f'{variant}-{scenario}-seed{seed}';directory.mkdir(parents=True,exist_ok=True)
    save_trace(directory,'trace',x,episodes,fills)
    (directory/'summary.json').write_text(json.dumps(clean_json(s),indent=2)+'\n')
    for name,records in [('inventory',inventory),('decisions',decisions)]:
        (directory/f'{name}.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
    d=describe_run(x,s,episodes,decisions,settings)
    d.update(variant=variant,scenario=scenario,seed=seed,data_prefix=str((directory/'trace').relative_to(ROOT)))
    print(f"Complete {variant} {scenario} seed={seed}: hold={d['holding_median_minutes']:.2f}min pnl/account={d['short_realized_per_account']:+.2f} stopshare={d['stop_loss_fraction']:.1%}",flush=True)
    return d


def run():
    settings=json.loads((HERE/'short_exit_config.json').read_text());old=json.loads((BASE/'manifest.json').read_text())
    protected={**old['protected_sha256'],**old['source_sha256']}
    prior=[r for r in json.loads((BASE/'metrics.json').read_text()) if r['accounts']==20]
    for r in prior:
        prefix=ROOT/r['data_prefix']
        for path in [Path(str(prefix)+'.npz'),Path(str(prefix)+'-episodes.json'),prefix.parent/'summary.json']:
            protected[str(path.relative_to(ROOT))]=hashlib.sha256(path.read_bytes()).hexdigest()
    for path in [BASE/'manifest.json',BASE/'metrics.json']:
        protected[str(path.relative_to(ROOT))]=hashlib.sha256(path.read_bytes()).hexdigest()
    for path,digest in protected.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    configs={v:{**old['config'],**overrides} for v,overrides in settings['variants'].items()}
    manifest=dict(settings=settings,configs=configs,profiles=old['profiles'],b=old['b'],schedule=old['schedule'],
        protected_sha256=protected,source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [HERE/'short_exit_config.json',HERE/'short_exit_tuning.py',HERE/'entry_model.py',HERE/'population_time_metrics.py']})
    OUTPUT.mkdir(exist_ok=True);(OUTPUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    results=[]
    for r in prior:
        prefix=ROOT/r['data_prefix'];s=json.loads((prefix.parent/'summary.json').read_text())
        episodes=json.loads(Path(str(prefix)+'-episodes.json').read_text())
        with np.load(str(prefix)+'.npz') as z:d=describe_run({k:z[k] for k in z.files},s,episodes,[],settings)
        for key in ['holding_median_minutes','short_realized_per_account','short_mean_wealth','trades_per_minute']:
            assert np.isclose(d[key],r[key])
        d.update(variant='baseline',scenario=r['scenario'],seed=r['seed'],data_prefix=r['data_prefix']);results.append(d)
    schedule={int(t):v for t,v in old['schedule'].items()}
    with ProcessPoolExecutor(max_workers=3) as pool:
        jobs=[pool.submit(run_case,v,scenario,seed,cfg,old['profiles'],old['b'],settings,schedule)
              for v,cfg in configs.items() if v!='baseline' for scenario in ['normal','shock'] for seed in settings['market_seeds']]
        for future in as_completed(jobs):
            results.append(future.result());(OUTPUT/'metrics.json').write_text(json.dumps(results,indent=2,allow_nan=False)+'\n')
    for v in configs:
        for seed in settings['market_seeds']:
            normal=next(r for r in results if (r['variant'],r['scenario'],r['seed'])==(v,'normal',seed))
            shock=next(r for r in results if (r['variant'],r['scenario'],r['seed'])==(v,'shock',seed))
            with np.load(ROOT/(normal['data_prefix']+'.npz')) as a,np.load(ROOT/(shock['data_prefix']+'.npz')) as b:
                for key in a.files:np.testing.assert_array_equal(a[key][:1000],b[key][:1000],err_msg=key)
    for path,digest in {**protected,**manifest['source_sha256']}.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    (OUTPUT/'audit.json').write_text(json.dumps(dict(paths=24,new_paths=18,reused_paths=6,both_assets_conserved=True,
        individual_wallet_marks_and_fills_verified=True,first_event_prefix_exact=True,
        old_results_and_sources_unchanged=True,protected_files_unchanged=len(protected)),indent=2)+'\n')


def report():
    m=json.loads((OUTPUT/'manifest.json').read_text());rs=json.loads((OUTPUT/'metrics.json').read_text());variants=list(m['configs'])
    fields=['holding_median_minutes','holding_mean_minutes','holding_p90_minutes','short_realized_per_account',
        'short_pnl_per_completed','short_win_fraction','stop_loss_fraction','stop_loss_per_account_hour',
        'trades_per_minute','short_trades_per_minute','clipped_fraction','mean_retraced_10m']
    rows=[dict(variant=r['variant'],scenario=r['scenario'],seed=r['seed'],range30=r['range30']['median'],
        **{key:r.get(key) for key in fields}) for r in sorted(rs,key=lambda r:(variants.index(r['variant']),r['scenario'],r['seed']))]
    with (OUTPUT/'comparison.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    def avg(v,scenario,key):return float(np.mean([r['range30']['median'] if key=='range30' else r[key]
        for r in rs if (r['variant'],r['scenario'])==(v,scenario)]))
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False})
    fig,axes=plt.subplots(1,2,figsize=(14,5))
    for ax,key,title in zip(axes,['holding_median_minutes','short_realized_per_account'],['常态短线持仓中位数','常态每位短线已实现盈亏']):
        for i,v in enumerate(variants):
            vals=[r[key] for r in rs if r['variant']==v and r['scenario']=='normal']
            ax.scatter(i+np.linspace(-.1,.1,3),vals,s=40,color='#176997');ax.plot([i-.25,i+.25],[np.mean(vals)]*2,color='#176997',lw=2)
        ax.set(xticks=range(len(variants)),xticklabels=[LABELS[v] for v in variants],title=title,ylabel='分钟' if key.startswith('holding') else '金圆券／每户')
        ax.tick_params(axis='x',labelsize=8);ax.grid(axis='y',alpha=.2)
    fig.tight_layout();fig.savefig(OUTPUT/'comparison.png',dpi=150);plt.close(fig)
    lines=['# 20人短线持仓小幅缩短','','## 固定设置与比较档位','','20人=10随机+5库存+5短线，5秒tick、库存30分钟、热度tau45分钟、psi=.0008，短线趋势权重¼；每户5000金圆券+1000外币，每轮200，不改人数、到达热度、订单力度或策略结构。每条10000tick约13小时53分钟，种子81/82/83，常态与相同10次±5%日程；终点事件无后续，不计入响应。原六条复用，其余三档各六条新跑，共24条。','','预先固定三档：A只将止盈从.5%降至.4%；B在A基础上把移动启动/回撤从.25%各降为.2%，止损仍.5%；C再把止损降至.4%。没有强制持仓超时，也不增加独立退出检查或全市场热度；所有退出仍由原BehaviorTrader在随机到达时检查。','','## 常态：三个种子等权均值','','|指标|原参数|止盈0.4%|止盈0.4%＋移动0.2%|止盈止损0.4%＋移动0.2%|','|---|---:|---:|---:|---:|']
    comparisons=[('持仓中位数/分钟','holding_median_minutes','.2f'),('持仓平均值/分钟','holding_mean_minutes','.2f'),
        ('持仓P90/分钟','holding_p90_minutes','.2f'),('每位短线已实现盈亏','short_realized_per_account','+.2f'),
        ('每完成轮次平均盈亏','short_pnl_per_completed','+.2f'),('盈利轮次占比','short_win_fraction','.1%'),
        ('止损轮次占比','stop_loss_fraction','.1%'),('每户每小时止损轮次','stop_loss_per_account_hour','.2f'),
        ('全部成交笔数/分钟','trades_per_minute','.2f'),('短线成交笔数/分钟','short_trades_per_minute','.2f'),
        ('30分钟波幅中位数','range30','.2%'),('裁剪/订单尝试','clipped_fraction','.2%')]
    for label,key,fmt in comparisons:lines.append('|'+label+'|'+'|'.join(format(avg(v,'normal',key),fmt) for v in variants)+'|')
    lines+=['','## 冲击：三个种子等权均值','','|指标|原参数|止盈0.4%|止盈0.4%＋移动0.2%|止盈止损0.4%＋移动0.2%|','|---|---:|---:|---:|---:|']
    for label,key,fmt in [('持仓中位数/分钟','holding_median_minutes','.2f'),('每位短线已实现盈亏','short_realized_per_account','+.2f'),
        ('止损轮次占比','stop_loss_fraction','.1%'),('每户每小时止损轮次','stop_loss_per_account_hour','.2f'),
        ('全部成交笔数/分钟','trades_per_minute','.2f'),('10分钟平均原始回吐','mean_retraced_10m','.1%')]:
        lines.append('|'+label+'|'+'|'.join(format(avg(v,'shock',key),fmt) for v in variants)+'|')
    lines+=['','## 口径与验证','','持仓统计仅包括完成轮次，时间按5秒换算；P90先逐路径计算再按种子平均，不是硬上限。未完成轮次及最老持仓年龄见metrics.json，未用强制退出缩短统计。已实现盈亏按实际5个短线户归一化，止损次数同时报告占比和每户每小时频率，防止以更频繁亏损换取较短持仓。各档改变市场自身路径，不是固定外生价格的逐笔配对回放。','','30分钟波幅使用360tick完整窗口；10分钟回吐观察120tick，取最后60秒12个收盘均价。各路径9个可观察事件相关，不把27个事件当独立种子。此轮无玩家，不将参数变化直接解释为玩家体验已改善。','','直接复用现有模拟器、三因子入口和退出算法；只覆盖退出阈值。逐tick核对两币守恒、钱包非负、个人净值与成交计数、正常/冲击首事件前缀、退出原因盈亏总和及旧文件哈希。没有新增永久测试；运行已有研究检查。','','![持仓与盈亏](comparison.png)','','复现`python -m research.fx.reversion.repeated.short_exit_tuning`；仅重建报告加`--report-only`。','','[种子级CSV](comparison.csv)、[完整指标与退出分解](metrics.json)、[固定参数和源哈希](manifest.json)、[运行核对](audit.json)。','']
    (OUTPUT/'report.md').write_text('\n'.join(lines));print('Report complete:',OUTPUT/'report.md',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--report-only',action='store_true');args=parser.parse_args()
    if not args.report_only:run()
    report()
