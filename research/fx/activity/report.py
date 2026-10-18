"""Standalone Chinese readout of the fixed, twelve-path activity experiment."""
import argparse
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from research.fx.common.analysis import LAG_BANDS
from research.fx.common.reporting import table as plain_table
from research.fx.common.series import block_mean
from .run import DEFAULT_OUTPUT, clean_json


def table(rows, columns):
    # Absolute-value notation contains literal pipes in Markdown table headers.
    return plain_table(rows, [(key, label.replace('|', r'\|'), fmt) for key,label,fmt in columns])


def average(curve, lo, hi):
    values = curve[lo:hi+1]
    return float(np.mean(values)) if all(v is not None for v in values) else None


def rows_for(results):
    rows = []
    for x in results:
        s, d = x['summary'], x['diagnostics']
        row = dict(env=s['environment'], b=s['b'], seed=s['seed'], **d['moments'], **d['returns'])
        row['label'] = f"{s['environment']} / {s['b']:g} / {s['seed']}"
        row['signed_max_1_1000'] = max(abs(v) for v in d['signed_acf'][1:1001])
        for name, curve in [('signed', d['signed_acf']), ('abs', d['absolute_acf']),
                            ('sq', d['squared_acf']), ('activity', d['activity']['acf'])]:
            for lo, hi in LAG_BANDS:
                row[f'{name}_{lo}_{hi}'] = average(curve, lo, hi)
        for block in d['activity']['blocks']:
            row[f"same_{block['width']}"] = block['same_block_corr']
            row[f"next_{block['width']}"] = block['next_block_corr']
        row.update(d.get('execution', {}))
        row['r2_over_depth_W'] = d['moments'].get('r2_over_depth_W')
        rows.append(row)
    return rows


def mean_range(rows, key):
    values = [r[key] for r in rows if r[key] is not None]
    if not values:
        return '不适用'
    return f'{np.mean(values):.5f} [{min(values):.5f}, {max(values):.5f}]'


def figures(results, output):
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), constrained_layout=True)
    colors = {41:'#167c80', 42:'#d2852d', 43:'#794c9d'}
    for environment, col in [('linear', 0), ('amm', 1)]:
        for x in results:
            s, d = x['summary'], x['diagnostics']
            if s['environment'] != environment or s['b'] == 0:
                continue
            lag = np.arange(1, 2001)
            axes[0,col].plot(lag, np.array(d['activity']['acf'])[1:2001], color=colors[s['seed']], label=f"seed {s['seed']}")
            # Band averaging reduces plotting noise; the CSV retains every lag band.
            for name, style in [('absolute_acf','-'), ('squared_acf','--')]:
                curve = np.array(d[name])
                centers = np.arange(25.5, 2001, 50)
                axes[1,col].plot(centers, [curve[k:k+50].mean() for k in range(1,2001,50)],
                                 style, color=colors[s['seed']], alpha=.85,
                                 label=f"seed {s['seed']} {'|r|' if name=='absolute_acf' else 'r²'}")
        theory = next(x['linear_theory'] for x in results if x['summary']['b'] > 0)
        axes[0,col].plot(lag, theory['c']**lag, 'k:', label='linear stationary theory c^k')
        axes[0,col].set(title=f'{environment}: intensity ACF', xlabel='lag (ticks)', ylabel='correlation', ylim=(-.15,1.02))
        axes[1,col].set(title=f'{environment}: volatility proxy ACF (50-lag means)', xlabel='lag (ticks)', ylabel='correlation')
        axes[0,col].legend(fontsize=8)
        axes[1,col].legend(fontsize=7, ncol=2)
        for axis in axes[:,col]:
            axis.axhline(0,color='gray',lw=.6)
            axis.grid(alpha=.18)
    fig.savefig(output/'acf.png',dpi=160)
    plt.close(fig)
    fig, axes = plt.subplots(3,2,figsize=(12,8),constrained_layout=True)
    for col, environment in enumerate(['linear','amm']):
        for row, seed in enumerate([41,42,43]):
            name=f'{environment}-b0.25-seed{seed}'
            with np.load(output/name/'series.npz') as x:
                burn=next(r['config']['burn_in'] for r in results)
                lam=block_mean(x['intensity'][burn+1:],100)
                variance=block_mean(x['returns'][burn+1:]**2,100)/(.0004**2)
                ticks=burn+100*(np.arange(len(lam))+.5)
                axes[row,col].plot(ticks,lam,color=colors[seed],label='pre-tick intensity, 100-tick mean')
                axes[row,col].plot(ticks,variance,color='gray',alpha=.5,lw=.6,label='r² / psi², 100-tick mean')
                axes[row,col].set(title=f'{environment}, seed {seed}',xlabel='tick',ylabel='activity / variance scale')
                axes[row,col].legend(fontsize=6)
    fig.savefig(output/'paths.png',dpi=160)
    plt.close(fig)


