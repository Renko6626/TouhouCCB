"""Standalone evidence for the approved three-way factor/activity comparison."""
import csv
import hashlib
import json

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from research.fx.activity.report import table
from .run import HERE,OUTPUT,CASES

LABELS={'factor_fixed':'三因子固定率','activity_v1':'活动反馈v1','factor_activity':'三因子＋活动反馈'}


def row_for(result):
    s=result['summary'];d=result['diagnostics']
    row=dict(case=s['case'],seed=s['seed'],label=f"{LABELS[s['case']]} / {s['seed']}",
        mean_intensity=d['mean_intensity'],peak_intensity=s['peak_intensity'],
        **d['returns'],**d['execution'])
    row['max_signed']=max(abs(v) for v in d['signed_acf'][1:1001])
    for short,key in [('signed','signed_acf'),('abs','absolute_acf'),('sq','squared_acf')]:
        for lo,hi in [(1,1),(2,10),(11,30),(31,100),(101,300),(301,1000),(1001,5000)]:
            row[f'{short}_{lo}_{hi}']=float(np.mean(d[key][lo:hi+1]))
    for block in d['activity']['blocks']:
        row[f"next_{block['width']}"]=block['next_block_corr']
    row['trades_per_tick']=row['trades']/d['returns']['n']
    row['gross_quantity_per_tick']=row['executed_abs_quantity']/d['returns']['n']
    return row


def span(rows,key):
    values=[r[key] for r in rows if r.get(key) is not None]
    if not values:return '不适用'
    return f'{np.mean(values):.5f} [{min(values):.5f}, {max(values):.5f}]'


