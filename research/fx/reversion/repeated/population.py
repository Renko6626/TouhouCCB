"""20/40/80 accounts: fixed per-wallet resources, role mix and per-person arrival rule."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from research.fx.episodes.run import save_trace
from research.fx.factor_activity.run import ROOT, clean_json
from .population_model import replicate_profiles, simulate
from .sensitivity import measure
from .run import OUTPUT as ORIGINAL


HERE=Path(__file__).parent
BASE=ORIGINAL/'sensitivity-psi0008'
OUTPUT=ORIGINAL/'population'


def run():
    old=json.loads((BASE/'manifest.json').read_text())
    protected={**old['baseline_protected_sha256'],**old['source_sha256']}
    for p in BASE.iterdir():
        if p.is_file():protected[str(p.relative_to(ROOT))]=hashlib.sha256(p.read_bytes()).hexdigest()
    for path,digest in protected.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    schedule={int(t):jump for t,jump in old['schedule'].items()}
    metrics={};manifest=dict(base_config=old['config'],base_spec=old['spec'],schedule=schedule,
        populations=[20,40,80],population_rule='Half random, quarter inventory, quarter episodes; repeat frozen role-specific personal coefficients. Pool, per-account wallet and order sizes fixed. Actual aggregate Lambda_N=(N/20)*(.75+.25*z^2), z update and psi=.0008 unchanged. Each account has its own arrival/mark RNG stream.',
        protected_sha256=protected,cases={},source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [HERE/'population.py',HERE/'population_model.py',HERE/'sensitivity.py',HERE/'metrics.py']})
    OUTPUT.mkdir(exist_ok=True)
    base_metrics=json.loads((BASE/'metrics.json').read_text())
    for n in [20,40,80]:
        directory=OUTPUT/f'n{n}';directory.mkdir(exist_ok=True)
        cfg={**old['config'],'accounts':n}
        profiles=replicate_profiles(old['profiles'],n)
        manifest['cases'][str(n)]=dict(config=cfg,profiles=profiles,inventory_count=n//4,arrival_scale=n/20)
        summaries={};control=None
        for case,events in [('control',{}),('shock',schedule)]:
            if n==20:
                metrics[f'n{n}_{case}']=base_metrics['new_'+case]
                continue
            x,s,episodes,fills,inventory_fills=simulate(cfg,profiles,n//4,old['spec']['b'],old['spec']['market_seed'],events)
            assert all(np.isfinite(a).all() for a in x.values())
            assert x['account_gold'].shape==(10001,n)
            assert x['account_gold'].min()>=0 and x['account_foreign'].min()>=0
            np.testing.assert_allclose(x['account_wealth'],x['account_gold']+x['account_foreign']*x['prices'][:,None],atol=1e-8,rtol=0)
            np.testing.assert_allclose(x['gold']+x['account_gold'].sum(axis=1),1000000+n*5000+np.cumsum(x['injection_gold']),atol=1e-8,rtol=0)
            np.testing.assert_allclose(x['pool_foreign']+x['account_foreign'].sum(axis=1),200000+n*1000+np.cumsum(x['injection_foreign']),atol=1e-8,rtol=0)
            np.testing.assert_array_equal(x['account_trades'].sum(axis=1),x['trades'])
            assert [sum(a['policy']==g for a in s['accounts']) for g in ['random','reversion','episode']]==[n//2,n//4,n//4]
            if case=='control':control=x
            else:
                for k in x:np.testing.assert_array_equal(x[k][:1000],control[k][:1000])
                assert len(s['events'])==10 and s['events'][-1]['tick']==10000
                for e in s['events']:assert abs(float(e['post_price'])/float(e['pre_price'])-1-schedule[e['tick']])<1e-11
            save_trace(directory,case,x,episodes,fills)
            (directory/f'{case}-inventory.jsonl').write_text(''.join(json.dumps(f)+'\n' for f in inventory_fills))
            metrics[f'n{n}_{case}'],_=measure(x,s,sorted(schedule))
            summaries[case]=s
            (directory/'summary.json').write_text(json.dumps(clean_json(summaries),indent=2)+'\n')
            print(f"Complete n={n} {case}: mean intensity={metrics[f'n{n}_{case}']['mean_intensity']:.3f}, peak={s['peak_intensity']:.3f}",flush=True)
        (OUTPUT/'metrics.json').write_text(json.dumps(metrics,indent=2,allow_nan=False)+'\n')
    for path,digest in protected.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    (OUTPUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    (OUTPUT/'audit.json').write_text(json.dumps(dict(new_paths=4,reused_paths=2,protected_files_unchanged=len(protected),
        both_assets_conserved=True,wallets_nonnegative=True,individual_wealth_verified=True,
        first_event_prefix_exact=True,event_sizes_exact=True,terminal_event_excluded_from_followup=True),indent=2)+'\n')


def report():
    metrics=json.loads((OUTPUT/'metrics.json').read_text())
    manifest=json.loads((OUTPUT/'manifest.json').read_text())
    colors={20:'#62778b',40:'#13669c',80:'#b36b1d'}
    fig,axes=plt.subplots(3,1,figsize=(13,11))
    curves={}
    for n in [20,40,80]:
        directory=BASE if n==20 else OUTPUT/f'n{n}'
        summaries=json.loads((directory/'summary.json').read_text())
        for ax,case in zip(axes[:2],['control','shock']):
            with np.load(directory/f'{case}.npz') as x:
                ax.plot(np.arange(10001)/360,x['prices'],label=f'{n}人',color=colors[n],lw=.9)
                if case=='shock':
                    q=[]
                    for e in summaries[case]['events'][:-1]:
                        t=e['tick'];pre=float(e['pre_price']);post=float(e['post_price'])
                        q.append((post-x['prices'][t:t+1000])/(post-pre))
                    curves[n]=np.mean(q,axis=0)
        axes[2].plot(np.arange(1000)/6,curves[n]*100,label=f'{n}人',color=colors[n])
    for ax,title in zip(axes[:2],['无冲击行情：池不变，每户资源不变','相同10次±5%冲击日程；每1000tick一次']):
        ax.set(xlabel='小时',ylabel='价格',title=title)
    for t in manifest['schedule']:axes[1].axvline(int(t)/360,color='gray',alpha=.15)
    axes[2].set(xlabel='冲击后分钟',ylabel='平均原始回吐（%）',title='前9次事件等权；未扣除长期价格偏移')
    axes[2].axhline(50,color='gray',ls='--')
    for ax in axes:ax.legend();ax.grid(alpha=.2)
    fig.tight_layout();fig.savefig(OUTPUT/'comparison.png',dpi=150);plt.close(fig)
    rows=[]
    for n in [20,40,80]:
        for case in ['control','shock']:
            d=metrics[f'n{n}_{case}']
            rows.append(dict(accounts=n,case=case,mean_intensity=d['mean_intensity'],max_intensity=d['max_intensity'],
                trades=d['trades'],normal_range30_median=d['range30_all']['median'] if case=='control' else None,
                clipped_fraction=d['clipped_per_attempt'],executed_fraction=d['executed_over_intended'],
                holding_median_minutes=d['holding_median_minutes'],mean_retraced_10m=d.get('mean_retraced_10m'),
                mean_first10_trades=d.get('mean_first10_trades'),mean_first10_distinct_traders=d.get('mean_first10_distinct_traders'),
                final_price=d['final_price'],final_bot_wealth_fraction=d['final_bot_wealth']/(n*10000)-1))
    with (OUTPUT/'comparison.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    with (OUTPUT/'events.csv').open('w') as f:
        events=[dict(accounts=n,**e) for n in [20,40,80] for e in metrics[f'n{n}_shock']['events']]
        writer=csv.DictWriter(f,fieldnames=list(events[0]));writer.writeheader();writer.writerows(events)
    lines=['# 增加人数：20、40、80人对照','','问题：在高敏感psi=.0008版本上，增员能否改善30分钟约2%常态波幅和冲击回归。只试预先指定的40/80人，没有按结果追加搜索。','','## 实验设置','','1tick10秒，每条10000tick；市场81、人口原301的角色参数成倍复制、冲击种子20261015。人数比例固定为一半随机、四分之一库存、四分之一短线。复制系数不复制随机决策：每个ID仍有独立到达与订单/噪声流，共享公开历史热度。新增资金只在初始化分配，过程中不补钱或重置。','','池固定100万金圆券/20万外币，每户固定5000金圆券+1000外币，短线每轮200外币、库存净目标±500、随机订单标准差120。20/40/80人的机器人初始总市值分别20万/40万/80万，相当于初始池市值的10%/20%/40%。因此本轮是人口、总资金和总活动的共同扩张，不是固定总资金的纯账户拆分。','','总到达率Lambda_N=(N/20)×[.75+.25z²]；z递推不变，psi=.0008、tau1000。相同价格历史下每人到达率保持一致；不能只把人数改大却把原总强度原封不动地分摊。实际内生历史不同，频率不保证严格成倍。未新增热度上限或调整策略。','','每个人数都有无冲击和相同10次±5%事件日程路径；20人复用刚才已保存的高敏感结果，40/80各运行两条新路径。相同种子不代表分叉后逐笔交易配对；每次发行额随当时池储备变化。前9次事件可观察，第10000tick最后冲击无后续。','','## 常态行情（无外部冲击）','','|指标|20人|40人|80人|','|---|---:|---:|---:|']
    normal=[('30分钟波幅中位数',lambda d:f"{d['range30_all']['median']:.2%}"),
        ('30分钟波幅P10–P90',lambda d:f"{d['range30_all']['p10']:.2%}～{d['range30_all']['p90']:.2%}"),
        ('平均总到达率/tick',lambda d:f"{d['mean_intensity']:.2f}"),('最大总到达率/tick',lambda d:f"{d['max_intensity']:.2f}"),
        ('单tick最大实际到达次数',lambda d:str(d['max_arrivals'])),('实际成交笔数',lambda d:str(d['trades'])),
        ('短线中位持仓/分钟',lambda d:f"{d['holding_median_minutes']:.2f}"),
        ('收益ACF lag1',lambda d:f"{d['returns']['acf']['signed']['1_1']:.5f}"),
        ('收益ACF最大绝对值1–100',lambda d:f"{d['returns']['acf']['signed']['max_abs_1_100']:.5f}"),
        ('绝对收益ACF31–180',lambda d:f"{d['returns']['acf']['abs']['31_180']:.5f}"),
        ('平方收益ACF31–180',lambda d:f"{d['returns']['acf']['sq']['31_180']:.5f}"),
        ('超额峰度',lambda d:f"{d['returns']['shape']['excess_kurtosis']:.2f}"),
        ('裁剪订单/实际订单尝试',lambda d:f"{d['clipped_per_attempt']:.2%}"),
        ('实际执行/意向绝对量',lambda d:f"{d['executed_over_intended']:.2%}")]
    for label,fmt in normal:lines.append('|'+label+'|'+'|'.join(fmt(metrics[f'n{n}_control']) for n in [20,40,80])+'|')
    lines+=['','## 冲击响应','','|指标|20人|40人|80人|','|---|---:|---:|---:|']
    event_fields=[('事件后100秒平均到达强度/tick',lambda d:f"{d['mean_first10_intensity']:.2f}"),
        ('事件后100秒平均成交笔数',lambda d:f"{d['mean_first10_trades']:.1f}"),
        ('事件后100秒平均参与人数',lambda d:f"{d['mean_first10_distinct_traders']:.1f}"),
        ('10分钟平均原始回吐',lambda d:f"{d['mean_retraced_10m']:.1%}"),
        ('曾回吐一半的事件数/9',lambda d:str(d['events_half_reached'])),
        ('回吐一半且连续保持12收盘的事件数/9',lambda d:str(d['events_half_sustained'])),
        ('最大到达强度/tick',lambda d:f"{d['max_intensity']:.2f}"),
        ('单tick最大实际到达数',lambda d:str(d['max_arrivals'])),
        ('裁剪/订单尝试',lambda d:f"{d['clipped_per_attempt']:.2%}"),
        ('实际执行/意向绝对量',lambda d:f"{d['executed_over_intended']:.2%}"),
        ('短线中位持仓/分钟',lambda d:f"{d['holding_median_minutes']:.2f}"),
        ('期末机器人总净值变化',lambda d,n=None:'')]
    for label,fmt in event_fields[:-1]:lines.append('|'+label+'|'+'|'.join(fmt(metrics[f'n{n}_shock']) for n in [20,40,80])+'|')
    for n in [20,40,80]:
        hits=np.flatnonzero(curves[n]>=.5)
        lines.append(f"\n{n}人平均回吐曲线首次到50%：{str(round(float(hits[0]/6),2))+'分钟' if len(hits) else '未到'}；峰值{curves[n].max():.1%}。这是首次触及，不是持久半衰期。")
    lines+=['','![行情及回吐对照](comparison.png)','','## 指标口径与验证','','30分钟波幅=(最高价−最低价)/窗口期初价；无冲击路径55个非重叠完整180tick窗口，尾部100tick不计。收益为收盘对数收益，含零收益，ACF使用common.diagnostics全局去均值的有偏估计，区间各lag等权平均；超额峰度正态为0。完整原始分布与较远滞后见metrics.json。','','10分钟回吐使用事件后49～60tick均价，相对于该次即时跳幅计算；涨跌统一用带符号跳幅。半幅观察在下次事件tick之前截断，连续12个收盘约2分钟。原始回吐没有扣除长期偏移，也不能用没有任何此前事件的对照逐次做边际因果校正。事件来自同一路径，共享钱包和历史，9个事件不等于9个独立种子。','','只改变人口、初始总资金、整体到达规模；未改变池、每户额度、psi或策略规则。不能把结果当作单独的人数因果效应、真实市场校准或多种子稳健性。新模拟逐tick核对两币守恒、钱包非负、个人净值及成交总数、首个事件前路径一致、±5%事件大小与终点处理。20人扩展模拟器与原模拟器有短路径逐字段与账本完全相同的检查。旧源码和原始结果哈希保持。','','运行：`python -m research.fx.reversion.repeated.population`；只重建报告：`python -m research.fx.reversion.repeated.population --report-only`。','','[完整指标](metrics.json)、[逐次事件](events.csv)、[紧凑CSV](comparison.csv)、[设置与源哈希](manifest.json)、[运行核对](audit.json)。20人原始路径见../sensitivity-psi0008；40/80人的个人钱包、短线轮次及成交账本保存在n40/n80子目录。','']
    (OUTPUT/'report.md').write_text('\n'.join(lines))
    print('Report complete:',OUTPUT/'report.md',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--report-only',action='store_true');args=parser.parse_args()
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False})
    if not args.report_only:run()
    report()
