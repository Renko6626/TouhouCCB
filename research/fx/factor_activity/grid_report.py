"""All-cell screening evidence, frozen shortlist and independent-seed results."""
import csv
import hashlib
import json

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.colors import TwoSlopeNorm
import numpy as np

from research.fx.activity.report import table
from research.fx.common.diagnostics import acf
from .grid import OUTPUT,VOL_KEYS,HERE,ROOT


def read(name):
    return json.loads((OUTPUT/name).read_text())


def export_csv(name,rows):
    keys=list(dict.fromkeys(k for r in rows for k in r))
    with (OUTPUT/name).open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=keys);writer.writeheader();writer.writerows(rows)


def flat_result(r):
    return dict(stage=r['stage'],id=r['id'],case=r['case'],seed=r['seed'],status=r['status'],
                **r['parameters'],**r.get('metrics',{}),error=r.get('error',''))


def avg_reference(records):
    metrics=[r['metrics'] for r in records if r['id']=='activity_v1' and r['status']=='complete']
    return {key:float(np.mean([r[key] for r in metrics]))
            if all(r.get(key) is not None for r in metrics) else None for key in metrics[0]} if metrics else {}


def segments(records):
    rows=[]
    for record in records:
        if record['status']!='complete':continue
        directory=OUTPUT/'validation'/f"{record['id']}-{record['case']}-seed{record['seed']}"
        cfg=record['config']
        with np.load(directory/'series.npz') as x:
            for part,indices in enumerate(np.array_split(np.arange(cfg['burn_in']+1,cfg['ticks']+1),3),1):
                r=x['returns'][indices];a=acf(abs(r),1000);s=acf(r*r,1000)
                arrivals=x['arrivals'][indices].sum()
                rows.append(dict(id=record['id'],case=record['case'],seed=record['seed'],part=part,
                    start=int(indices[0]),end=int(indices[-1]),n=len(r),std=float(r.std(ddof=1)),
                    abs_101_300=float(a[101:301].mean()),abs_301_1000=float(a[301:1001].mean()),
                    sq_101_300=float(s[101:301].mean()),sq_301_1000=float(s[301:1001].mean()),
                    mean_intensity=float(x['intensity'][indices].mean()),
                    gross_quantity_per_tick=float(x['executed_abs_quantity'][indices].mean()),
                    clipped_fraction=float(x['clipped_orders'][indices].sum()/arrivals) if arrivals else None,
                    wallet_gold_min=float(x['wallet_gold_min'][indices].min()),
                    wallet_foreign_min=float(x['wallet_foreign_min'][indices].min())))
    return rows


def segment_means(rows,ids):
    output=[]
    for cell in ids:
        for case in ['factor_fixed','factor_activity','activity_v1']:
            for part in [1,2,3]:
                group=[r for r in rows if r['id']==cell and r['case']==case and r['part']==part]
                if group:
                    output.append(dict(id=cell,case=case,part=part,
                        **{k:float(np.mean([r[k] for r in group])) for k in
                           ['std','abs_101_300','abs_301_1000','sq_101_300','sq_301_1000',
                            'mean_intensity','gross_quantity_per_tick','clipped_fraction']}))
    return output


