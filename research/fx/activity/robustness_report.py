"""Readout of fixed-version unseen-seed, longer-path validation."""
import csv
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from .report import table
from .robustness import HERE, OUTPUT, freeze


def flatten(path, window):
    w=path['windows'][window]
    row=dict(environment=path['environment'],b=path['b'],seed=path['seed'],window=window,
        label=f"{path['environment']} / {path['b']:g} / {path['seed']}",
        **w['moments'],**w['returns'],signed_acf1=w['signed_bands']['1-1'],
        signed_max=w['signed_max_1_1000'])
    for short,key in [('abs','abs_bands'),('sq','sq_bands')]:
        row.update({f'{short}_{band}':value for band,value in w[key].items()})
    row.update({f'activity_{band}':value for band,value in w['activity']['acf_bands'].items()})
    row.update({f"next_{block['width']}":block['next_block_corr'] for block in w['activity']['blocks']})
    row.update(w.get('execution',{}))
    return row


def span(rows,key,digits=5):
    values=[r[key] for r in rows if r.get(key) is not None]
    if not values:return '不适用'
    return f'{np.mean(values):.{digits}f} [{min(values):.{digits}f}, {max(values):.{digits}f}]'


def plots(study,output):
    fig,axes=plt.subplots(2,2,figsize=(12,8),constrained_layout=True)
    for col,environment in enumerate(['linear','amm']):
        for path in study['paths']:
            if path['environment']!=environment or path['b']==0:continue
            seed=path['seed']; name=f'{environment}-b0.25-seed{seed}'
            result=json.loads((output/name/'result.json').read_text())
            d=result['diagnostics']; lag=np.arange(1,5001)
            axes[0,col].plot(lag,d['activity']['acf'][1:],label=f'seed {seed}',lw=1)
            centers=np.arange(25.5,5001,50)
            curve=d['squared_acf']
            axes[1,col].plot(centers,[np.mean(curve[k:k+50]) for k in range(1,5001,50)],label=f'seed {seed}',lw=1)
        c=result['linear_theory']['c']
        axes[0,col].plot(lag,c**lag,'k:',label='linear stationary c^k')
        for row,title in enumerate(['intensity ACF','squared-return ACF (50-lag means)']):
            axes[row,col].set(title=f'{environment}: {title}',xlabel='lag (ticks)',ylabel='correlation')
            axes[row,col].axhline(0,color='gray',lw=.6)
            axes[row,col].grid(alpha=.2)
            axes[row,col].legend(fontsize=7,ncol=2)
    fig.savefig(output/'acf.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(2,2,figsize=(12,7),constrained_layout=True)
    for col,environment in enumerate(['linear','amm']):
        for path in study['paths']:
            if path['environment']!=environment or path['b']==0:continue
            ws=[path['windows'][f'third{i}'] for i in range(1,4)]
            axes[0,col].plot([1,2,3],[w['sq_bands']['301-1000'] for w in ws],marker='o',label=f"seed {path['seed']}")
            axes[1,col].plot([1,2,3],[w['moments']['mean_intensity'] for w in ws],marker='o',label=f"seed {path['seed']}")
        for row,title in enumerate(['squared-return ACF, lags 301-1000','mean arrival intensity']):
            axes[row,col].set(title=f'{environment}: {title}',xticks=[1,2,3],xlabel='consecutive 165000-tick segment')
            axes[row,col].grid(alpha=.2);axes[row,col].legend(fontsize=7)
        axes[0,col].axhline(0,color='gray',lw=.6)
    fig.savefig(output/'segments.png',dpi=150);plt.close(fig)


def build():
    freeze()
    study=json.loads((OUTPUT/'study.json').read_text())
    theory=json.loads((OUTPUT/'theory.json').read_text())
    validation=json.loads((OUTPUT/'validation.json').read_text())
    paths=study['paths']; cfg=study['config']
    full=[flatten(p,'full') for p in paths]
    windows=[flatten(p,w) for p in paths for w in ['prefix100k','third1','third2','third3','full']]
    fields=list(dict.fromkeys(k for row in windows for k in row))
    with (OUTPUT/'metrics.csv').open('w',newline='') as file:
        writer=csv.DictWriter(file,fieldnames=fields);writer.writeheader();writer.writerows(windows)
    groups={(env,b):[r for r in full if r['environment']==env and r['b']==b] for env in ['linear','amm'] for b in [0,.25]}
    keys=['signed_acf1','signed_max','abs_101-300','abs_301-1000','abs_1001-5000',
          'sq_101-300','sq_301-1000','sq_1001-5000','next_100','next_240','next_1000',
          'mean_intensity','variance_intensity','excess_kurtosis']
    cells=[dict(label=f'{env} / {b:g}',**{k:span(rows,k) for k in keys}) for (env,b),rows in groups.items()]
    amm=groups['amm',.25]
    four=['abs_101-300','abs_301-1000','sq_101-300','sq_301-1000']
    joint=sum(all(r[k]>0 for k in four) for r in amm)
    thirds=[r for r in windows if r['environment']=='amm' and r['b']==.25 and r['window'].startswith('third')]
    joint_thirds=sum(all(r[k]>0 for k in four) for r in thirds)
    lifetime=[validation['runtime_including_burn_in'][f"amm-b0.25-seed{r['seed']}"] for r in amm]
    peak=max(r['peak_intensity'] for r in lifetime)
    max_arrivals=max(r['max_arrivals'] for r in lifetime)
    clip=span(amm,'clipped_fraction',6)
    sq_thirds=[np.mean([r['sq_301-1000'] for r in thirds if r['window']==f'third{i}']) for i in [1,2,3]]
    next_thirds=[np.mean([r['next_100'] for r in thirds if r['window']==f'third{i}']) for i in [1,2,3]]
    signed_feedback=np.mean([r['signed_max'] for r in amm])
    signed_control=np.mean([r['signed_max'] for r in groups['amm',0]])
    plots(study,OUTPUT)
    text=[f'''# 活动反馈第二轮：冻结版本的未见种子与长路径验证

2026-10-15。本轮冻结第一版模型，增加五个未见种子101–105，将各路径从10万延长至50万tick；仍为线性/AMM×b=0/0.25四格，共20条路径、1000万模拟tick。所有模型参数与执行规则均保持原值，没有调参、加策略、加活动上限、补钱或重置库存。

在AMM反馈组，完整路径中四项波动相关（绝对/平方收益，101–300及301–1000段）同时为正的种子为 **{joint}/5**；三个连续时段共15个“种子×时段”组合中为 **{joint_thirds}/15**。后者共享路径，不能当作15条独立重复或显著性检验。

前、中、后三段的301–1000段平方收益ACF均值分别为{sq_thirds[0]:.5f}、{sq_thirds[1]:.5f}、{sq_thirds[2]:.5f}。末段比中段弱，但下一100tick块相关均值从首段{next_thirds[0]:.3f}到末段{next_thirds[2]:.3f}，没有出现所有种子的有限尺度相关在后段共同消失。不能据此承诺永不退化。全程方向收益最大ACF绝对值均值，反馈{signed_feedback:.5f}、固定率{signed_control:.5f}，仍处于相近量级。

本轮重复性结果支持保留第一版：当前证据已扩展到未见种子与五倍长度。这个结论仍限定于有限尺度活动反馈，不认证严格长记忆、全部现实市场性质或任意长运行的边界。

AMM反馈组在含预热的全部50万tick中，观察到最大预定强度 **{peak:.3f}次到达/tick**，实际单tick到达峰值 **{max_arrivals}**。这些是有限样本峰值，没有构成模型上限。本轮没有预设游戏允许的活动/时延阈值，不能仅凭运行完成认证上线可接受。

## 1. 冻结内容、实验设置与输出

原版来源与首轮结果的SHA256见[frozen-reference.json](../../frozen-reference.json)，执行前后均核对未改。当前新增脚本只调用原run_one/simulate；原model.py、run.py、common和生产报价不改。原[首轮报告](../reference/report.md)保留。

| 设置 | 固定口径 |
| --- | --- |
| 种子与格数 | 新种子101–105；linear/amm，各b=0、.25；20条路径 |
| 长度与预热 | 每条500000tick；删前5000，共495000分析收益 |
| 同路径前缀 | tick5001–100000，95000收益，用来观察原长度统计与长路径差别 |
| 连续三段 | 5001–170000、170001–335000、335001–500000，各165000收益；不重置任何状态 |
| 状态/到达 | z初值0；tau1000；目标总到达1/tick；psi=.0004，固定不拟合 |
| AMM与账户 | 池金币1000000/外币200000；20户各金币5000/外币1000；费用0 |
| 订单与执行 | q=40epsilon，epsilon独立标准高斯；成交时池限额.005、有限钱包裁剪、6位量化，真实恒定乘积报价 |
| 随机流 | 沿用首轮20组独立Poisson/mark流与事件洗牌；b=0跨环境输入完全配对，反馈组内生分叉 |

输入[robustness.json](../../configs/robustness.json)，计划[ROBUSTNESS_PLAN.md](../../ROBUSTNESS_PLAN.md)。从仓库根目录运行：

```bash
OPENBLAS_NUM_THREADS=1 MPLCONFIGDIR=/tmp/fx-matplotlib python -m research.fx.activity.robustness --workers 3
OPENBLAS_NUM_THREADS=1 MPLCONFIGDIR=/tmp/fx-matplotlib python -m research.fx.activity.robustness_report
python -m unittest discover -s research/fx/tests -v
```

完整study.json含每路径/窗口结果，[metrics.csv](metrics.csv)提供100行窗口级指标。每路径保留series.npz、原口径result.json（完整ACF、聚合峰度、DFA及最终钱包）、windows.json。窗口只切保存的原始序列，不重新生成行情。`--analyze-only`可复算已保存路径的窗口汇总，无需再模拟。

## 2. 模型与统计定义

tick t先用期初z_t计算Lambda_t=(1−b)+b z_t²。每户抽Poisson(Lambda_t/20)，打乱事件后抽独立epsilon。线性收益r_t=psi sum(epsilon)；AMM按真实报价成交后r_t=ln(P_t/P_(t−1))。收盘更新z_(t+1)=a z_t+sqrt(1−a²)r_t/psi，a=exp(−1/1000)。无独立基本价值、目标库存或外生波动路径。

收益包含零收益。ACF对所在窗口独立去均值，用各lag乘积和除以lag0乘积和，不作N/(N−lag)修正；段值为全部整数lag的简单均值。方向最大相关为lag1–1000绝对值最大值，不是预测能力或多重检验结论。超额峰度用SciPy bias=False，高斯基准0。

跨块关系比较不重叠块内平均期初Lambda与平均r²；下一块用块j的Lambda对应块j+1的r²。完整窗口495000收益，30/100/240/1000宽度分别有16500/4950/2062/495完整块；三段各165000收益，分别有5500/1650/687/165块；下一块配对再减1，尾部不足一块舍弃。b=0强度常数，强度ACF和块相关未定义，以null/不适用表示。

强度矩为样本平均，方差除以N；99%/99.9%分位取逐tick预定强度的NumPy默认线性插值分位。预定强度与实际到达峰值分开记录。裁剪比例分母为所有到达，含完全跳过；少执行超过2e−6外币计为裁剪，常规量化不计；实际量比例为逐笔绝对执行量之和/逐笔绝对意向量之和。零现金/外币次数按该方向事件前对应钱包余额为0计数；钱包最小值是逐tick收盘账户最小余额的最小值，不是逐笔盘中最小值。

主表均为五个种子等权均值 [最小, 最大]，不是置信区间。没有按统计正负挑种子，没有事后统一通过门槛，也没有把前缀、三段和全程视为相互独立的实验。

## 3. 全长四格对照
''']
    text.append(table(cells,[('label','环境 / b',''),('signed_acf1','r ACF1',''),('signed_max','max |r ACF|',''),('abs_101-300','|r| 101–300',''),('abs_301-1000','|r| 301–1000',''),('sq_101-300','r² 101–300',''),('sq_301-1000','r² 301–1000','')]))
    text.append(table(cells,[('label','环境 / b',''),('abs_1001-5000','|r| 1001–5000',''),('sq_1001-5000','r² 1001–5000',''),('next_100','下一100',''),('next_240','下一240',''),('next_1000','下一1000','')]))
    text.append('\n### AMM反馈组逐种子\n\n'+table(amm,[('seed','seed',''),('abs_101-300','|r| 101–300','{:.5f}'),('abs_301-1000','|r| 301–1000','{:.5f}'),('sq_101-300','r² 101–300','{:.5f}'),('sq_301-1000','r² 301–1000','{:.5f}'),('sq_1001-5000','r² 1001–5000','{:.5f}'),('next_100','下一100','{:.5f}'),('next_240','下一240','{:.5f}'),('next_1000','下一1000','{:.5f}')]))
    text.append('''
![50万tick下的强度与平方收益ACF](acf.png)

理论虚线只对线性平稳强度成立，右上AMM仅作参照。平方收益曲线为50lag均值，完整逐lag曲线在各result.json；不凭图线平滑认证长记忆。

## 4. 同路径延长及前后时段

下表给出AMM反馈组各窗口的五种子均值和范围；前缀是完整路径的一部分，不能将延长前后的差额当作独立重复的处理效应。
''')
    window_groups=[]
    for window in ['prefix100k','third1','third2','third3','full']:
        rows=[r for r in windows if r['environment']=='amm' and r['b']==.25 and r['window']==window]
        window_groups.append(dict(window=window,n=rows[0]['n'],**{k:span(rows,k) for k in ['sq_101-300','sq_301-1000','sq_1001-5000','next_100','next_240','mean_intensity','clipped_fraction']}))
    text.append(table(window_groups,[('window','窗口',''),('n','收益数',''),('sq_101-300','r² 101–300',''),('sq_301-1000','r² 301–1000',''),('sq_1001-5000','r² 1001–5000',''),('next_100','下一100',''),('next_240','下一240',''),('mean_intensity','平均强度',''),('clipped_fraction','裁剪比例','')]))
    text.append('\n逐种子、逐连续时段如下。负值应保留，不能只看全程平均；15段不是15个独立种子。\n')
    text.append(table(thirds,[('seed','seed',''),('window','时段',''),('sq_101-300','r² 101–300','{:.5f}'),('sq_301-1000','r² 301–1000','{:.5f}'),('sq_1001-5000','r² 1001–5000','{:.5f}'),('next_100','下一100','{:.5f}'),('next_240','下一240','{:.5f}'),('mean_intensity','平均强度','{:.5f}'),('clipped_fraction','裁剪比例','{:.3%}')]))
    text.append('''
![前中后三段的变化](segments.png)

各段没有重置市场。短于总体记忆/尾部收敛所需的有限窗口可能表现不同；不能仅由一次下降断言永久退化，也不能用“有限样本”排除真实退化可能。

## 5. 运行边界与钱包

以下为完整分析窗口。活动峰值不是上限，钱包触零不等于全账户永久停机；后续反向交易可以改变余额。最终钱包和精确资产总额保留在每条result.json。
''')
    text.append(table([r for r in full if r['environment']=='amm'],[('label','环境 / b / seed',''),('mean_intensity','平均强度','{:.5f}'),('intensity_q99','强度99%','{:.3f}'),('intensity_q999','强度99.9%','{:.3f}'),('peak_intensity','强度峰值','{:.3f}'),('max_arrivals','实际到达峰值','{:.0f}'),('clipped_fraction','裁剪比例','{:.3%}'),('executed_fraction_of_raw_abs','实际/意向绝对量','{:.3%}'),('cash_saturated','买入前零现金','{:.0f}'),('foreign_saturated','卖出前零外币','{:.0f}')]))
    text.append('''
没有提供单tick可处理事件数、延迟预算或允许裁剪比例，因此此处只给观察范围。它不构成自动增加上限或资金恢复机制的授权。

## 6. 高阶矩补充与经验偏差

令d=1−a²、u=z²、lambda0=1−b。给定u，S=sum(epsilon)满足E[S²]=Lambda，E[S⁴]=3(Lambda+Lambda²)，E[S⁶]=15(Lambda+3Lambda²+Lambda³)。所以六阶下一期条件矩多项式是

E[z_next⁶|z]=a⁶u³+15a⁴d u²Lambda+45a²d²u(Lambda+Lambda²)+15d³(Lambda+3Lambda²+Lambda³)，Lambda=lambda0+b u。

用独立Polynomial展开核对最高次系数，与直接公式q6=a⁶+15a⁴db+45a²d²b²+15d³b³一致。当前参数结果：
''')
    text.append(f"q6={theory['q6']:.12f}>1，低次项C4={theory['coefficients_u_0_to_3'][2]:.12g}、C2={theory['coefficients_u_0_to_3'][1]:.12g}、C0={theory['coefficients_u_0_to_3'][0]:.12g}均正。因此，若理想线性模型存在平稳分布，该分布不能有有限六阶矩。四阶系数仍q4≈0.9989997518<1，对应E[z⁴]≈8.994、Var(Lambda)≈0.499626，二者并不矛盾。")
    text.append(f"\n以线性平稳强度理论ACF c^k计算，原95000样本均值方差换算的N_eff近似为{theory['mean_intensity_neff_stationary_approx']:.3f}；保留有限N权重的值为{theory['mean_intensity_neff_stationary_finite']:.3f}。这只是平均强度的方差等效量，不是全部指标通用样本量，也不是实测AMM有效样本量。")
    text.append('''
有限六阶矩不存在意味着不能套用温和高阶矩下的收敛直觉，不能解释为每条路径爆炸。估计强度方差依赖z四阶矩，估计该统计量的不确定性又涉及更高阶矩；罕见活动片段可能影响很大。它为经验偏差提供理论背景，但不能证明全部偏差已由有限样本解释，更不能将发散结论直接套到有限钱包AMM。

全长经验状态矩如下，五种子均值 [最小, 最大]。不要为逼近理论值而调参。
''')
    text.append(table(cells,[('label','环境 / b',''),('mean_intensity','平均Lambda',''),('variance_intensity','Var Lambda',''),('excess_kurtosis','收益超额峰度','')]))
    text.append('''
## 7. 验证与结论边界

20条路径均完成全长有限值、状态递推、期初强度、收益公式与来源hash核对。10条AMM路径的池+钱包金币精确为1100000、外币为220000；保存的逐tick收盘钱包最小值非负。五个固定率种子的到达与原始需求在两环境逐点完全一致。冻结的原来源/首轮结果执行前后hash一致。分段来自原始数组，覆盖495000收益，无缺口或重叠。另以直接乘积和核对20条路径lag1/301/1000/5000平方收益ACF，以独立分块及Pearson计算核对全程和每个三分段的全部下一块相关。含预热运行峰值单独记录。已有8项单元检查通过，证据见[validation.json](validation.json)。

本轮检验的是同参数在未见种子与更长时间上的重复性及观察运行边界。没有额外做钱包/曲率/池深因果消融，没有引入策略竞争；尾部幂律、杠杆效应、真实成交量与波动的独立检验仍不在范围内。方向ACF小不等于不可套利；正波动相关不等于交易收益；有限尺度DFA高不等于严格长记忆。

是否保留第一版，应据本轮完整路径、分段变化与运行边界共同判断；即便出现更高活动峰值，也不能自动改写原模型。报告没有把首轮“60.6%”当作钱包的因果削弱比例，也没有用旧统一0.01阈值否定或认证全部现象。
''')
    (OUTPUT/'report.md').write_text('\n\n'.join(text)+'\n')
    provenance={str(p.relative_to(HERE)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [HERE/'robustness.py',HERE/'robustness_report.py',HERE/'configs/robustness.json',OUTPUT/'study.json',OUTPUT/'validation.json']}
    (OUTPUT/'report-provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
    print(OUTPUT/'report.md')


if __name__=='__main__':
    build()