def build_report(output):
    output = Path(output)
    results = [json.loads(p.read_text()) for p in sorted(output.glob('*/result.json'))]
    if len(results) != 12:
        raise ValueError('reference readout requires all twelve paths')
    rows = rows_for(results)
    cfg=results[0]['config']
    theory=next(x['linear_theory'] for x in results if x['summary']['b'] > 0)
    figures(results,output)
    columns=list(dict.fromkeys(k for r in rows for k in r))
    with (output/'metrics.csv').open('w',newline='') as file:
        writer=csv.DictWriter(file,fieldnames=columns)
        writer.writeheader(); writer.writerows(rows)
    groups={(env,b):[r for r in rows if r['env']==env and r['b']==b] for env in ['linear','amm'] for b in [0,.25]}
    means=[dict(cell=f'{env} / {b:g}', **{key:mean_range(group,key) for key in
           ['signed_1_1','abs_101_300','abs_301_1000','sq_101_300','sq_301_1000','activity_301_1000','next_100','next_240','next_1000','excess_kurtosis']})
           for (env,b),group in groups.items()]
    linear, amm = groups['linear',.25], groups['amm',.25]
    la, aa = (np.mean([r['sq_301_1000'] for r in group]) for group in [linear,amm])
    all_positive=all(r[k] > 0 for r in amm for k in ['abs_101_300','abs_301_1000','sq_101_300','sq_301_1000','next_100','next_240'])
    parts=[f'''# 活动反馈第一轮：线性参照与有限钱包 AMM

2026-10-15。按第五轮讨论独立实现，固定参数跑完四格×三个种子，共12条10万tick路径。归档M/R未恢复，线上市场未改动。

本地结果：AMM反馈组三个种子的101–300、301–1000段绝对/平方收益相关，以及下一100/240块关系{'均为正' if all_positive else '未全部为正'}。平方收益301–1000段跨种子均值，线性为{la:.5f}，AMM为{aa:.5f}（两组实现值之比{aa/la:.1%}）。这是各自内生路径的描述性比较，不能据此把差额全部归因于钱包：AMM曲率、池深和反馈路径也一并改变了。

这说明该反馈在此次AMM设置下保留了有限尺度的波动持续性。它不代表全部市场典型事实已经复现，也没有证明严格幂律长记忆。三种子不足以作显著性或稳健性结论；本轮没有调参或新设通过门槛。

## 1. 实验设置与复算

| 设置 | 固定取值 |
| --- | --- |
| 环境与反馈 | linear/amm，各b=0及0.25 |
| 长度与预热 | 每条100000tick；去除tick1–5000，分析tick5001–100000，共95000收益 |
| 种子 | 41、42、43；每格单独运行 |
| 状态 | z初值0；tau=1000，a=exp(−1/tau) |
| 强度与尺度 | 目标平均总到达1/tick；psi=0.0004，固定不拟合 |
| 账户与钱包 | 20账户；AMM每户金币5000、外币1000；线性无钱包 |
| AMM | 恒定乘积池，初始金币1000000、外币200000，初价5；费率0 |
| 订单 | 独立标准高斯epsilon；AMM外币意向q=40epsilon；单笔按成交时池外币储备的0.005限额，再按钱包裁剪与6位量化 |
| 机制限制 | 无目标库存、M/R身份、分数选择、额外价格噪声、补钱、持仓重置、强制成交或活动强度上限 |

从仓库根目录运行（已有NumPy/SciPy/Matplotlib环境）：

```bash
OPENBLAS_NUM_THREADS=1 python -m research.fx.activity.run --workers 3
MPLCONFIGDIR=/tmp/fx-matplotlib OPENBLAS_NUM_THREADS=1 python -m research.fx.activity.report
python -m unittest discover -s research/fx/tests -v
```

输入为[reference.json](../../configs/reference.json)，代码为[model.py](../../model.py)、[run.py](../../run.py)、[report.py](../../report.py)，复用[common](../../../common/README.md)。每条路径保存series.npz、result.json（含参数、钱包、来源SHA256、完整诊断）；[metrics.csv](metrics.csv)提供种子级指标，[validation.json](validation.json)提供全长旧对照一致性证据。JSON中未定义相关为null，CSV空单元格/本文“不适用”同义，不替成0。

## 2. 模型、时序与理论边界

每个tick t先记录已知的z_t与Lambda_t=(1−b)×1+b z_t²，再由20个账户各抽Poisson(Lambda_t/20)，重复到达分别计数，事件顺序打乱；每次由该账户独立mark流抽epsilon~N(0,1)。线性环境r_t=psi×sum(epsilon)；AMM环境依次以当前池、当前钱包真实报价成交，收盘r_t=ln(P_t/P_(t−1))。最后更新z_(t+1)=a z_t+sqrt(1−a²)r_t/psi。数组index0是初始化哨兵，不进入任何统计；z_pre[t]、intensity[t]均在returns[t]之前决定。

各账户到达流种子为seed×1000003+i×101+1，mark流为+2，事件洗牌用Python Random(seed×1000003+4)。b=0时两种环境到达与原始需求完全配对；b>0的实际反馈会让事件次数及后续mark消费分叉，同编号种子不意味着动态组逐点配对。

线性条件均值0，精确条件二阶矩W_t=psi² Lambda_t。在线性平稳且四阶矩存在时，E[z²]=1，E[Lambda]=1，强度ACF=c^k，c=a²+b(1−a²)。b=0强度常数，强度/W相关均不适用；z仍可递推但不再改变到达率。

当前b=0.25时c={theory['c']:.12f}，理论e倍衰减时间{theory['intensity_decay_ticks']:.2f}tick；q4=a^4+6a²(1−a²)b+3(1−a²)²b²={theory['q4']:.12f}<1。解四阶递推得到E[z^4]={theory['stationary_z4']:.6f}、Var(Lambda)={theory['stationary_variance_intensity']:.6f}。这些是平稳总体值，不能要求有限样本精确等于它们，也不能用“已有理论”掩盖经验偏差。

AMM中上述均值、ACF及方向鞅结论不再获得解析保证。本文仍记录psi²Lambda作为参考W；另记期初池深近似W_depth=(2×40/F_pre)²Lambda，只作诊断，不进入反馈更新或重新归一化。这两个W都不是AMM精确条件二阶矩。常数池深/无裁剪近似无法替代真实执行。

该离散模型取自讨论稿的具体定义；文献背景是Blanc、Donier、Bouchaud的[Quadratic Hawkes processes for financial prices](https://arxiv.org/abs/1509.07710)，不能称为其连续时间模型原样复现。讨论稿所附sandbox脚本不在工作区，外部数值仅作独立参考，本地随机流与外部不同，不声称逐点复现。

## 3. 指标定义与口径

- 收益：AMM为相邻tick收盘对数价格差（数值上用ln价格比），线性为复合Poisson高斯和；包含无到达及未成交产生的零收益。
- ACF：对95000样本先全样本去均值，FFT计算各lag乘积和，除以lag0乘积和；不作N/(N−lag)修正。段值为各整数lag ACF的简单均值；最大方向ACF取lag1–1000绝对值最大值。完整曲线保留至5000。
- 波动代理：|r|及r²；W参考与Lambda为固定正比例，二者ACF及块相关相同。代理的噪声与内生反馈决定相关幅度，不能把Lambda、|r|、r²统一0.01当成通用物理门槛。
- 块关系：预热后从头分成不重叠块，分别取块内平均期初Lambda与平均r²；同块用同编号块，下一块用Lambda块j对应r²块j+1做Pearson相关。30/100/240/1000tick分别有3166/950/395/95完整块、3165/949/394/94个下一块配对，尾部不足一块舍弃。下一块相关不等于当期预测的无偏检验，也不是置信区间。
- 分布：标准差ddof=1；偏度及Fisher超额峰度用SciPy bias=False，正态超额峰度0；非零收益单独保存。高峰度也可能来自稀疏到达，不能单独证明波动聚集。
- 状态矩：平均Lambda及z²、z⁴均为样本直接平均；Var(Lambda)为样本去均值平方和除以N（ddof=0）；r²/W比值为两者样本平均值之比，不是逐tick比值平均。
- 聚合：不重叠的1/5/30/60/240/1000tick收益求和再算分布；尾部舍弃。DFA1/2对去均值后累积profile，在固定16–8192尺度双端分块去趋势，保存32–256、512–8192、2048–8192区间拟合。重叠端段不作独立样本；有限区间斜率不作严格长记忆证据。
- 执行：到达包含未成交；raw_demand为q的有符号和，raw_abs_demand为绝对值和；actual_flow为钱包外币净增加，executed_abs_quantity逐笔取绝对值后相加；金币成交额为买入金币输入+卖出金币输出。裁剪含部分和完全未执行，以少执行超过2e−6外币区分常规量化；skipped为无成交，余额边界为该方向事件发生前钱包对应余额为0。
- 下表所有诊断与执行比例采用去预热后的95000tick；summary的到达/成交总数、总金币额覆盖全部100000tick；钱包为最终余额。跨种子均值对三个种子等权，方括号为最小/最大值，均非置信区间。

## 4. 四格核心结果

各格为跨种子均值 [最小, 最大]。
''']
    parts.append(table(means,[('cell','环境 / b',''),('signed_1_1','r ACF1',''),('abs_101_300','|r| 101–300',''),('abs_301_1000','|r| 301–1000',''),('sq_101_300','r² 101–300',''),('sq_301_1000','r² 301–1000','')]))
    parts.append('\n'+table(means,[('cell','环境 / b',''),('activity_301_1000','Lambda 301–1000',''),('next_100','下一100块',''),('next_240','下一240块',''),('next_1000','下一1000块',''),('excess_kurtosis','超额峰度','')]))
    parts.append('\n种子级关键结果如下；不要用均值遮盖小相关和不同衰减速度。\n')
    parts.append(table(rows,[('label','环境 / b / seed',''),('signed_1_1','r ACF1','{:.5f}'),('signed_max_1_1000','max |r ACF|','{:.5f}'),('abs_301_1000','|r| 301–1000','{:.5f}'),('sq_301_1000','r² 301–1000','{:.5f}'),('next_100','下一100','{:.5f}'),('next_240','下一240','{:.5f}'),('next_1000','下一1000','{:.5f}'),('excess_kurtosis','超额峰度','{:.4f}')]))
    parts.append('''
## 5. 活动理论与经验矩

线性反馈组的经验强度均值及方差低于理论平稳值，强度ACF也未普遍贴合c^k。三个种子、每条10万tick不足以验证高阶矩收敛；尤其Var(Lambda)依赖z四阶矩，容易受少数高活动段影响。目前只能报告偏差，不能证明偏差仅由有限样本造成，或据此宣称理论数值已验证。条件矩递推已用独立手算例子检查，线性经验r²/参考W则接近1。

AMM中的r²/参考W约小于1，但这是总体平均比值，不是逐状态校准；也不能直接把下降解释为钱包因果效应。若后续要验证总体矩，应另作更长独立路径的预先规划，当前不据此改参数。
''')
    parts.append(table(rows,[('label','环境 / b / seed',''),('mean_intensity','平均Lambda','{:.5f}'),('variance_intensity','Var Lambda','{:.5f}'),('mean_z2','均值z²','{:.5f}'),('mean_z4','均值z⁴','{:.5f}'),('mean_arrivals','实际到达/tick','{:.5f}'),('peak_intensity','最大Lambda','{:.3f}'),('r2_over_reference_W','r²/参考W','{:.5f}'),('r2_over_depth_W','r²/池深W','{:.5f}')]))
    parts.append('''
![强度ACF与收益波动代理](acf.png)

上排虚线是线性平稳理论，右侧仅作参照，不是AMM理论；下排为50lag均值，完整曲线在各result.json。

![三个种子的活动与实际波动](paths.png)

两条线均以100tick不重叠块平均展示，不表示逐tick条件方差已校准。

## 6. 全部区间、跨块关系、分布与DFA
''')
    for name,title in [('signed','方向收益ACF'),('abs','绝对收益ACF'),('sq','平方收益ACF'),('activity','活动强度ACF')]:
        parts.append(f'\n### {title}\n\n'+table(rows,[('label','环境 / b / seed','')]+[(f'{name}_{lo}_{hi}',f'{lo}–{hi}','{:.5f}') for lo,hi in LAG_BANDS]))
    for relation,title in [('same','同块关系'),('next','下一块关系')]:
        parts.append(f'\n### Lambda与r²的{title}\n\n'+table(rows,[('label','环境 / b / seed','')]+[(f'{relation}_{w}',str(w),'{:.5f}') for w in [30,100,240,1000]]))
    parts.append('\n### 收益分布\n\n'+table(rows,[('label','环境 / b / seed',''),('mean','均值','{:.8f}'),('std','标准差','{:.8f}'),('zero_fraction','零收益占比','{:.4%}'),('skew','偏度','{:.4f}'),('excess_kurtosis','超额峰度','{:.4f}')]))
    aggregation=[]; dfa=[]
    for x,row in zip(results,rows):
        d=x['diagnostics']
        aggregation.append(dict(label=row['label'], **{w:v['excess_kurtosis'] for w,v in d['aggregation'].items()}))
        values=dict(label=row['label'])
        for name,short in [('returns','r'),('absolute_returns','abs')]:
            for order,key in [(1,'fits'),(2,'dfa2_fits')]:
                for band in ['32-256','512-8192','2048-8192']:
                    values[f'{short}_{order}_{band}']=d['dfa'][name][key].get(band,{}).get('alpha')
        dfa.append(values)
    parts.append('\n### 聚合收益超额峰度\n\n'+table(aggregation,[('label','环境 / b / seed','')]+[(str(w),str(w),'{:.4f}') for w in [1,5,30,60,240,1000]]))
    for name,short in [('方向收益','r'),('绝对收益','abs')]:
        parts.append(f'\n### {name}DFA拟合斜率\n\n'+table(dfa,[('label','环境 / b / seed','')]+[(f'{short}_{order}_{band}',f'DFA{order} {band}','{:.4f}') for order in [1,2] for band in ['32-256','512-8192','2048-8192']]))
    parts.append('''
## 7. AMM执行与资金约束

以下为去预热口径；部分裁剪及完全跳过均计入裁剪比例。它们没有使所有成交中止，但实际高斯意向分布已被改变。没有额外加入资金或跨账户转账。所有AMM路径期末池+钱包金币精确等于1100000、外币精确等于220000；成交逻辑在每笔交易检查余额，最终钱包非负。
''')
    parts.append(table([r for r in rows if r['env']=='amm'],[('label','环境 / b / seed',''),('arrivals','到达','{:.0f}'),('trades','成交','{:.0f}'),('clipped_fraction','裁剪比例','{:.3%}'),('skipped_orders','跳过','{:.0f}'),('cash_saturated','买入时零现金','{:.0f}'),('foreign_saturated','卖出时零外币','{:.0f}'),('executed_fraction_of_raw_abs','实际/意向绝对量','{:.3%}'),('gold_volume','金币成交额','{:.2f}')]))
    parts.append('''
固定率AMM组与归档20N的三个种子，在含初始化点的100001个点上，prices/gold/arrivals/raw_demand(旧noise_demand)/trades/gold_volume六字段逐点完全一致；这支持公共成交与时钟兼容，不意味着活动反馈组与旧组逐点可比。详细最终钱包保存在各result.json，原始路径保存在series.npz。

## 8. 判断与下一步边界

本轮接通了“过去价格变化→到达强度→真实成交→后续价格变化”的闭环。在当前参数及三个种子上，AMM保留正的100/240tick下一块关系，长滞后原始收益代理的相关幅度仍小。1000tick下一块关系与1001–5000段应单独看，不能从较短窗口正相关外推。

下一步先讨论要验证的目标：若目标是活动状态的总体矩与理论曲线收敛，需规划更长的线性独立路径；若目标是判断AMM迁移损失的来源，则需要单独设计执行层对照。当前四格只能显示联合迁移后的实现差别，不能区分钱包、池深与曲率各自的贡献。本轮未运行新消融或扫描，也未加风险/参与机制。

当前模型有20个独立随机账户，但共享一个价格历史活动状态。它可研究内生活动反馈，尚不解释独立交易者的策略选择和竞争涌现；也没有独立基本价值输入。旧统一0.01筛查不再用作通用成功定义，外部线性结果不代替本地AMM验收。

## 9. 已完成的验证

`python -m unittest discover -s research/fx/tests -v`：8项通过，覆盖活动更新时序、关闭反馈、独立手算条件矩、跨环境固定率输入、真实执行守恒和已有统计检查。`python -m research.fx.activity.run --workers 3`及报告生成命令均完成。

另对12条保存的全长序列核对了有限值、期初状态递推、到达率与收益公式、95000样本边界和当前来源hash；直接乘积和独立复算lag1/30/301/1000/5000的收益及波动代理ACF，直接分块及Pearson计算核对全部同块/下一块关系。AMM储备/钱包最小值非负、两类资产期末精确守恒，固定率输入跨环境完全相同，common来源与归档兼容版本一致。证据见[validation.json](validation.json)。这些检查验证实现及统计口径，没有验证更长路径下的高阶矩收敛或现实市场适配性。

一次最终只读审阅未发现其余重要缺陷，发现的Markdown表头竖线转义问题已修复；两张图已人工查看。未运行线上或浏览器交互测试，本轮不涉及线上代码。
''')
    report='\n\n'.join(parts)+'\n'
    (output/'report.md').write_text(report)
    (output/'report-data.json').write_text(json.dumps(clean_json(dict(cells=means,rows=rows)),ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    print(output/'report.md')


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=DEFAULT_OUTPUT)
    build_report(parser.parse_args().output)