def plot_screen(rows,manifest,selection):
    spec=manifest['spec'];valid=[r for r in rows if r['status']=='complete']
    selected={r['id'] for r in selection['candidates']}
    fig,ax=plt.subplots(figsize=(10,6),constrained_layout=True)
    for noise,color in zip(spec['noise_sigmas'],['#247d8c','#d18b33','#915c9e']):
        group=[r for r in valid if r['parameters']['noise_sigma']==noise]
        ax.scatter([r['max_signed'] for r in group],[r['volatility_min'] for r in group],
                   color=color,alpha=.7,s=30,label=f'noise sigma={noise}')
    for row in valid:
        if row['id'] in selected:
            ax.scatter(row['max_signed'],row['volatility_min'],s=100,facecolors='none',edgecolors='black')
            ax.annotate(row['id'],(row['max_signed'],row['volatility_min']),xytext=(4,4),textcoords='offset points',fontsize=8)
    ax.axhline(0,color='gray',lw=.8)
    ax.set(xlabel='收益ACF最大绝对值（lag 1–1000；越小越好）',
           ylabel='四段波动ACF均值中的最小值（越大越好）',title='162组初筛：波动持续性与方向相关的取舍')
    ax.grid(alpha=.2);ax.legend()
    fig.savefig(OUTPUT/'screen-tradeoff.png',dpi=150);plt.close(fig)
    lookup={(p['trend_window'],p['mean_window'],p['noise_sigma'],p['adjustment'],
             p['trend_multiplier'],p['reversion_multiplier']):r
            for r in rows for p in [r['parameters']]}
    for adjustment in spec['adjustments']:
        for metric,title in [('volatility_min','四段波动ACF的最小值'),('max_signed','方向ACF最大绝对值')]:
            values=[r[metric] for r in valid]
            if metric=='volatility_min':
                scale=max(max(abs(v) for v in values),1e-6)
                norm=TwoSlopeNorm(vmin=-scale,vcenter=0,vmax=scale);cmap='RdBu_r'
            else:
                norm=plt.Normalize(0,max(max(values),1e-6));cmap='YlOrRd'
            fig,axes=plt.subplots(3,3,figsize=(11,10),constrained_layout=True)
            for i,(tw,mw) in enumerate(spec['window_pairs']):
                for j,noise in enumerate(spec['noise_sigmas']):
                    matrix=np.full((3,3),np.nan);ids={}
                    for y,t in enumerate(spec['trend_multipliers']):
                        for x,m in enumerate(spec['reversion_multipliers']):
                            r=lookup[tw,mw,noise,adjustment,t,m];ids[y,x]=r['id']
                            if r['status']=='complete':matrix[y,x]=r[metric]
                    ax=axes[i,j];im=ax.imshow(matrix,norm=norm,cmap=cmap,origin='lower')
                    for y in range(3):
                        for x in range(3):
                            text=f'{matrix[y,x]:.3f}' if np.isfinite(matrix[y,x]) else '失败'
                            if ids[y,x] in selected:text+='*'
                            ax.text(x,y,text,ha='center',va='center',fontsize=9,
                                    bbox=dict(facecolor='white',alpha=.65,edgecolor='none',pad=1))
                    ax.set(xticks=range(3),xticklabels=spec['reversion_multipliers'],
                           yticks=range(3),yticklabels=spec['trend_multipliers'],
                           xlabel='回归权重倍率',ylabel='趋势权重倍率',
                           title=f'窗口 {tw}/{mw}，噪声 {noise}')
            fig.suptitle(f'初筛：{title}；调仓比例 {adjustment}（*为冻结候选）')
            fig.colorbar(im,ax=axes.ravel().tolist(),shrink=.7)
            fig.savefig(OUTPUT/f'screen-{metric}-a{adjustment}.png',dpi=140);plt.close(fig)


def plot_validation(records,selection,seeds):
    good=[r for r in records if r['status']=='complete']
    ids=[c['id'] for c in selection['candidates']]+['activity_v1']
    colors={key:plt.cm.tab10(i%10) for i,key in enumerate(ids)};colors['activity_v1']='black'
    fig,axes=plt.subplots(3,1,figsize=(11,11),constrained_layout=True)
    for cell in ids:
        group=[r for r in good if r['id']==cell and r['case']!='factor_fixed']
        if not group:continue
        for ax,key,title,maxlag,width in zip(axes,['signed_acf','absolute_acf','squared_acf'],
                                            ['收益方向ACF','绝对收益ACF','平方收益ACF'],[300,2000,2000],[5,20,20]):
            curve=np.mean([r['diagnostics'][key] for r in group],axis=0)
            starts=range(1,maxlag+1,width)
            ax.plot([k+(width-1)/2 for k in starts],[curve[k:k+width].mean() for k in starts],
                    color=colors[cell],label=cell,lw=1.2)
            ax.set(title=f'{title}（{width} lag平均；三个新种子均值）',xlabel='滞后 / tick',ylabel='ACF')
    for ax in axes:ax.axhline(0,color='gray',lw=.7);ax.grid(alpha=.2);ax.legend(ncol=4,fontsize=8)
    fig.savefig(OUTPUT/'validation-acf.png',dpi=150);plt.close(fig)
    nrows=(len(ids)+1)//2
    fig,axes=plt.subplots(nrows,2,figsize=(13,nrows*2.8),constrained_layout=True,squeeze=False)
    for ax,cell in zip(axes.ravel(),ids):
        for case,style,label in [('factor_fixed','--','关闭反馈'),('factor_activity','-','开启反馈'),('activity_v1','-','v1')]:
            group=[r for r in good if r['id']==cell and r['case']==case and r['seed']==seeds[0]]
            if group:
                r=group[0]
                with np.load(OUTPUT/'validation'/f"{cell}-{case}-seed{r['seed']}"/'series.npz') as x:
                    ax.plot(np.arange(len(x['prices'])),x['prices'],style,lw=.7,label=label)
        ax.set(title=f'{cell} / seed {seeds[0]}',xlabel='tick',ylabel='本币 / 外币');ax.grid(alpha=.2);ax.legend(fontsize=8)
    for ax in axes.ravel()[len(ids):]:ax.set_visible(False)
    fig.savefig(OUTPUT/'validation-paths.png',dpi=140);plt.close(fig)


