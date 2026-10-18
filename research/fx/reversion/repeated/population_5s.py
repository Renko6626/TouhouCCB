"""Twenty-account low-intervention candidate versus the frozen 40-account 5s model."""
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
BASE=HERE/'results/timing-5s'
OUTPUT=HERE/'results/population-5s'


def run_case(scenario,seed,cfg,profiles,b,settings,schedule):
    n=cfg['accounts'];cfg={**cfg,'seeds':[seed]}
    x,s,episodes,fills,inventory,decisions=simulate(cfg,profiles,n//4,b,seed,
        schedule if scenario=='shock' else {},'weak_trend',settings)
    assert all(np.isfinite(v).all() for v in x.values())
    assert x['account_gold'].min()>=0 and x['account_foreign'].min()>=0
    np.testing.assert_allclose(x['account_wealth'],x['account_gold']+x['account_foreign']*x['prices'][:,None],atol=1e-8,rtol=0)
    np.testing.assert_allclose(x['gold']+x['account_gold'].sum(axis=1),1000000+n*5000+np.cumsum(x['injection_gold']),atol=1e-8,rtol=0)
    np.testing.assert_allclose(x['pool_foreign']+x['account_foreign'].sum(axis=1),200000+n*1000+np.cumsum(x['injection_foreign']),atol=1e-8,rtol=0)
    np.testing.assert_array_equal(x['account_trades'].sum(axis=1),x['trades'])
    assert [sum(a['policy']==g for a in s['accounts']) for g in ['random','reversion','episode']]==[10,5,5]
    assert abs(sum(float(e['realized_profit']) for e in episodes)-float(s['behavior']['realized_pnl']))<1e-7
    if scenario=='shock':
        assert len(s['events'])==10 and s['events'][-1]['tick']==10000
        for e in s['events']:assert abs(float(e['post_price'])/float(e['pre_price'])-1-schedule[e['tick']])<1e-11
    else:assert not s['events'] and s['issued_gold']=='0' and s['issued_foreign']=='0'
    directory=OUTPUT/f'n20-{scenario}-seed{seed}';directory.mkdir(parents=True,exist_ok=True)
    save_trace(directory,'trace',x,episodes,fills)
    (directory/'summary.json').write_text(json.dumps(clean_json(s),indent=2)+'\n')
    for name,records in [('inventory',inventory),('decisions',decisions)]:
        (directory/f'{name}.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
    result=metrics(x,s,episodes,settings['tick_seconds'])
    result.update(accounts=n,scenario=scenario,seed=seed,data_prefix=str((directory/'trace').relative_to(ROOT)),
        trades_per_minute=result['trades']/(result['hours']*60),
        foreign_volume_per_minute=float(x['executed_abs_quantity'][1:].sum()/(result['hours']*60)))
    print(f"Complete n20 {scenario} seed={seed}: range30={result['range30']['median']:.2%}, trades/min={result['trades_per_minute']:.2f}"+
          (f" retrace10={result['mean_retraced_10m']:.1%}" if scenario=='shock' else ''),flush=True)
    return result


def run():
    settings=json.loads((HERE/'population_5s_config.json').read_text());old=json.loads((BASE/'manifest.json').read_text())
    protected={**old['protected_sha256'],**old['source_sha256']}
    prior=[r for r in json.loads((BASE/'metrics.json').read_text()) if r['variant']=='short_memory']
    for r in prior:
        prefix=ROOT/r['data_prefix']
        for path in [Path(str(prefix)+'.npz'),Path(str(prefix)+'-episodes.json'),prefix.parent/'summary.json']:
            protected[str(path.relative_to(ROOT))]=hashlib.sha256(path.read_bytes()).hexdigest()
    for path in [BASE/'manifest.json',BASE/'metrics.json']:
        protected[str(path.relative_to(ROOT))]=hashlib.sha256(path.read_bytes()).hexdigest()
    for path,digest in protected.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    cfg={**old['configs']['short_memory'],'accounts':20}
    assert [key for key in cfg if cfg[key]!=old['configs']['short_memory'][key]]==['accounts']
    selected=old['profiles'][:10]+old['profiles'][20:25]+old['profiles'][30:35]
    profiles=[{**p,'account':i} for i,p in enumerate(selected)]
    schedule={int(t):float(v) for t,v in old['schedule'].items()}
    OUTPUT.mkdir(exist_ok=True)
    manifest=dict(settings=settings,config=cfg,profiles=profiles,b=old['b'],schedule=schedule,
        protected_sha256=protected,source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [HERE/'population_5s_config.json',HERE/'population_5s.py',HERE/'population_time_metrics.py',HERE/'entry_model.py']})
    (OUTPUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    results=[]
    for r in prior:
        prefix=ROOT/r['data_prefix'];s=json.loads((prefix.parent/'summary.json').read_text())
        episodes=json.loads(Path(str(prefix)+'-episodes.json').read_text())
        with np.load(str(prefix)+'.npz') as saved:
            x={k:saved[k] for k in saved.files};d=metrics(x,s,episodes,5)
            for key,value in d.items():assert value==r[key],key
            d.update(accounts=40,scenario=r['scenario'],seed=r['seed'],data_prefix=r['data_prefix'],
                trades_per_minute=d['trades']/(d['hours']*60),
                foreign_volume_per_minute=float(x['executed_abs_quantity'][1:].sum()/(d['hours']*60)))
        results.append(d)
    with ProcessPoolExecutor(max_workers=3) as pool:
        jobs=[pool.submit(run_case,scenario,seed,cfg,profiles,old['b'],settings,schedule)
              for scenario in ['normal','shock'] for seed in settings['market_seeds']]
        for future in as_completed(jobs):
            results.append(future.result());(OUTPUT/'metrics.json').write_text(json.dumps(results,indent=2,allow_nan=False)+'\n')
    for seed in settings['market_seeds']:
        a=next(r for r in results if (r['accounts'],r['scenario'],r['seed'])==(20,'normal',seed))
        b=next(r for r in results if (r['accounts'],r['scenario'],r['seed'])==(20,'shock',seed))
        with np.load(ROOT/(a['data_prefix']+'.npz')) as x,np.load(ROOT/(b['data_prefix']+'.npz')) as y:
            for key in x.files:np.testing.assert_array_equal(x[key][:1000],y[key][:1000],err_msg=key)
    for path,digest in {**protected,**manifest['source_sha256']}.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    (OUTPUT/'audit.json').write_text(json.dumps(dict(new_paths=6,reused_paths=6,
        original40_metrics_exact=True,both_assets_conserved=True,individual_wallet_marks_and_fills_verified=True,
        first_event_prefix_exact=True,terminal_event_no_followup=True,
        protected_files_unchanged=len(protected)),indent=2)+'\n')


def report():
    rs=json.loads((OUTPUT/'metrics.json').read_text());m=json.loads((OUTPUT/'manifest.json').read_text())
    fields=['trades_per_minute','foreign_volume_per_minute','mean_intensity','peak_intensity','holding_median_minutes',
            'short_realized_per_account','short_mean_vs_hold','short_win_fraction','clipped_fraction','executed_fraction',
            'signed_acf_lag1','abs_acf_5_30m','mean_retraced_10m','mean_trades_100s','half_reached','half_sustained']
    rows=[dict(accounts=r['accounts'],scenario=r['scenario'],seed=r['seed'],range30=r['range30']['median'],
        **{key:r.get(key) for key in fields}) for r in sorted(rs,key=lambda r:(r['accounts'],r['scenario'],r['seed']))]
    with (OUTPUT/'comparison.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    with (OUTPUT/'events.csv').open('w') as f:
        events=[dict(accounts=r['accounts'],seed=r['seed'],**e) for r in rs for e in r['events']]
        writer=csv.DictWriter(f,fieldnames=list(events[0]));writer.writeheader();writer.writerows(events)
    def avg(n,scenario,key):return float(np.mean([r['range30']['median'] if key=='range30' else r[key]
        for r in rs if r['accounts']==n and r['scenario']==scenario]))
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False})
    fig,axes=plt.subplots(2,1,figsize=(13,8))
    for n,color in [(40,'#788b9c'),(20,'#146b9b')]:
        for ax,scenario in zip(axes,['normal','shock']):
            r=next(r for r in rs if (r['accounts'],r['scenario'],r['seed'])==(n,scenario,81))
            with np.load(ROOT/(r['data_prefix']+'.npz')) as x:ax.plot(np.arange(10001)*5/3600,x['prices'],label=f'{n}人',color=color,lw=1)
    for ax,scenario in zip(axes,['normal','shock']):
        ax.set(xlabel='小时',ylabel='价格',title='seed81 无冲击' if scenario=='normal' else 'seed81 相同±5%事件日程')
        ax.grid(alpha=.2);ax.legend()
    fig.tight_layout();fig.savefig(OUTPUT/'comparison.png',dpi=150);plt.close(fig)
    lines=['# 5秒tick：20人低干预候选与40人对照','','## 设置','','保持5秒tick、库存参考360tick=30分钟、热度tau540tick=45分钟、psi=.0008、短线趋势权重¼。20人=10随机+5库存+5短线；40人=20随机+10库存+10短线。池100万金圆券/20万外币固定，每户5000金圆券+1000外币、短线每轮200、随机订单标准差120、库存净目标±500、调整差额50%、退出规则均不变。','','总到达率Lambda_N=(N/20)×(.75+.25z²)。相同z下每人查看频率不变，20人的整体强度减半；实际历史内生变化，不保证所有时段的成交恰好减半。总初始机器人资金从池市值的20%减到10%，因此人数、总资金和总活动共同减少。新增20人使用40人各角色参数的第一份副本，个人随机流仍独立。','','种子81/82/83，每条10000tick约13小时53分钟。每种人数都有常态与相同10次±5%冲击日程；冲击每1000tick（83分20秒），4涨6跌，最后一次在终点无后续。40人六条直接复用前轮，20人新跑六条，不增加玩家或持续趋势情景。','','## 常态：三个种子等权均值','','|指标|40人|20人|','|---|---:|---:|']
    normal=[('30分钟波幅中位数','range30','.2%'),('实际成交笔数/分钟','trades_per_minute','.2f'),
        ('实际成交绝对量/分钟（外币）','foreign_volume_per_minute','.1f'),('平均到达次数/tick','mean_intensity','.3f'),
        ('短线中位持仓/分钟','holding_median_minutes','.2f'),('每位短线已实现盈亏','short_realized_per_account','+.2f'),
        ('每位短线相对持有差额','short_mean_vs_hold','+.2f'),('短线完成轮次盈利占比','short_win_fraction','.1%'),
        ('裁剪/订单尝试','clipped_fraction','.2%'),('实际/意向绝对量','executed_fraction','.2%'),
        ('收益ACF lag1（5秒）','signed_acf_lag1','.5f'),('绝对收益ACF5–30分钟','abs_acf_5_30m','.5f')]
    for label,key,fmt in normal:lines.append('|'+label+'|'+'|'.join(format(avg(n,'normal',key),fmt) for n in [40,20])+'|')
    lines+=['','## 冲击：三个种子等权均值','','|指标|40人|20人|','|---|---:|---:|']
    shock=[('10分钟平均原始回吐','mean_retraced_10m','.1%'),('冲击后100秒平均成交笔数','mean_trades_100s','.1f'),
        ('短线中位持仓/分钟','holding_median_minutes','.2f'),('实际成交笔数/分钟（全程）','trades_per_minute','.2f'),
        ('曾回吐一半的事件数/9（种子均值）','half_reached','.2f'),('保持半幅约2分钟事件数/9（种子均值）','half_sustained','.2f'),
        ('每位短线已实现盈亏','short_realized_per_account','+.2f'),('裁剪/订单尝试','clipped_fraction','.2%')]
    for label,key,fmt in shock:lines.append('|'+label+'|'+'|'.join(format(avg(n,'shock',key),fmt) for n in [40,20])+'|')
    lines+=['','## 玩家体验与指标边界','','没有玩家订单，不能测定机器人占玩家成交的份额、挤占玩家机会或真实玩家体验。这里的每分钟成交、绝对量、总钱包规模和冲击回吐只用于衡量背景市场力量；减少人数并不自动保证玩家体验改善。','','30分钟波幅使用360tick完整非重叠窗口，27窗；10分钟回吐观察120tick后，取最后60秒12个收盘；持续半幅用24个收盘。回吐相对当次即时跳幅，不扣除长期偏移，最后终点事件留空；每条路径9个事件相关，不能当独立重复。ACF5–30分钟为lag61–360，含零收益、全局去均值的有偏估计；冲击收益仅替换瞬时发行跳价，保留随后响应。已实现短线盈亏按实际5/10个账户归一化，期末净值另含未平仓浮盈亏。','','20人与40人价格路径会分叉，同种子不代表逐笔交易配对；比较的是整个机器人规模的变化，非纯账户数量因果效应。尚未测试24小时全程、玩家介入、手续费或持续单边行情。','','![行情对照](comparison.png)','','## 复现与验证','','运行`python -m research.fx.reversion.repeated.population_5s`；只重建报告加`--report-only`。20人逐tick两币守恒、钱包非负、个人净值和成交计数核对；正常/冲击在首事件前逐字段一致。新人数通用指标在40人全部字段与已保存结果一致，20人的短线账户切片和盈亏分母使用实际人数，避免沿用40人常量。源与旧结果哈希保持。','','[种子级CSV](comparison.csv)、[完整指标](metrics.json)、[逐次事件](events.csv)、[配置与源哈希](manifest.json)、[运行核对](audit.json)。','']
    (OUTPUT/'report.md').write_text('\n'.join(lines));print('Report complete:',OUTPUT/'report.md',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--report-only',action='store_true');args=parser.parse_args()
    if not args.report_only:run()
    report()
