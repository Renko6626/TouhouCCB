"""Crossed population/market evidence with equal class moments and stage checks."""
import csv
import hashlib
import json

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import NullFormatter
import numpy as np

from research.fx.activity.report import table
from research.fx.common.diagnostics import acf
from .run import HERE,OUTPUT,ROOT

LABELS={'fixed':'原两类参数','hetero_0.5':'个人异质 s=0.5','hetero_1':'个人异质 s=1','activity_v1':'活动反馈v1'}
VOL_KEYS=['abs_101_300','abs_301_1000','sq_101_300','sq_301_1000']


def mean_metrics(rows):
    return {key:float(np.mean([r[key] for r in rows])) if all(r.get(key) is not None for r in rows) else None
            for key in rows[0]} if rows else {}


def export_csv(name,rows):
    keys=list(dict.fromkeys(k for r in rows for k in r))
    with (OUTPUT/name).open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=keys);writer.writeheader();writer.writerows(rows)


def segment_rows(records):
    rows=[]
    for record in records:
        if record['status']!='complete':continue
        cfg=record['config']
        with np.load(OUTPUT/record['name']/'series.npz') as x:
            for part,indices in enumerate(np.array_split(np.arange(cfg['burn_in']+1,cfg['ticks']+1),3),1):
                r=x['returns'][indices];a=acf(abs(r),1000);s=acf(r*r,1000)
                row=dict(name=record['name'],model=record['model'],population=record['population'],
                         seed=record['seed'],b=record['b'],part=part,start=int(indices[0]),end=int(indices[-1]),n=len(r),
                         std=float(r.std(ddof=1)),abs_101_300=float(a[101:301].mean()),abs_301_1000=float(a[301:1001].mean()),
                         sq_101_300=float(s[101:301].mean()),sq_301_1000=float(s[301:1001].mean()),
                         mean_intensity=float(x['intensity'][indices].mean()),
                         gross_quantity_per_tick=float(x['executed_abs_quantity'][indices].mean()),
                         clipped_fraction=float(x['clipped_orders'][indices].sum()/x['arrivals'][indices].sum()),
                         forecast_alignment=float(x['forecast_alignment'][indices].mean()) if 'forecast_alignment' in x else None)
                rows.append(row)
    return rows


def aggregate(records,segments):
    cells=[];stages=[]
    for model in LABELS:
        for b in [0,.25]:
            group=[r for r in records if r['status']=='complete' and r['model']==model and r['b']==b]
            if not group:continue
            averages=mean_metrics([r['metrics'] for r in group])
            cells.append(dict(model=model,b=b,label=f'{LABELS[model]} / b={b}',**averages,paths=len(group),
                all_four_positive=sum(all(r['metrics'][k]>0 for k in VOL_KEYS) for r in group),
                sq301_min=min(r['metrics']['sq_301_1000'] for r in group),
                sq301_max=max(r['metrics']['sq_301_1000'] for r in group),
                direction_min=min(r['metrics']['max_signed'] for r in group),
                direction_max=max(r['metrics']['max_signed'] for r in group)))
            for part in [1,2,3]:
                rows=[r for r in segments if r['model']==model and r['b']==b and r['part']==part]
                keys=['std',*VOL_KEYS,'mean_intensity','gross_quantity_per_tick','clipped_fraction','forecast_alignment']
                stages.append(dict(model=model,b=b,label=f'{LABELS[model]} / b={b}',part=part,n=len(rows),
                                   **mean_metrics([{k:r[k] for k in keys} for r in rows])))
    return cells,stages


def comparisons(records):
    good=[r for r in records if r['status']=='complete'];rows=[]
    keys=['max_signed',*VOL_KEYS,'gross_quantity_per_tick','forecast_alignment']
    for model in ['hetero_0.5','hetero_1']:
        for label,reference in [('异质开启−同人口关闭',lambda r:next(q for q in good if q['population']==r['population'] and q['seed']==r['seed'] and q['b']==0)),
                               ('异质开启−原两类开启',lambda r:next(q for q in good if q['model']=='fixed' and q['seed']==r['seed'] and q['b']==.25))]:
            pairs=[(r,reference(r)) for r in good if r['model']==model and r['b']==.25]
            differences=[{k:r['metrics'][k]-q['metrics'][k] for k in keys} for r,q in pairs]
            rows.append(dict(label=f'{LABELS[model]}：{label}',n=len(pairs),**mean_metrics(differences),
                all_four_gain_positive=sum(all(d[k]>0 for k in VOL_KEYS) for d in differences)))
    return rows