def build():
    manifest=read('manifest.json');selection=read('selection.json')
    screen=read('screen-summary.json');screen_rows=read('screen-aggregates.json')
    validation=read('validation-summary.json');validation_rows=read('validation-aggregates.json')
    ids=[c['id'] for c in selection['candidates']]
    rank={key:i for i,key in enumerate(ids)};validation_rows.sort(key=lambda r:rank[r['id']])
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False})
    export_csv('screen-paths.csv',[flat_result(r) for r in screen])
    export_csv('validation-paths.csv',[flat_result(r) for r in validation])
    export_csv('screen-parameters.csv',[dict(id=r['id'],**r['parameters'],status=r['status'],
        **{k:r.get(k) for k in ['volatility_min','feedback_gain_min','max_signed','all_four_positive','all_four_gain_positive']}) for r in screen_rows])
    plot_screen(screen_rows,manifest,selection)
    plot_validation(validation,selection,manifest['spec']['validation']['seeds'])
    ref=avg_reference(validation);complete=[r for r in validation_rows if r['status']=='complete']
    segment_rows=segments(validation)
    export_csv('segments.csv',segment_rows)
    segment_average=segment_means(segment_rows,ids+['activity_v1'])
    failures=[r for r in screen+validation if r['status']!='complete']
    evidence=[]
    for row in complete:
        on=row['on'];off=row['off']
        evidence.append(dict(id=row['id'],r1=on['signed_1_1'],R=on['max_signed'],
            a101=on['abs_101_300'],a301=on['abs_301_1000'],s101=on['sq_101_300'],s301=on['sq_301_1000'],
            off_s301=off['sq_301_1000'],V=row['volatility_min'],D=row['feedback_gain_min'],
            positive=row['all_four_positive'],gain_positive=row['all_four_gain_positive']))
    if ref:
        evidence.append(dict(id='activity_v1',r1=ref['signed_1_1'],R=ref['max_signed'],a101=ref['abs_101_300'],
            a301=ref['abs_301_1000'],s101=ref['sq_101_300'],s301=ref['sq_301_1000'],off_s301=None,V=None,D=None,positive=None,gain_positive=None))
    params=[dict(id=c['id'],**next(r['parameters'] for r in screen_rows if r['id']==c['id']),
                 reason=', '.join(c['reasons'])) for c in selection['candidates']]
    training=[dict(id=r['id'],V=r.get('volatility_min'),D=r.get('feedback_gain_min'),R=r.get('max_signed'))
              for r in screen_rows if r['id'] in ids]
    count_pos=sum(r['all_four_positive']==3 for r in complete)
    count_gain=sum(r['all_four_gain_positive']==3 for r in complete)
    best_gain=max(complete,key=lambda r:r['feedback_gain_min']) if complete else None
    findings=[]
    if best_gain and ref:
        r=best_gain;on=r['on'];off=r['off']
        findings.append(f"**扫参找到了更大的全程波动相关，同时暴露了方向相关的代价。** 配置{r['id']}在验证中，"
            f"平方收益301–1000段ACF为{on['sq_301_1000']:.5f}，关闭反馈为{off['sq_301_1000']:.5f}，"
            f"四段增益在{r['all_four_gain_positive']}/3种子中同时为正；但收益ACF1为{on['signed_1_1']:.5f}，"
            f"方向R为{on['max_signed']:.5f}。同种子v1分别为平方收益ACF {ref['sq_301_1000']:.5f}、"
            f"方向R {ref['max_signed']:.5f}。不能只凭波动相关更强就将它升级为替代基线。")
        thirds=[v for v in segment_average if v['id']==r['id'] and v['case']=='factor_activity']
        if len(thirds)==3:
            first,last=thirds[0],thirds[-1]
            findings.append(f"**{r['id']}还存在明显的运行退化与非平稳混杂。** 预热后的前/中/后三段平方收益301–1000段ACF分别为"
                f"{thirds[0]['sq_301_1000']:.5f}/{thirds[1]['sq_301_1000']:.5f}/{thirds[2]['sq_301_1000']:.5f}；"
                f"收益标准差由{first['std']:.6f}降到{last['std']:.6f}，"
                f"裁剪比例由{first['clipped_fraction']:.1%}升到{last['clipped_fraction']:.1%}。"
                "最小金币钱包曾触及0（这不等于所有账户永久破产）。全程ACF可能混入波动水平缓慢下降的影响，"
                "不能把0.04全部解释为平稳的活动反馈聚集；本轮也没有量化各因素的独立贡献。")
        weaker=[r['id'] for r in complete if r['on']['sq_301_1000']<r['off']['sq_301_1000']
                and r['id']!=manifest['baseline_id']]
        if weaker:
            findings.append(f"另外，{', '.join(weaker)}的验证平方收益301–1000段相关，在关闭反馈时反而更强。"
                "它们的波动持续性也可能来自三因子与调仓本身，不能全部记为活动反馈的功劳。"
                "初筛排名与独立验证出现差异，报告保留全部候选，不按验证结果重选。")
        findings.append("本轮经过验证的候选尚未同时满足持续性、与v1相近的低方向R和无明显运行退化。"
            "这不否定整个参数空间；未入选的158组只有短路径初筛证据，不能据此宣布全部三因子模型失败。")
    parts=[f'''# 三因子参数网格：初筛与独立种子验证

本轮扫描162组参数，每组用种子51/52比较反馈开关，共{len(screen)}条25000tick路径；预热5000。按事先确定的三种排序冻结{len(ids)}个候选（含原参数），再用61/62/63三个新种子、每条100000tick验证，同时重跑同种子v1参照，共{len(validation)}条验证路径。本轮不调整活动反馈的psi、b或tau。

验证中，{len(complete)}个完整候选里，三个种子四段波动ACF均为正的有 **{count_pos}组**；三个种子四段ACF都高于同参数关闭反馈对照的有 **{count_gain}组**。这两项只描述重复性，不是显著性门槛；是否值得保留还必须看方向相关和成交规模。全部{len(screen)+len(validation)}条路径中失败{len(failures)}条。

{chr(10).join(chr(10)+p+chr(10) for p in findings)}

## 1. 新种子的结果

以下先对各lag段取平均，再对三个种子取平均；R是每条路径收益ACF在lag1–1000的最大绝对值，再取种子均值。开启反馈的四项指标必须同时阅读。关闭反馈r²301–1000单列作为配对参照，其余完整指标见CSV。
''']
    parts.append(table(evidence,[('id','配置',''),('r1','r ACF1','{:.5f}'),('R','R','{:.5f}'),
        ('a101','绝对收益101–300','{:.5f}'),('a301','绝对收益301–1000','{:.5f}'),
        ('s101','平方收益101–300','{:.5f}'),('s301','平方收益301–1000','{:.5f}'),('off_s301','关闭反馈平方收益301–1000','{:.5f}')]))
    parts.append(table(evidence,[('id','配置',''),('V','V：四项最小均值','{:.5f}'),('D','D：四项配对增益最小均值','{:.5f}'),
                                ('positive','四项为正的种子数 / 3',''),('gain_positive','四项增益为正的种子数 / 3','')]))
    parts.append('''
![验证ACF](validation-acf.png)

![验证价格路径](validation-paths.png)

价格图只展示验证seed61；各图纵轴不同，不能只凭视觉振幅比较。曲线包括完整预热段，统计均排除前5000tick。反馈开关后的价格、事件数和随机数消费会分叉，不作逐笔因果解释。

## 2. 候选参数和初筛依据

趋势/回归倍率分别乘旧的两类账户权重[(30,0.1),(3,1)]；个人噪声是log价格预期的标准差。三因子仍通过500*tanh(100*g)给出目标净外币，每次到达调整指定比例，净仓位相对开局1000外币。没有恢复锁仓或成绩切换。
''')
    parts.append(table(params,[('id','配置',''),('trend_multiplier','趋势倍率',''),('reversion_multiplier','回归倍率',''),
        ('noise_sigma','噪声sigma',''),('trend_window','趋势窗口',''),('mean_window','均价窗口',''),('adjustment','调仓比例',''),('reason','入选排序','')]))
    parts.append('初筛依次按V、D、V/(R+0.000001)从大到小排序，每项各取前2，合并去重，再补原参数。排序未设置物理通过线，负V的比值也仍按原公式排序；不将该比值当作独立经济指标。')
    parts.append(table(training,[('id','配置',''),('V','初筛V','{:.5f}'),('D','初筛D','{:.5f}'),('R','初筛R','{:.5f}')]))
    parts.append('''
## 3. 全网格结果

趋势/回归各[0.1,1,10]倍，噪声[0.0005,0.002,0.008]，窗口(30,30)/(10,100)/(100,1000)，调仓[0.2,0.5]，全因子162组。窗口为预先选定的三种组合，本轮未穷举趋势窗口与均价窗口的全部交叉，也未扫描仓位上限、账户数或钱包规模。

![全部初筛配置的取舍](screen-tradeoff.png)

初筛热图：星号标记进入独立验证的配置，所有格点保留。每个指标的色标在两种调仓比例下保持一致。
''')
    for adjustment in manifest['spec']['adjustments']:
        parts.append(f'\n![波动热图，调仓{adjustment}](screen-volatility_min-a{adjustment}.png)\n\n![方向热图，调仓{adjustment}](screen-max_signed-a{adjustment}.png)\n')
    parts.append('''
## 4. 活动、成交与资金约束

下表均为预热后种子均值，峰值为全程各路径峰值的平均，不能当作模型上限。三因子裁剪比较已量化订单输入与实际输入，v1保持原裁剪口径；跨策略裁剪比例仅作辅助阅读。完整钱包状态在每路径result.json，最小钱包序列和资产池序列在series.npz。
''')
    execution=[]
    for row in complete:
        for tag in ['off','on']:
            execution.append(dict(id=row['id'],feedback=tag,**row[tag]))
    if ref:execution.append(dict(id='activity_v1',feedback='on',**ref))
    parts.append(table(execution,[('id','配置',''),('feedback','反馈',''),('mean_intensity','平均强度','{:.4f}'),
        ('peak_intensity','路径峰值均值','{:.3f}'),('gross_quantity_per_tick','外币绝对量/tick','{:.3f}'),
        ('clipped_fraction','裁剪比例','{:.3%}'),('std','收益标准差','{:.8f}'),('excess_kurtosis','超额峰度','{:.3f}'),
        ('next_100','下一100块','{:.3f}'),('next_240','下一240块','{:.3f}'),('next_1000','下一1000块','{:.3f}')]))
    parts.append('''
### 分段检查

查看价格图发现g069振幅随时间减小后，统一对全部验证路径补做相同的三等分分析，没有据此修改参数或候选。分析段为tick5001–36667、36668–68334、68335–100000；每段重新去均值计算ACF，没有重置模拟状态。下表先计算每条路径的段指标再平均三个种子；它不能独立证明非平稳的具体成因，但能检查全程ACF是否掩盖了阶段变化。
''')
    parts.append(table(segment_average,[('id','配置',''),('case','模型',''),('part','段',''),('std','收益标准差','{:.7f}'),
        ('sq_101_300','平方收益101–300','{:.5f}'),('sq_301_1000','平方收益301–1000','{:.5f}'),
        ('mean_intensity','平均强度','{:.3f}'),('gross_quantity_per_tick','外币量/tick','{:.2f}'),
        ('clipped_fraction','裁剪比例','{:.2%}')]))
    parts.append('''
## 5. 逐种子结果
''')
    paths=[dict(id=r['id'],case=r['case'],seed=r['seed'],**r.get('metrics',{})) for r in validation if r['status']=='complete']
    parts.append(table(paths,[('id','配置',''),('case','模型',''),('seed','种子',''),('max_signed','R','{:.5f}'),
        ('abs_101_300','绝对收益101–300','{:.5f}'),('abs_301_1000','绝对收益301–1000','{:.5f}'),
        ('sq_101_300','平方收益101–300','{:.5f}'),('sq_301_1000','平方收益301–1000','{:.5f}')]))
    parts.append('''
## 6. 实验设置与统计口径

20个账户各本币5000/外币1000，池本币100万/外币20万，初价5、零手续费、每单池限额0.005；不补钱、不重置钱包。活动状态tau1000、psi0.0004，b=0/.25，初始z=0。个人预期每户每tick更新，同tick重复到达仍按真实库存重新调仓；公开价格读取、窗口定义与[上一轮](../comparison/report.md)一致。

初筛分析20000个收益；验证分析95000个收益。r=ln(P_t/P_(t−1))，包含零收益；ACF去均值后用lag乘积和/lag0乘积和，不作N/(N−lag)修正。指标区间包含全部整数lag。方向ACF涉及有符号收益本身，并非仅对sign(r)计算。超额峰度采用Fisher定义及bias=False。下一块关系是期初强度块均值与下一不重叠块r²均值的Pearson相关，常数状态未定义；不是涨跌预测或交易收益。

初筛只计算ACF、矩和块关系；验证额外保存完整lag1–5000曲线、聚合分布、DFA1/2。DFA沿用公共固定尺度16–8192；本轮不按DFA挑选参数，也不据此宣称幂律长记忆。完整结果在每条路径result.json。

筛选使用两个种子的均值，会受到多重比较和罕见路径影响。候选由[selection.json](selection.json)在验证前冻结，含初筛文件hash；验证种子没有参与选参。三个验证种子的范围不能替代置信区间，未经过验证的网格点不作长期结论。只有配对反馈开关比较能较直接判断活动反馈增益，和v1比较同时改变了订单规则。有限样本方向ACF小也不能证明不可预测或不可套利。

## 7. 文件和复算

- [网格/源代码清单](manifest.json)
- [全部初筛路径指标](screen-paths.csv)、[全部参数及筛选指标](screen-parameters.csv)
- [全部验证路径指标](validation-paths.csv)、[冻结候选](selection.json)
- [全部验证路径的三段指标](segments.csv)
- [独立数值核对](audit.json)
- [计划与选择规则](../../GRID_PLAN.md)

每条路径目录保存series.npz、result.json及来源SHA256；失败路径保留异常，不隐去格点。既有冻结v1、common、archive和生产市场未修改。单元检查命令：`OPENBLAS_NUM_THREADS=1 python -m unittest discover -s research/fx/tests -v`，15项通过。

```bash
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m research.fx.factor_activity.grid
OPENBLAS_NUM_THREADS=1 MPLCONFIGDIR=/tmp/fx-matplotlib python -m research.fx.factor_activity.grid_report
```
''')
    if failures:parts.append('\n失败路径：\n\n'+table(failures,[('stage','阶段',''),('id','配置',''),('case','模型',''),('seed','种子',''),('error','原因','')]))
    (OUTPUT/'report.md').write_text('\n\n'.join(parts)+'\n')
    provenance={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in [HERE/'grid_report.py',OUTPUT/'manifest.json',OUTPUT/'selection.json',
                          OUTPUT/'screen-summary.json',OUTPUT/'validation-summary.json']}
    (OUTPUT/'report-provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
    print(OUTPUT/'report.md')


if __name__=='__main__':build()
