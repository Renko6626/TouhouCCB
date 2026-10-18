"""Five-second ticks and fixed 30/45-minute reference/activity memory settings."""
import argparse
import csv
import hashlib
import json
import math
from concurrent.futures import ProcessPoolExecutor,as_completed
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import ROOT,clean_json
from .entry_model import simulate
from .timing_metrics import metrics


HERE=Path(__file__).parent
BASE=HERE/'results/entry-policies'
OUTPUT=HERE/'results/timing-5s'


def run_case(variant,scenario,seed,cfg,profiles,b,settings,schedule):
    directory=OUTPUT/f'{variant}-{scenario}-seed{seed}'
    reused=variant=='baseline' and scenario=='normal'
    if reused:
        directory=BASE/f'weak_trend-seed{seed}'
        with np.load(directory/'normal.npz') as saved:x={k:saved[k] for k in saved.files}
        s=json.loads((directory/'summary.json').read_text())
        episodes=json.loads((directory/'normal-episodes.json').read_text())
        label='normal'
    else:
        events=schedule if scenario=='shock' else {}
        x,s,episodes,fills,inventory,decisions=simulate(cfg,profiles,10,b,seed,events,'weak_trend',settings)
        directory.mkdir(parents=True,exist_ok=True);label='trace'
        save_trace(directory,label,x,episodes,fills)
        (directory/'summary.json').write_text(json.dumps(clean_json(s),indent=2)+'\n')
        for name,records in [('inventory',inventory),('decisions',decisions)]:
            (directory/f'{name}.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
    assert all(np.isfinite(v).all() for v in x.values())
    assert x['account_gold'].min()>=0 and x['account_foreign'].min()>=0
    np.testing.assert_allclose(x['account_wealth'],x['account_gold']+x['account_foreign']*x['prices'][:,None],atol=1e-8,rtol=0)
    np.testing.assert_allclose(x['gold']+x['account_gold'].sum(axis=1),1200000+np.cumsum(x['injection_gold']),atol=1e-8,rtol=0)
    np.testing.assert_allclose(x['pool_foreign']+x['account_foreign'].sum(axis=1),240000+np.cumsum(x['injection_foreign']),atol=1e-8,rtol=0)
    np.testing.assert_array_equal(x['account_trades'].sum(axis=1),x['trades'])
    assert abs(sum(float(e['realized_profit']) for e in episodes)-float(s['behavior']['realized_pnl']))<1e-7
    if scenario=='shock':
        assert len(s['events'])==10 and s['events'][-1]['tick']==10000
        for e in s['events']:assert abs(float(e['post_price'])/float(e['pre_price'])-1-schedule[e['tick']])<1e-11
    else:assert not s['events'] and s['issued_gold']=='0' and s['issued_foreign']=='0'
    result=metrics(x,s,episodes,5)
    result.update(variant=variant,scenario=scenario,seed=seed,reused=reused,
        data_prefix=str((directory/label).relative_to(ROOT)))
    print(f"Complete {variant} {scenario} seed={seed}: hold={result['holding_median_minutes']:.2f}min range30={result['range30']['median']:.2%}"+
        (f" retrace10={result['mean_retraced_10m']:.1%}" if scenario=='shock' else ''),flush=True)
    return result


def run():
    settings=json.loads((HERE/'timing_config.json').read_text())
    old=json.loads((BASE/'manifest.json').read_text())
    protected={**old['protected_sha256'],**old['source_sha256']}
    for seed in settings['market_seeds']:
        for name in ['normal.npz','normal-episodes.json','summary.json']:
            p=BASE/f'weak_trend-seed{seed}'/name;protected[str(p.relative_to(ROOT))]=hashlib.sha256(p.read_bytes()).hexdigest()
    for path,digest in protected.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    configurations={}
    for variant in ['baseline','short_memory']:
        configurations[variant]={**old['config'],'tau':settings['baseline_tau'] if variant=='baseline' else settings['tau'],
            'inventory_window':settings['baseline_inventory_window'] if variant=='baseline' else settings['inventory_window']}
    ticks=list(range(settings['event_interval'],settings['ticks']+1,settings['event_interval']))
    signs=np.random.default_rng(settings['shock_seed']).choice([-1,1],size=len(ticks))
    schedule={t:float(sign*settings['event_abs_return']) for t,sign in zip(ticks,signs)}
    manifest=dict(settings=settings,configs=configurations,profiles=old['profiles'],b=old['b'],schedule=schedule,
        protected_sha256=protected,source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [HERE/'timing_config.json',HERE/'timing_metrics.py',HERE/'timing_experiment.py',HERE/'entry_model.py']})
    OUTPUT.mkdir(exist_ok=True);(OUTPUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    results=[]
    with ProcessPoolExecutor(max_workers=3) as pool:
        jobs=[pool.submit(run_case,variant,scenario,seed,{**cfg,'seeds':[seed]},old['profiles'],old['b'],settings,schedule)
              for variant,cfg in configurations.items() for scenario in ['normal','shock'] for seed in settings['market_seeds']]
        for future in as_completed(jobs):
            results.append(future.result());(OUTPUT/'metrics.json').write_text(json.dumps(results,indent=2,allow_nan=False)+'\n')
    for variant in configurations:
        for seed in settings['market_seeds']:
            normal=next(r for r in results if (r['variant'],r['scenario'],r['seed'])==(variant,'normal',seed))
            shock=next(r for r in results if (r['variant'],r['scenario'],r['seed'])==(variant,'shock',seed))
            with np.load(ROOT/(normal['data_prefix']+'.npz')) as a,np.load(ROOT/(shock['data_prefix']+'.npz')) as b:
                for key in a.files:np.testing.assert_array_equal(a[key][:1000],b[key][:1000],err_msg=key)
    for path,digest in {**protected,**manifest['source_sha256']}.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    (OUTPUT/'audit.json').write_text(json.dumps(dict(paths=12,new_paths=9,reused_paths=3,
        both_assets_conserved=True,individual_wallets_and_fills_verified=True,
        first_event_prefix_exact=True,terminal_event_no_followup=True,
        protected_files_unchanged=len(protected)),indent=2)+'\n')


def report():
    m=json.loads((OUTPUT/'manifest.json').read_text());results=json.loads((OUTPUT/'metrics.json').read_text())
    groups={(v,c):[r for r in results if r['variant']==v and r['scenario']==c] for v in m['configs'] for c in ['normal','shock']}
    fields=['holding_median_minutes','short_realized_per_account','short_mean_vs_hold','short_win_fraction',
            'mean_intensity','peak_intensity','trades','clipped_fraction','executed_fraction','signed_acf_lag1',
            'abs_acf_5_30m','sq_acf_5_30m','mean_retraced_10m','mean_trades_100s','half_reached','half_sustained']
    rows=[]
    for r in sorted(results,key=lambda r:(r['variant'],r['scenario'],r['seed'])):
        rows.append(dict(variant=r['variant'],scenario=r['scenario'],seed=r['seed'],range30=r['range30']['median'],
            **{key:r.get(key) for key in fields}))
    with (OUTPUT/'comparison.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    with (OUTPUT/'events.csv').open('w') as f:
        events=[dict(variant=r['variant'],seed=r['seed'],**e) for r in results for e in r['events']]
        writer=csv.DictWriter(f,fieldnames=list(events[0]));writer.writeheader();writer.writerows(events)
    def avg(variant,scenario,key):
        return float(np.mean([r['range30']['median'] if key=='range30' else r[key] for r in groups[variant,scenario]]))
    labels={'baseline':'5秒、原tick参数','short_memory':'5秒、30/45分钟记忆'}
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False})
    fig,axes=plt.subplots(2,1,figsize=(13,8))
    for v,color in [('baseline','#7b8b9b'),('short_memory','#176997')]:
        for ax,scenario in zip(axes,['normal','shock']):
            row=next(r for r in groups[v,scenario] if r['seed']==81)
            with np.load(ROOT/(row['data_prefix']+'.npz')) as z:ax.plot(np.arange(10001)*5/3600,z['prices'],label=labels[v],color=color,lw=1)
    for ax,scenario in zip(axes,['normal','shock']):
        ax.set(xlabel='小时',ylabel='价格',title='seed81 无冲击' if scenario=='normal' else 'seed81 每1000tick随机±5%冲击')
        ax.legend();ax.grid(alpha=.2)
    fig.tight_layout();fig.savefig(OUTPUT/'comparison.png',dpi=150);plt.close(fig)
    gain={v:math.sqrt(1-math.exp(-2/cfg['tau']))/cfg['psi'] for v,cfg in m['configs'].items()}
    lines=['# 5秒tick与几十分钟记忆','','## 本轮固定设置','','采用40人=20随机+10库存+10短线，短线趋势项×¼。每户5000金圆券+1000外币，短线每轮200，随机订单标准差120，库存净目标±500、每次调整差额50%；止盈止损各.5%、移动启动/回撤各.25个百分点。psi=.0008、b=.25，总到达强度2×(.75+.25z²)。池100万金圆券/20万外币，无费用、补钱、重置或期末强平。','','本轮两组都使用5秒tick。对照保留库存720tick（60分钟）和tau1000（83.33分钟）；新候选为库存360tick（30分钟）和tau540（45分钟）。短线两窗口仍为30tick，即2.5分钟；不增加成交确认机制。','',
        f"tau是z的指数e倍衰减尺度，45分钟不是硬截断；无新价格变化时z半衰期约{540*5/60*math.log(2):.2f}分钟，z²超出基线部分半衰期约{540*5/120*math.log(2):.2f}分钟。此处只描述无进一步收益输入的机械衰减，不是市场冲击回吐半衰期。",
        '',f"保留psi不变时，缩短tau还使单次收益输入系数sqrt(1−exp(−2/tau))/psi从{gain['baseline']:.3f}变为{gain['short_memory']:.3f}，增强{gain['short_memory']/gain['baseline']-1:.1%}；这不是纯粹只改变衰减、不改变输入增益的实验。",'',
        '市场种子81/82/83。每组每种子各有无冲击和随机±5%冲击路径，共12条；对照无冲击的三条原始tick路径可直接复用前轮趋势权重¼结果，仅按5秒重新计时与统计；其余9条新模拟。每条10000tick约13小时53分钟。冲击方向独立种子20261015，每1000tick一次，即83分钟20秒一次，4涨6跌；最后一次在终点，无后续。没有新增持续趋势场景。','','## 无冲击：种子等权均值','','|指标|5秒、原tick参数|5秒、30/45分钟记忆|','|---|---:|---:|']
    normal_fields=[('30分钟波幅中位数','range30','.2%'),('短线持仓中位数/分钟','holding_median_minutes','.2f'),
        ('每位短线已实现盈亏','short_realized_per_account','+.2f'),('每位短线相对持有基准差额','short_mean_vs_hold','+.2f'),
        ('短线完成轮次盈利占比','short_win_fraction','.1%'),('平均到达强度/tick','mean_intensity','.3f'),
        ('实际成交笔数','trades',',.0f'),('裁剪/实际订单尝试','clipped_fraction','.2%'),('实际/意向绝对量','executed_fraction','.2%'),
        ('收益ACF lag1（5秒）','signed_acf_lag1','.5f'),('绝对收益ACF 5–30分钟','abs_acf_5_30m','.5f'),
        ('平方收益ACF 5–30分钟','sq_acf_5_30m','.5f')]
    for label,key,fmt in normal_fields:lines.append('|'+label+'|'+'|'.join(format(avg(v,'normal',key),fmt) for v in m['configs'])+'|')
    lines+=['','## 冲击：种子等权均值','','|指标|5秒、原tick参数|5秒、30/45分钟记忆|','|---|---:|---:|']
    shock_fields=[('10分钟平均原始回吐','mean_retraced_10m','.1%'),('事件后100秒平均成交笔数','mean_trades_100s','.1f'),
        ('曾回吐一半的事件数/9（种子均值）','half_reached','.2f'),('保持半幅约2分钟的事件数/9（种子均值）','half_sustained','.2f'),
        ('短线持仓中位数/分钟','holding_median_minutes','.2f'),('每位短线已实现盈亏','short_realized_per_account','+.2f'),
        ('观测强度峰值（种子均值）','peak_intensity','.2f'),('裁剪/实际订单尝试','clipped_fraction','.2%')]
    for label,key,fmt in shock_fields:lines.append('|'+label+'|'+'|'.join(format(avg(v,'shock',key),fmt) for v in m['configs'])+'|')
    lines+=['','## 指标与验证','','全部时间由tick_seconds=5换算。30分钟窗口为360tick，取27个完整非重叠窗口，尾部280tick不计；波幅=(窗口最高−最低)/期初价。持仓时间=(退出tick−入场tick)×5/60。10分钟响应为120tick后，取最后60秒的12个收盘均价（第109～120tick），上下冲击统一按带符号跳幅计算。持续半幅要求24个连续收盘，标记起始收盘，约2分钟；下一次事件tick前截断。各路径9个事件相关，先路径内汇总再种子等权，不把27次当独立样本。','','不复用旧报告硬编码的/6、180tick、60tick时间口径。旧版回吐曾将第49～60tick的12个收盘称为9～10分钟，按10秒实际对应8分10秒～10分钟；本轮明确按实际秒数计算最后1分钟，两个新列口径完全一致。原报告保留历史结果，不修改旧文件。','','收益统计仅将事件tick替换为常规成交后、发行前收益，保留之后的响应，不等于无冲击市场。ACF含零收益、全局去均值、采用滞后乘积和/零滞后平方和；5–30分钟对应lag61–360。净值=现金+外币×当时边际价，含未实现盈亏；持有基准H=5000+1000×本路径期末价。','','逐tick核对两币守恒、钱包非负、个人净值和成交计数；每组每种子的正常/冲击路径在首事件前逐字段完全一致；事件幅度与终点处理核对。新时间指标已有360tick窗口和120tick响应的实际行为检查。旧源码与结果哈希保持。未验证玩家介入、24小时全程或游戏性能。','','![行情对照](comparison.png)','','运行：`python -m research.fx.reversion.repeated.timing_experiment`；仅重建报告加`--report-only`。','','[种子级CSV](comparison.csv)、[逐次事件](events.csv)、[完整指标](metrics.json)、[设置与来源](manifest.json)、[运行核对](audit.json)。','']
    (OUTPUT/'report.md').write_text('\n'.join(lines));print('Report complete:',OUTPUT/'report.md',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--report-only',action='store_true');args=parser.parse_args()
    if not args.report_only:run()
    report()