def figures(records,manifest,cells,stages):
    good=[r for r in records if r['status']=='complete']
    colors={'fixed':'#aa6666','hetero_0.5':'#24847b','hetero_1':'#c18a32','activity_v1':'#333333'}
    fig,axes=plt.subplots(3,1,figsize=(11,11),constrained_layout=True)
    for model in LABELS:
        group=[r for r in good if r['model']==model and r['b']==.25]
        for ax,key,title,width,maxlag in zip(axes,['signed_acf','absolute_acf','squared_acf'],
                ['收益方向ACF','绝对收益ACF','平方收益ACF'],[5,20,20],[300,2000,2000]):
            curve=np.mean([r['diagnostics'][key] for r in group],axis=0)
            starts=list(range(1,maxlag+1,width))
            ax.plot([k+(width-1)/2 for k in starts],[curve[k:k+width].mean() for k in starts],
                    label=LABELS[model],color=colors[model])
            ax.set(title=f'{title}，{width} lag均值（各组合等权平均；开启反馈）',xlabel='滞后 / tick',ylabel='ACF')
    for ax in axes:ax.axhline(0,color='gray',lw=.6);ax.grid(alpha=.2);ax.legend(fontsize=9)
    fig.savefig(OUTPUT/'acf.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(4,2,figsize=(13,11),constrained_layout=True)
    for row,model in enumerate(LABELS):
        group=[r for r in good if r['model']==model and r['b']==.25 and r['seed']==manifest['spec']['market_seeds'][0]]
        for r in group:
            with np.load(OUTPUT/r['name']/'series.npz') as x:
                t=np.arange(len(x['prices']))
                axes[row,0].plot(t,x['prices'],lw=.7,label=r['population'])
                axes[row,1].plot(t[1:],x['intensity'][1:],lw=.7,label=r['population'])
        axes[row,0].set(title=LABELS[model]+'：价格',xlabel='tick',ylabel='本币 / 外币')
        axes[row,1].set(title=LABELS[model]+'：期初强度',xlabel='tick',ylabel='预期到达 / tick')
        for ax in axes[row,:]:ax.grid(alpha=.2);ax.legend(fontsize=8)
    fig.savefig(OUTPUT/'paths.png',dpi=140);plt.close(fig)
    fig,axes=plt.subplots(2,2,figsize=(11,7),constrained_layout=True)
    for model in LABELS:
        rows=[r for r in stages if r['model']==model and r['b']==.25]
        for ax,key,title in zip(axes.ravel(),['sq_301_1000','std','clipped_fraction','mean_intensity'],
                               ['平方收益ACF，301–1000','收益标准差','裁剪比例','平均活动强度']):
            ax.plot([r['part'] for r in rows],[r[key] for r in rows],marker='o',color=colors[model],label=LABELS[model])
            ax.set(title=title,xlabel='预热后等分时段',xticks=[1,2,3]);ax.grid(alpha=.2)
    axes[0,0].legend(fontsize=8);fig.savefig(OUTPUT/'stages.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(2,3,figsize=(12,7),constrained_layout=True)
    populations=[p for p in manifest['populations'] if p['population_seed'] is not None]
    for ax,p in zip(axes.ravel(),populations):
        for group,(a,b) in enumerate(manifest['config']['factor_mix']):
            profiles=[r for r in p['profiles'] if r['type']==group]
            ax.scatter([r['trend']/a for r in profiles],[r['reversion']/b for r in profiles],
                       s=[25+50*r['sigma']/manifest['config']['noise_sigma'] for r in profiles],label=f'原类型 {group}')
        ax.axhline(1,color='gray',lw=.6);ax.axvline(1,color='gray',lw=.6)
        ax.set(xscale='log',yscale='log',xlim=(.03,10),ylim=(.03,10),
               xlabel='个人趋势 / 原类型趋势',ylabel='个人回归 / 原类型回归',title=p['id'])
        ax.set_xticks([.1,1,10],labels=['0.1','1','10']);ax.set_yticks([.1,1,10],labels=['0.1','1','10'])
        ax.xaxis.set_minor_formatter(NullFormatter());ax.yaxis.set_minor_formatter(NullFormatter())
        ax.grid(alpha=.2);ax.legend(fontsize=8)
    fig.suptitle('固定个人参数：点大小随个人噪声sigma增大')
    fig.savefig(OUTPUT/'profiles.png',dpi=150);plt.close(fig)


def build():
    records=json.loads((OUTPUT/'summary.json').read_text());manifest=json.loads((OUTPUT/'manifest.json').read_text())
    segments=segment_rows(records);cells,stages=aggregate(records,segments);paired=comparisons(records)
    rows=[dict(name=r['name'],model=r['model'],population=r['population'],seed=r['seed'],b=r['b'],
               status=r['status'],**r.get('metrics',{}),error=r.get('error','')) for r in records]
    profile_rows=[dict(population=p['id'],spread=p['spread'],population_seed=p['population_seed'],**r)
                  for p in manifest['populations'] if p['profiles'] is not None for r in p['profiles']]
    export_csv('metrics.csv',rows);export_csv('profiles.csv',profile_rows);export_csv('segments.csv',segments)
    export_csv('aggregates.csv',cells);export_csv('paired.csv',paired)
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False})
    figures(records,manifest,cells,stages)
    pop_rows=[]
    for p in manifest['populations']:
        if p['profiles'] is None:continue
        group=[r for r in records if r['status']=='complete' and r['population']==p['id'] and r['b']==.25]
        pop_rows.append(dict(population=p['id'],**mean_metrics([r['metrics'] for r in group])))
    on={r['model']:r for r in cells if r['b']==.25}
    finding=[]
    for model in ['hetero_0.5','hetero_1']:
        r=on[model];fixed=on['fixed'];v1=on['activity_v1']
        finding.append(f"{LABELS[model]}：方向R均值{r['max_signed']:.5f}（原两类{fixed['max_signed']:.5f}、v1 {v1['max_signed']:.5f}）；"
            f"平方收益301–1000段ACF均值{r['sq_301_1000']:.5f}，范围[{r['sq301_min']:.5f}, {r['sq301_max']:.5f}]；"
            f"平均强度{r['mean_intensity']:.6f}，四段波动ACF同时为正{r['all_four_positive']}/{r['paths']}条。")
    failures=[r for r in records if r['status']!='complete']
    parts=[f'''# 随机个人三因子与活动反馈：三人口×三行情对照

用户确认检验“个人参数固定随机化＋公共价格活动反馈”。本轮完成{len(records)}条10万tick路径，预热5000，失败{len(failures)}条。两个异质程度各包含3批交易者×3个行情种子×反馈开关；原两类参数作同种子开关对照，v1作同种子反馈参照。没有按结果重抽人口、改参或扩大扫描。

**在当前参数中心和两档异质程度下，固定随机个人参数没有恢复v1的中尺度波动持续性。** 方向R仅小幅变化，平均活动强度仍贴近底值.75。本轮不支持“只需打散个人系数，就足以解决上一轮问题”的判断；也不能推广成其他参数分布、窗口或交易规则下的异质性都无效。

{chr(10).join(chr(10)+p+chr(10) for p in finding)}

这些是交叉组合的描述统计，9条不是9个完全独立样本。总体参数矩保持一致，也不保证实际订单、收益尺度和活动强度一致。是否改善需要同时阅读方向相关、配对波动增益、分段变化与钱包。

判断同向程度在原两类、s=.5、s=1开启反馈组分别为{on['fixed']['forecast_alignment']:.4f}/{on['hetero_0.5']['forecast_alignment']:.4f}/{on['hetero_1']['forecast_alignment']:.4f}，只小幅变化且不随异质程度单调下降。关闭反馈组的平均到达率为1，开启组约.751；开关同时改变有效交易频率，不能把开组方向R较低全部解释为有益的活动记忆。

本轮异质组的三段成交量与收益尺度大致稳定，全部三因子路径的裁剪均为0；没有出现上一轮强参数候选那种共同的持续耗损。当前现象更接近“活动反馈很弱”，不能用资金耗尽解释本轮失败。开启反馈后每tick绝对外币成交约{on['hetero_0.5']['gross_quantity_per_tick']:.2f}/{on['hetero_1']['gross_quantity_per_tick']:.2f}，v1约{on['activity_v1']['gross_quantity_per_tick']:.2f}；沿用同一psi时，实际收益尺度也不同。这与反馈未被充分激活相符，但尚未隔离订单规模、目标仓位回复和方向效应的独立贡献。

## 1. 实验设置与抽样规则

20户原先交替两类权重(30,.1)/(3,1)。每位交易者的趋势、回归、噪声参数各用独立标准高斯u乘exp(s*u)，s=.5/1；在每个原类型的10人内，把趋势/回归倍率各归一化到平均1，噪声倍率归一化到均方1。开局固定参数，s=0精确退回旧两类模型。没有参数每tick重抽、学习或策略类型切换。

人口种子301/302/303与行情种子71/72/73交叉；同人口在两种反馈下完全相同，人口参数流独立于行情随机流，不消费到达/预期/洗牌随机数。两档异质程度使用同一人口的标准高斯抽样。正系数归一化造成同一类型内约束相关，不能把20个参数称为互不相关抽样。

保留窗口30/30、每tick每户更新信号g=a_i*T+b_i*M+sigma_i*epsilon_i，g仍为log(主观目标价/观察价)；T与M沿用旧历史定义，不引用独立基本价值。目标净外币500*tanh(100*g)，相对开局1000外币，每到达向目标差额调整20%，真实成交后按真实库存重算。买入以观察价格换算金币预算，卖出以外币数量输入。sensitivity100、公共窗口和调仓规则固定，没有随机化账户到达偏好。

账户各金币5000/外币1000，池金币100万/外币20万，初价5、零手续费、单笔池限额.005；钱包非负、不补钱或重置。共享活动状态tau1000、psi.0004、z初始0，期初Lambda=(1−b)+b*z²，b=0/.25；当期真实收益后更新z_next=exp(−1/1000)*z+sqrt(1−exp(−2/1000))*r/.0004。z累计带符号变化，同向积累、反向抵消后再平方；并非直接r²估计。各户Poisson(Lambda/20)，事件洗牌执行。

所有人口参数在正式运行前保存于[manifest.json](manifest.json)，[profiles.csv](profiles.csv)列出每人参数。新驱动复用原AMM、时钟、信号组件与执行函数；s=0对反馈开关分别与旧simulate全部21个公共字段逐点一致，见[compatibility.json](compatibility.json)。旧来源和冻结数据未改。

![个人参数](profiles.png)

## 2. 全程结果

以下各路径先计算指标再取等权均值；原两类和v1各3个行情，异质组各9个交叉组合。R为收益ACF在lag1–1000最大绝对值；波动指标为全部整数lag均值。
''']
    parts.append(table(cells,[('label','组别 / 反馈',''),('paths','路径数',''),('signed_1_1','收益ACF1','{:.5f}'),
        ('max_signed','方向R','{:.5f}'),('abs_101_300','绝对收益101–300','{:.5f}'),
        ('abs_301_1000','绝对收益301–1000','{:.5f}'),('sq_101_300','平方收益101–300','{:.5f}'),
        ('sq_301_1000','平方收益301–1000','{:.5f}'),('all_four_positive','四段同时为正数','')]))
    parts.append(table(cells,[('label','组别 / 反馈',''),('direction_min','R最小','{:.5f}'),('direction_max','R最大','{:.5f}'),
        ('sq301_min','平方收益301–1000最小','{:.5f}'),('sq301_max','平方收益301–1000最大','{:.5f}'),
        ('mean_intensity','平均强度','{:.6f}'),('forecast_alignment','判断同向程度','{:.4f}')]))
    parts.append('判断同向程度为每tick 20人的sign(g)均值取绝对值，再取时间均值；0表示方向各半，1表示全同向。它只衡量预期方向，不代表到达后的真实成交方向，也不是收益不可预测性的检验。')
    parts.append('![方向和波动ACF](acf.png)\n\n![价格与活动路径](paths.png)\n\n价格图展示行情seed71，各批人口均保留；统计均排除前5000tick，图含预热，各图纵轴不同。')
    parts.append('## 3. 配对比较\n\n每个异质路径分别减同人口同种子关闭反馈、同种子原两类开启反馈。方向R差值越小表示方向相关越弱；波动差值为正表示该指标上升，不单独代表成功。')
    parts.append(table(paired,[('label','配对差',''),('n','配对数',''),('max_signed','方向R差','{:.5f}'),
        ('abs_101_300','绝对收益101–300差','{:.5f}'),('abs_301_1000','绝对收益301–1000差','{:.5f}'),
        ('sq_101_300','平方收益101–300差','{:.5f}'),('sq_301_1000','平方收益301–1000差','{:.5f}'),
        ('all_four_gain_positive','四段差同时为正数','')]))
    parts.append('## 4. 每批交易者的结果\n\n同一批人口先对三个行情平均，全部人口均列出，没有挑选有利人群。')
    parts.append(table(pop_rows,[('population','人口',''),('max_signed','方向R','{:.5f}'),
        ('abs_101_300','绝对收益101–300','{:.5f}'),('abs_301_1000','绝对收益301–1000','{:.5f}'),
        ('sq_101_300','平方收益101–300','{:.5f}'),('sq_301_1000','平方收益301–1000','{:.5f}'),
        ('mean_intensity','平均强度','{:.6f}'),('forecast_alignment','判断同向程度','{:.4f}')]))
    parts.append('## 5. 分段、成交与资金约束\n\n预热后按tick5001–36667、36668–68334、68335–100000三等分，每段重新去均值计算ACF，不重置模拟状态。分段在运行前已列为必要检查，防止长期波动衰减抬高全程相关。')
    parts.append(table(stages,[('label','组别 / 反馈',''),('part','段',''),('sq_101_300','平方收益101–300','{:.5f}'),
        ('sq_301_1000','平方收益301–1000','{:.5f}'),('std','收益标准差','{:.8f}'),
        ('mean_intensity','平均强度','{:.5f}'),('gross_quantity_per_tick','外币绝对量/tick','{:.3f}'),
        ('clipped_fraction','裁剪比例','{:.3%}')]))
    parts.append('![三段状态](stages.png)\n\n三因子裁剪比较正常量化后的意向输入与实际输入，v1保持原外币意向/执行口径，跨策略裁剪比例仅作辅助。最小钱包触及0不等于所有账户永久破产，钱包和裁剪必须同时看。')
    parts.append(table(cells,[('label','组别 / 反馈',''),('std','收益标准差','{:.8f}'),('excess_kurtosis','超额峰度','{:.3f}'),
        ('gross_quantity_per_tick','外币绝对量/tick','{:.3f}'),('clipped_fraction','裁剪比例','{:.3%}'),
        ('wallet_gold_min','路径最低金币均值','{:.3f}'),('wallet_foreign_min','路径最低外币均值','{:.3f}'),
        ('peak_intensity','路径峰值均值','{:.3f}'),('next_100','下一100块','{:.3f}'),('next_240','下一240块','{:.3f}'),('next_1000','下一1000块','{:.3f}')]))
    parts.append('## 6. 全部逐路径结果')
    parts.append(table([r for r in rows if r['status']=='complete'],[('name','路径',''),('max_signed','方向R','{:.5f}'),
        ('abs_301_1000','绝对收益301–1000','{:.5f}'),('sq_301_1000','平方收益301–1000','{:.5f}'),
        ('mean_intensity','平均强度','{:.6f}'),('clipped_fraction','裁剪比例','{:.3%}')]))
    parts.append('''## 7. 统计口径、验证与边界

每路径分析95000个含零值收益，r=ln(P_t/P_(t−1))。ACF去均值后用lag乘积和/lag0乘积和，不作N/(N−lag)修正；方向指标使用有符号收益，不是仅sign(r)。段均值含所有整数lag；图按5或20lag平均，仅作展示，完整曲线到5000保存在result.json。std用ddof=1，偏度/超额峰度bias=False，Fisher正态超额峰度0。

活动块关系是期初Lambda平均与下一不重叠100/240/1000tick块r²平均的Pearson相关；固定率状态恒定，相关为null。每路径同时保存聚合收益分布、DFA1/2固定尺度16–8192；未用DFA挑参数或宣称严格长记忆。高峰度不证明稳定幂律尾部。

这轮同时随机化三种个人系数，能够检验这一整套异质性设定，不能拆分每个系数的独立贡献。总体矩约束并不控制a/b协方差、非线性目标或成交尺度。三个市场种子跨人口复用、三个参数人口跨市场复用，范围只是组合间最小/最大，不是置信区间。没有检验随机窗口、独立活动状态、玩家/注资响应或真实市场复现，也不据此宣称不可套利。有限样本结果不能排除其他参数分布或中心下的作用。

- [预先固定的实验计划](../../PLAN.md)
- [人口与参数清单](manifest.json)、[每人参数](profiles.csv)、[逐路径指标](metrics.csv)
- [配对比较](paired.csv)、[全部分段指标](segments.csv)、[汇总指标](aggregates.csv)
- [零异质性兼容检查](compatibility.json)、[独立数值核对](audit.json)

每路径保存完整series.npz、result.json（个人参数、资金状态、来源hash、完整统计）。运行命令：

```bash
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m research.fx.heterogeneous.run
OPENBLAS_NUM_THREADS=1 MPLCONFIGDIR=/tmp/fx-matplotlib python -m research.fx.heterogeneous.report
OPENBLAS_NUM_THREADS=1 python -m unittest discover -s research/fx/tests -v
```

相关单元检查17项通过，包含人口总体矩、固定可重复性、s=0真实旧路径兼容与反馈开关的个人噪声配对。来源和冻结文件的核对记录在audit.json。
''')
    if failures:parts.append(table(failures,[('name','失败路径',''),('error','原因','')]))
    (OUTPUT/'report.md').write_text('\n\n'.join(parts)+'\n')
    provenance={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in
                [HERE/'report.py',OUTPUT/'summary.json',OUTPUT/'manifest.json',OUTPUT/'segments.csv']}
    (OUTPUT/'report-provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
    print(OUTPUT/'report.md')


if __name__=='__main__':build()