def figures(results):
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False})
    fig,axes=plt.subplots(3,2,figsize=(13,9),constrained_layout=True)
    colors={41:'#167a73',42:'#c47a2d',43:'#8054a1'}
    for row,case in enumerate(CASES):
        for result in results:
            s=result['summary']
            if s['case']!=case:continue
            with np.load(OUTPUT/f"{case}-seed{s['seed']}"/'series.npz') as x:
                t=np.arange(len(x['prices']))
                axes[row,0].plot(t,x['prices'],lw=.8,color=colors[s['seed']],label=f"seed {s['seed']}")
                axes[row,1].plot(t[1:],x['intensity'][1:],lw=.8,color=colors[s['seed']],label=f"seed {s['seed']}")
        axes[row,0].set(title=LABELS[case]+'：价格',ylabel='本币 / 外币',xlabel='tick')
        axes[row,1].set(title=LABELS[case]+'：期初交易强度',ylabel='预期到达 / tick',xlabel='tick')
        for ax in axes[row,:]:ax.grid(alpha=.2);ax.legend(fontsize=8)
    fig.savefig(OUTPUT/'paths.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(2,2,figsize=(12,8),constrained_layout=True)
    keys=['signed_acf','absolute_acf','squared_acf']
    colors=['#39768e','#9c6c9e','#bc742f']
    for case,color in zip(CASES,colors):
        group=[r for r in results if r['summary']['case']==case]
        for ax,key,title in zip(axes.ravel()[:3],keys,['收益方向ACF','绝对收益ACF','平方收益ACF']):
            curve=np.mean([r['diagnostics'][key] for r in group],axis=0)
            centers=np.arange(10.5,2001,20)
            ax.plot(centers,[curve[k:k+20].mean() for k in range(1,2001,20)],color=color,label=LABELS[case])
            ax.set(title=title+'（20 lag均值）',xlabel='滞后 / tick',ylabel='ACF')
        if case!='factor_fixed':
            curve=np.mean([r['diagnostics']['activity']['acf'] for r in group],axis=0)
            axes[1,1].plot(np.arange(1,2001),curve[1:2001],color=color,label=LABELS[case])
    axes[1,1].set(title='活动强度ACF（固定率不适用）',xlabel='滞后 / tick',ylabel='ACF')
    for ax in axes.ravel():ax.axhline(0,color='gray',lw=.6);ax.grid(alpha=.2);ax.legend(fontsize=8)
    fig.savefig(OUTPUT/'acf.png',dpi=150);plt.close(fig)


def build():
    results=[json.loads((OUTPUT/f'{case}-seed{seed}'/'result.json').read_text()) for case in CASES for seed in [41,42,43]]
    rows=[row_for(r) for r in results]
    groups={case:[r for r in rows if r['case']==case] for case in CASES}
    metrics=['signed_1_1','max_signed','abs_101_300','abs_301_1000','sq_101_300','sq_301_1000',
        'mean_intensity','peak_intensity','trades_per_tick','gross_quantity_per_tick','clipped_fraction','excess_kurtosis',
        'next_100','next_240','next_1000']
    cells=[dict(label=LABELS[case],**{k:span(group,k) for k in metrics}) for case,group in groups.items()]
    figures(results)
    with (OUTPUT/'metrics.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    pure=groups['activity_v1'];fixed=groups['factor_fixed'];combo=groups['factor_activity']
    m=lambda group,key:float(np.mean([r[key] for r in group]))
    joint=sum(all(r[k]>0 for k in ['abs_101_300','abs_301_1000','sq_101_300','sq_301_1000']) for r in combo)
    feedback_addition=m(combo,'mean_intensity')-.75
    parts=[f'''# 三因子与活动反馈组合：第一轮三组对照

2026-10-15。用户批准恢复三因子，并比较原三因子、活动反馈v1、组合模型。本轮三组×三个种子，共9条10万tick路径，预热5000。未扫描参数，未运行央行注资或玩家场景，未修改生产市场、archive、common或冻结v1。

组合组在101–300和301–1000段的绝对/平方收益ACF全部为正的种子为 **{joint}/3**。301–1000段平方收益ACF均值：三因子固定率 **{m(fixed,'sq_301_1000'):.5f}**，活动反馈v1 **{m(pure,'sq_301_1000'):.5f}**，组合 **{m(combo,'sq_301_1000'):.5f}**。方向lag1 ACF均值依次为 **{m(fixed,'signed_1_1'):.5f} / {m(pure,'signed_1_1'):.5f} / {m(combo,'signed_1_1'):.5f}**；必须同时看方向相关和波动持续性，不能只选有利的一列。

本轮问题是：恢复交易者价格判断之后，既有活动反馈还能保留多少作用。三组结果为有限样本描述，不是统计显著性、交易盈利或整体市场复现的证明。完整曲线、DFA和分布均保留在各result.json。

**当前固定组合没有保留v1的中尺度波动持续性。** 组合平均Lambda={m(combo,'mean_intensity'):.6f}，比公式底值.75仅多{feedback_addition:.6f}；实际外币绝对成交量/tick为{m(combo,'gross_quantity_per_tick'):.3f}，v1为{m(pure,'gross_quantity_per_tick'):.3f}。组合收益标准差约{m(combo,'std'):.8f}，仍使用psi=.0004，尺度比约{m(combo,'std')/.0004:.3f}。同时，加入三因子后短期方向相关比v1更强。已有研究基线的结论不能直接转移到这次组合。

这些观测与“有界目标调仓使成交和累计价格偏离较小，而固定psi下活动增益很弱”相符；本轮没有单独区分订单规模、库存回复和方向相关各自的贡献，不把结果全部归因于某一个因素。没有为了补救结果自动改psi、订单量或噪声。

## 1. 实验设置与旧模型来源

| 项目 | 本轮固定口径 |
| --- | --- |
| 三组 | factor_fixed：三因子+固定总到达1；activity_v1：原随机订单+活动反馈；factor_activity：三因子+活动反馈 |
| 路径 | 种子41/42/43；每条100000tick；分析tick5001–100000，共95000收益 |
| 账户/池/手续费 | 20户各本币5000、外币1000；池本币1000000、外币200000，初价5；费用0 |
| 三因子 | 窗口30/30；偶数户趋势30/回归.1，奇数户趋势3/回归1；噪声sigma=.0005，每户每tick一次；无动态类型转换 |
| 仓位规则 | 目标净外币=500*tanh(100*g)，相对开局1000外币；每次到达向目标差额调整20%，用真实库存重算 |
| 买卖执行 | 三因子买入将外币意向按期初价折成本币预算，卖出按外币数量；经当前池限额.005和钱包约束，调用生产报价 |
| 活动反馈 | b=.25、tau1000、psi=.0004，z初值0；关闭反馈组b=0；不按新收益波动重新拟合psi |
| v1订单 | 每次到达q=40*独立标准高斯，按原common.execute执行，保持原v1代码 |

三因子来自[归档基线](../../../archive/2026-10-15/configs/baseline.json)及原expectation.py/position.py，保留加法信号、固定异质权重、tanh目标和20%调仓。统一采用当前有限钱包、零费用、分离随机流及期初公开价格。本轮“原三因子”指恢复这些决策规则，不是旧路径原样复现；旧归档用近乎无限钱包、不同随机源和逐账户读价，本轮没有恢复这些条件。锁仓、成绩切换和M/R身份转换不在范围内。

两种订单分布与资金利用率不同，不保证相同成交量或收益尺度。factor_fixed与factor_activity之间只改变反馈开关，是主要比较；与v1的比较同时改变订单策略。

## 2. 单tick时序与信号定义

先读取上一收盘P和历史，保存期初z与Lambda。每个账户更新g=aT+bM+sigma*epsilon；T=(当前对数价格−此前窗口最旧对数价格)/30，M=此前30个收盘对数均值−当前对数价格。历史包含当前观察收盘，没有未来数据；不足30点时相应确定性因子为0。g=ln(主观目标价格/观察价格)，近期均价不代表独立基本价值。

随后每人抽Poisson(Lambda/20)，事件洗牌后逐笔真实成交；同tick重复到达复用该tick预期，重新读取真实库存。最后收盘r=ln(P_t/P_(t−1))，更新z_next=exp(−1/1000)*z+sqrt(1−exp(−2/1000))*r/.0004。三因子每tick消耗一个个人噪声数，不因到达次数变化而改变预期噪声序列；两三因子组该序列配对，价格及信号可以分叉。动态组不要求到达或成交逐点相同。

psi²Lambda不作为三因子/组合的精确条件二阶矩；固定psi下，原v1的条件收益与理论ACF保证不转移到组合模型。净仓位为钱包外币减开局1000，负净仓位表示减持已有外币，没有借币裸卖空。

## 3. 核心结果

以下均为三个种子均值 [最小, 最大]，不是置信区间。
''']
    parts.append(table(cells,[('label','模型',''),('signed_1_1','r ACF1',''),('max_signed','max |r ACF|',''),('abs_101_300','|r| 101–300',''),('abs_301_1000','|r| 301–1000',''),('sq_101_300','r² 101–300',''),('sq_301_1000','r² 301–1000','')]))
    parts.append(table(cells,[('label','模型',''),('mean_intensity','平均Lambda',''),('next_100','下一100块',''),('next_240','下一240块',''),('next_1000','下一1000块',''),('excess_kurtosis','超额峰度','')]))
    parts.append('\n种子级结果：\n\n'+table(rows,[('label','模型 / seed',''),('signed_1_1','r ACF1','{:.5f}'),('abs_301_1000','|r| 301–1000','{:.5f}'),('sq_301_1000','r² 301–1000','{:.5f}'),('next_100','下一100','{:.5f}'),('next_240','下一240','{:.5f}'),('next_1000','下一1000','{:.5f}')]))
    parts.append('''
![三组价格与活动](paths.png)

每行是同一模型的三个种子。不同图轴范围应按标签读取，不凭视觉振幅判断显著性。

![方向、波动与活动ACF](acf.png)

收益曲线按20个lag平均展示；段均值和每个lag的原数值均保留，常数强度ACF未定义。

## 4. 全部滞后区间
''')
    for short,title in [('signed','方向收益'),('abs','绝对收益'),('sq','平方收益')]:
        parts.append(f'\n### {title}ACF\n\n'+table(rows,[('label','模型 / seed','')]+[(f'{short}_{lo}_{hi}',f'{lo}–{hi}','{:.5f}') for lo,hi in [(1,1),(2,10),(11,30),(31,100),(101,300),(301,1000),(1001,5000)]]))
    parts.append('''
## 5. 成交尺度及因子量级

全部执行指标去掉前5000tick。三因子裁剪按实际订单输入与“未受钱包/池限制、但已经过同样正常量化的输入预算”比较，避免将两次量化或买入滑点误记为钱包裁剪；v1仍按外币意向/真实填单比较。因此裁剪比例跨订单类型只作辅助描述，成交量和钱包记录应一并阅读。跳过包含目标差额过小的无单事件，不全部代表资金耗尽。
''')
    parts.append(table(cells,[('label','模型',''),('trades_per_tick','成交/tick',''),('gross_quantity_per_tick','外币绝对量/tick',''),('clipped_fraction','裁剪比例',''),('peak_intensity','全程最大强度','')]))
    factor_rows=[]
    for r in results:
        s=r['summary']
        for f in s.get('factors') or []:
            factor_rows.append(dict(label=f"{LABELS[s['case']]} / {s['seed']} / {f['weights']}",T=f['std'][0],M=f['std'][1],N=f['std'][2]))
    parts.append('\n各因子已乘权重后的标准差，按预热后所有账户更新计算；它们不是价格方差贡献，未忽略协方差作归因：\n\n'+table(factor_rows,[('label','模型 / seed / 权重',''),('T','趋势std','{:.7f}'),('M','回归std','{:.7f}'),('N','噪声std','{:.7f}')]))
    parts.append('''
## 6. 分布、聚合与DFA

所有收益含零值，标准差ddof=1、偏度及Fisher超额峰度bias=False，正态超额峰度0；高峰度不单独证明稳定厚尾。聚合为不重叠收益求和、尾部不足一块丢弃。DFA1/2沿用common的固定尺度16–8192及双端分块，斜率不当作已认证长记忆。
''')
    parts.append(table(rows,[('label','模型 / seed',''),('std','标准差','{:.8f}'),('zero_fraction','零收益比例','{:.3%}'),('skew','偏度','{:.4f}'),('excess_kurtosis','超额峰度','{:.4f}')]))
    agg=[];dfa=[]
    for r,row in zip(results,rows):
        d=r['diagnostics'];agg.append(dict(label=row['label'],**{w:v['excess_kurtosis'] for w,v in d['aggregation'].items()}))
        values=dict(label=row['label'])
        for name,short in [('returns','r'),('absolute_returns','abs')]:
            for order,key in [(1,'fits'),(2,'dfa2_fits')]:
                for band in ['32-256','512-8192','2048-8192']:
                    values[f'{short}_{order}_{band}']=d['dfa'][name][key].get(band,{}).get('alpha')
        dfa.append(values)
    parts.append('\n聚合超额峰度：\n\n'+table(agg,[('label','模型 / seed','')]+[(str(w),str(w),'{:.4f}') for w in [1,5,30,60,240,1000]]))
    for short,label in [('r','收益'),('abs','绝对收益')]:
        parts.append(f'\n{label}DFA：\n\n'+table(dfa,[('label','模型 / seed','')]+[(f'{short}_{order}_{band}',f'DFA{order} {band}','{:.4f}') for order in [1,2] for band in ['32-256','512-8192','2048-8192']]))
    parts.append('''
## 7. 统计口径、复算与验证

ACF为所在95000样本去均值后的lag乘积和/lag0乘积和，不作N/(N−lag)修正；区间是全部整数lag平均，方向max取1–1000。完整曲线保存到5000。块关系为期初Lambda平均值与同/下一块r²均值的Pearson相关，不重叠30/100/240/1000tick块，完整块分别3166/950/395/95，下一块配对再减1；没有在预热或分块边界重置状态。固定率Lambda恒定，相关为null/不适用。

每路径保存series.npz、result.json（参数、来源hash、最终钱包、全指标）；[metrics.csv](metrics.csv)为种子级表。验证见[validation.json](validation.json)：v1三种子全部保存字段与冻结首轮逐点完全一致；两三因子组个人预期噪声序列一致，不要求动态到达配对；全部资产精确守恒、钱包非负，三因子实际净仓位未超±500。

```bash
OPENBLAS_NUM_THREADS=1 python -m research.fx.factor_activity.run
OPENBLAS_NUM_THREADS=1 MPLCONFIGDIR=/tmp/fx-matplotlib python -m research.fx.factor_activity.report
python -m unittest discover -s research/fx/tests -v
```

本轮没有为了提高ACF调整b、psi、因子权重或仓位幅度。组合是否有用要结合方向相关、跨块持续性及成交尺度判断；即使失败，也只针对这次明确的移植组合，不能推论全部三因子模型无效。

相关单元检查13项通过，包含三因子手算、预算买入的实际滑点/重复调仓、无借贷减持、预期噪声配对和反馈时序。审阅发现并修复正常量化误记裁剪的问题：回归用例先复现34次错误计数、修复后为0；修正只涉及诊断分类，全部保存的价格、需求和成交序列与修正前逐项SHA核对，不改变路径或调参。一次最终只读审阅及两图查看完成，详细独立核对记录在validation.json。
''')
    (OUTPUT/'report.md').write_text('\n\n'.join(parts)+'\n')
    provenance={str(p.relative_to(HERE)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [HERE/'report.py',HERE/'run.py',HERE/'model.py',HERE/'config.json']}
    (OUTPUT/'report-provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
    print(OUTPUT/'report.md')


if __name__=='__main__':build()
