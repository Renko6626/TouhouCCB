"""Inspect fixed-composition responses and pre-trade demand, without tuning."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from .diagnostics import acf

ROOT = Path(__file__).resolve().parent
MODES = ['fixed_m4', 'fixed', 'fixed_m12', 'random', 'performance', 'matched_noise', 'noise']
LABELS = dict(fixed_m4='固定4M/12R/4N', fixed='固定8M/8R/4N', fixed_m12='固定12M/4R/4N',
              random='随机转换', performance='成绩B', matched_noise='匹配4N＋16惰性', noise='20N参照')
COLORS = ['#9333ea','#64748b','#d97706','#16a34a','#2563eb','#db2777','#0891b2']
PLOT_LABELS = dict(fixed_m4='4M / 12R / 4N', fixed='8M / 8R / 4N', fixed_m12='12M / 4R / 4N')


def correlation(x, y):
    return float(np.corrcoef(x,y)[0,1]) if np.std(x)>0 and np.std(y)>0 else None


def block_mean(x, width=100):
    return x[:len(x)//width*width].reshape(-1,width).mean(axis=1)


def inspect(result, x):
    burn = result['config']['burn_in']
    post = slice(burn+1, None)
    r = np.diff(np.log(x['prices']))[burn:]
    d = x['demand_D'][post]
    mu, v = x['predicted_mean'][post], x['predicted_variance'][post]
    second = x['predicted_second_moment'][post]
    d_acf = acf(d,5000)
    residual2 = (r-mu)**2
    f = x['pool_foreign'][:-1][burn:]
    strategy = 2/f*x['strategy_flow'][post]
    noise = 2/f*x['noise_flow'][post]
    covariance = float(np.cov(strategy,noise,ddof=1)[0,1])
    diag, sim = result['diagnostics'], result['simulation']
    nonnoise = sum(a['kind'] in ('M','R') for a in sim['accounts'])
    gap_activity = dict(demand_mean=float(d.mean()), demand_sd=float(d.std(ddof=1)),
        demand_r2_corr=correlation(d,r*r), demand_block_second_moment_corr=correlation(block_mean(d),block_mean(r*r)),
        predicted_actual_second_moment_ratio=float(second.mean()/np.mean(r*r)),
        predicted_residual_variance_ratio=float(v.mean()/residual2.mean()),
        target_near_fraction=float(x['target_near_count'][post].mean()/nonnoise) if nonnoise else None,
        strategy_inventory_sd=float(x['inventory_strategy'][post].std(ddof=1)),
        noise_inventory_sd=float(x['inventory_noise'][post].std(ddof=1)),
        strategy_impact_variance=float(strategy.var(ddof=1)), noise_impact_variance=float(noise.var(ddof=1)),
        two_impact_covariance=2*covariance, linear_impact_residual_rms=float(np.sqrt(np.mean((r-strategy-noise)**2))),
        actual_return_variance=float(r.var(ddof=1)))
    row = dict(mode=result['mode'], seed=result['seed'], return_sd=diag['returns']['std'],
        signed_acf1=diag['signed_acf'][1], absolute_acf1=diag['absolute_acf'][1],
        abs_acf_101_1000=float(np.mean(diag['absolute_acf'][101:1001])),
        return_dfa_512_8192=diag['dfa']['returns']['fits']['512-8192']['alpha'],
        abs_dfa_512_8192=diag['dfa']['absolute_returns']['fits']['512-8192']['alpha'],
        excess_kurtosis=diag['returns']['excess_kurtosis'], nonzero_excess_kurtosis=diag['nonzero_returns']['excess_kurtosis'],
        aggregation_kurtosis_240=diag['aggregation']['240']['excess_kurtosis'],
        strategy_trades_per_tick=float((x['trades_M'][post]+x['trades_R'][post]).mean()),
        noise_trades_per_tick=float(x['trades_N'][post].mean()),
        strategy_gold_volume_per_tick=float((x['volume_M'][post]+x['volume_R'][post]).mean()),
        noise_gold_volume_per_tick=float(x['volume_N'][post].mean()),
        price_range_pct=sim['range_pct'], switches=sim['switches'], **gap_activity,
        **{f'demand_acf_{lo}_{hi}':float(d_acf[lo:hi+1].mean()) if np.isfinite(d_acf[lo:hi+1]).all() else None
           for lo,hi in [(1,1),(2,10),(11,30),(31,100),(101,300),(301,1000),(1001,5000)]})
    # Exact accounting identity, using the actual quantized k_t, not an assumed constant k.
    predicted_log_price = np.log(x['gold']*x['pool_foreign'])-2*np.log(x['pool_foreign'])
    checks = dict(max_log_price_identity_error=float(np.max(abs(predicted_log_price-np.log(x['prices'])))),
                  max_foreign_identity_error=float(np.max(abs(x['pool_foreign']-
                                  (result['config']['foreign']-x['inventory_strategy']-x['inventory_noise'])))))
    return row, d_acf, checks


def table(rows, columns):
    out = ['| '+' | '.join(label for _,label,_ in columns)+' |',
           '| '+' | '.join('---' for _ in columns)+' |']
    for row in rows:
        values = []
        for key, _, fmt in columns:
            value = row.get(key)
            values.append('不适用' if value is None else (fmt.format(value) if fmt else str(value)))
        out.append('| '+' | '.join(values)+' |')
    return '\n'.join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT/'results/switching-v2')
    args = parser.parse_args()
    summary = json.loads((args.source/'summary.json').read_text())
    seeds = sorted({r['seed'] for r in summary['runs']})
    rows, arrays, results, curves, identity_checks = [], {}, {}, {}, {}
    for mode in MODES:
        for seed in seeds:
            key = (mode, seed)
            directory = args.source/f'{mode}-seed{seed}'
            result = json.loads((directory/'result.json').read_text())
            with np.load(directory/'series.npz') as raw:
                x = {k:raw[k] for k in raw.files}
            row, curve, checks = inspect(result,x)
            rows.append(row)
            arrays[key], results[key], curves[key] = x,result,curve
            identity_checks[f'{mode}-seed{seed}'] = checks
            (directory/'response.json').write_text(json.dumps(dict(metrics=row,demand_acf=curve.tolist(),identities=checks),indent=2)+'\n')
    means = {mode:{k:(float(np.mean([r[k] for r in rows if r['mode']==mode]))
                        if all(r[k] is not None for r in rows if r['mode']==mode) else None)
                  for k in rows[0] if k not in ('mode','seed')} for mode in MODES}
    (args.source/'response-summary.json').write_text(json.dumps(dict(means=means,runs=rows,
                           identity_checks=identity_checks,report_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()),indent=2)+'\n')
    with (args.source/'response.csv').open('w') as handle:
        writer = csv.DictWriter(handle,list(rows[0]))
        writer.writeheader(); writer.writerows(rows)

    # Fixed-composition diagnostics: three paired seeds, no pooled concatenation.
    fig,axes = plt.subplots(1,3,figsize=(15,4),constrained_layout=True)
    for mode,color in zip(MODES[:3],COLORS[:3]):
        for seed in seeds:
            result=results[mode,seed]
            axes[0].semilogx(np.arange(1,1001),curves[mode,seed][1:1001],lw=.65,alpha=.6,color=color,
                            label=PLOT_LABELS[mode] if seed==seeds[0] else None)
            axes[1].semilogx(np.arange(1,1001),result['diagnostics']['absolute_acf'][1:1001],lw=.65,alpha=.6,color=color)
            x=arrays[mode,seed]; burn=result['config']['burn_in']
            r=np.diff(np.log(x['prices']))[burn:]
            axes[2].scatter(block_mean(x['demand_D'][burn+1:]),block_mean(r*r),s=3,alpha=.12,color=color)
    for ax,title in zip(axes,['Uncompleted demand ACF','Absolute-return ACF','100-tick mean demand vs mean squared return']):
        ax.set(title=title)
    axes[0].set(xlabel='Lag (ticks)'); axes[1].set(xlabel='Lag (ticks)')
    axes[2].set(xlabel='D (foreign units squared / tick)',ylabel='Mean r squared')
    axes[0].legend(fontsize=7)
    fig.savefig(args.source/'composition.png',dpi=150);plt.close(fig)

    fig,axes=plt.subplots(len(seeds),3,figsize=(15,3*len(seeds)),squeeze=False,constrained_layout=True)
    for i,seed in enumerate(seeds):
        x=arrays['performance',seed]; index=np.arange(len(x['prices']))[::20]
        axes[i,0].plot(index,x['prices'][::20],lw=.65)
        axes[i,1].plot(index,x['inventory_strategy'][::20],lw=.65,label='Strategy active inventory')
        axes[i,1].plot(index,x['inventory_noise'][::20],lw=.65,label='Noise inventory change')
        # D uses a pre-trade index; preserve it rather than shifting onto a later return.
        axes[i,2].plot(index,x['demand_D'][::20],lw=.65,label='Pre-trade D')
        for ax,title in zip(axes[i],['Price','Inventory components','Uncompleted demand']):
            ax.set(title=f'Seed {seed}: {title}',xlabel='Tick')
        axes[i,1].legend(fontsize=7)
    fig.savefig(args.source/'inventory-demand.png',dpi=150);plt.close(fig)

    displayed=[dict(label=LABELS[mode],**means[mode]) for mode in MODES]
    old=[]
    for mode in ('fixed','random','performance'):
        previous=[json.loads((ROOT/f'results/switching/{mode}-seed{s}/result.json').read_text()) for s in seeds]
        old.append(dict(label=LABELS[mode], old_switches=float(np.mean([r['simulation']['switches'] for r in previous])),
            new_switches=means[mode]['switches'],old_acf=float(np.mean([r['diagnostics']['signed_acf'][1] for r in previous])),
            new_acf=means[mode]['signed_acf1'], old_abs_dfa=float(np.mean([r['diagnostics']['dfa']['absolute_returns']['fits']['512-8192']['alpha'] for r in previous])),
            new_abs_dfa=means[mode]['abs_dfa_512_8192']))
    calibration=json.loads((args.source/'calibration.json').read_text())
    validation=json.loads((args.source/'validation.json').read_text())
    text='''# 评分修正与固定策略构成：第二轮实验

2026-10-15。依据用户提供的gpt-discussion-2进行。本轮只修影子评分时点并增加观察量，之后测试固定配比；没有改变资金、阈值、窗口、到达率或执行规则。异质期限、C、滑点评分暂未实施。

## 主要判断

**修正评分没有使成绩B出现持续的中长尺度波动聚集。D确实与波动强度相联系，但自身缺少持续的正相关状态。固定策略配比改变了反馈形式，并非趋势人越多波动就越大。**这不是所有参数下的否定结论，以下判断限于同值30/30窗口、固定阈值及三个种子。

- 固定A和随机组价格/成交完全不变，B的平均转换由336.0变398.7，收益ACF1由−0.0880变−0.0766，绝对收益DFA1从0.5299变0.5138。修正有行为影响，但101–1000tick绝对收益ACF平均仍约0.00009。
- 固定4M/8M/12M的收益标准差分别约0.000807/0.000874/0.000798，8M最高，非单调；三条种子各档数值接近。配比对方向反馈更明显：收益ACF1分别−0.177、−0.045、+0.147。12M组D与波动ACF均先正后负并有回摆，符合振荡型响应，不能当作持续的正相关聚集。
- D与同tick收益平方的相关约0.24–0.38；对100tick不重叠块取均值后约0.69–0.93。平均预测二阶矩/实际二阶矩约1.01，说明这个小订单近似在此设置下有解释力；平均比接近1不等于每tick预测准确或证明因果。
- 固定三配比和B只有约0.3%–1.5%的策略账户-时点接近当前候选目标（差额≤5外币）。所以本轮不能归因为“大部分人到目标后长期停手”；频繁变化的目标让账户持续有差额，关键缺口是这些差额/反馈强度未持续形成高低状态。
- 匹配四噪声参照的收益标准差约0.000177、收益DFA1约0.480；A/B约将瞬时波动放大4.5–4.9倍，但方向收益长尺度DFA远低于它。在相同四人外生噪声下，交易反馈会放大短期变化并使其更可逆；不能只用封闭资金解释方向扩散不足。

继续保留240tick尾部现象：新B平均超额峰度约1.925，随机组约1.959，旧B约3.94。修正改变了这个中间尺度结果，也进一步提醒它不是稳定长记忆证据。

## 设置与组别

每组20账户，池初始100万金币/20万外币，初价5；每人现金5000、外币1000，费用0。100000tick，预热5000，种子41/42/43。每账户每tick独立Poisson(0.05)到达，打乱全部到达后按即时钱包和池子成交。主动幅度500、eta=0.2、入场0.0005/离场0.00025。M信号是过去30tick对数价差，R信号是当前公开对数价EMA减当前公开对数价，alpha=2/31。所有交易决策只读上一收盘；EMA、信号及阈值同值。初始实际/影子z及影子u/成绩均0。

固定目标外币1000+500z，z∈{-1,0,1}为低配/基准/高配；每次到达交易0.2*(目标−真实库存)，达到强阈值进入对应状态，已高/低配信号穿越离场阈值回基准，强反向可直接反转。数量先限于即时池外币的0.5%，再按有限钱包裁剪、6位量化，用真实恒定乘积报价。N每次独立提交40*N(0,1)外币，受同样裁剪。无基本价值、外部玩家、注入、杠杆或波动驱动到达。

原四组为固定8M/8R/4N、随机转换、成绩B、20N参照。新增固定4M/12R/4N、固定12M/4R/4N以及16惰性＋4N，共7组×3种子=21条。M/R改变只在账户ID0–15间，N保持ID16–19；matched_noise保留这四人全部噪声需求，16惰性人仍产生到达并参与事件打乱，但不下单。20N参照的噪声订单方差是四N的五倍，不作严格消融。

result.json中的config为共享参数（默认type_counts=8/8/4），固定配比/惰性模式由mode覆盖初始类型；不可将公共config的type_counts误当作这些模式的实际人数。模式的覆盖值就是上面列出的4/12/4、12/4/4、16I/4N，期末账户行也记录每人实际类型。

随机/B允许原4M+4R的8人转换，每tick独立机会概率0.001；随机组每次M/R各50%，B用sigmoid(beta*(UM−UR))。gamma按账户编号mod3取0.0005/0.001/0.002。转换不重置实际z，不立即成交；在下一次到达按新规则更新目标。两影子按实际到达、同样滞回及20%速度调整u，不交易、不受钱包裁剪，只评分主动敞口，非真实净利润。

## 评分时点与历史一致性

修正后的每tick：冻结期初影子u_hold；读取上一收盘并更新信号；按已知成绩抽选规则；随机执行本tick到达并更新影子u；形成r_t=ln(P_t/P_(t−1))；用pi_t=u_hold*r_t更新U_t=(1−gamma)U_(t−1)+gamma*pi_t。新成绩供下一tick选择。这样新增/退出敞口不覆盖本tick整段收益，最后tick收益也记分。

没有引入真实影子钱包、交易成本或剔除自身对市场价格的影响；修正的是期初/期末敞口归属，不是把学习信号变成可兑现净收益。beta仍只按固定A三个种子、8名资格账户、95000个预热后tick的个人成绩差RMS定标，先平方再合并，未按市场指标挑beta。

'''
    text+=f"修正后RMS={calibration['rms']:.10g}，beta={calibration['beta']:.8f}（旧beta=47443.70729445）。固定A、随机组和20N共{len(validation['previous_market_series_identical'])}条历史市场序列逐点相同；score_difference发生变化不在不变检查内。新固定配比和匹配噪声的到达/原始噪声与A逐点配对。\n\n"
    text+='修正前后跨种子平均：\n\n'+table(old,[('label','组别',''),('old_switches','旧转换','{:.1f}'),('new_switches','新转换','{:.1f}'),
             ('old_acf','旧收益ACF1','{:.5f}'),('new_acf','新收益ACF1','{:.5f}'),
             ('old_abs_dfa','旧绝对收益DFA1','{:.4f}'),('new_abs_dfa','新绝对收益DFA1','{:.4f}')])+'\n\n'
    text+='''## 新诊断量与口径

**成交前需求：**D[t]=sum_(M/R) lambda*eta²*d_i²，其中d_i为“按当前公开信号、当前实际z，在下一次到达会采用的目标”减当前库存；观察不改变实际z。使用当前候选目标，不能直接用尚未在到达时更新的旧z，也不能使用成交后库存。D单位外币²/tick，未作余额裁剪。

小订单近似：V[t]=(2/F_(t−1))²*(D[t]+N_noise*lambda*40²)，mu[t]=(2/F_(t−1))*sum_(M/R)lambda*eta*d_i；二阶矩近似V+mu²。它们预测同tick r_t，而非下一tick；忽略钱包裁剪、曲线非线性和同tick多到达后差额递减。r_t²的期望是二阶矩，不能直接作为有漂移时的条件方差。另比较V与(r_t−mu[t])²。报告平均预测/平均观测比，以及D与r²/100tick块均值的描述性相关性，不作因果或拟合优度结论。

**成交后库存：**S[t]=sum_(M/R)(h_i−1000)，DeltaHN[t]=sum_(N)(h_i−1000)，惰性账户不动。验证F[t]=200000−S[t]−DeltaHN[t]。真实价格满足p_t=ln(k_t)−2ln(F_t)，使用实际k_t=G_t*F_t，允许量化使k微增。线性分解r_strategy=2/F_(t−1)*DeltaS，r_noise=2/F_(t−1)*DeltaHN；报告两者方差及2Cov，不能只把方差相加而忽略抵销。恒等式本身不是机制因果证据。

**接近目标：**成交前|d_i|≤5外币（主动幅度的1%）的M/R人数/非噪声策略人数；只作诊断，不改变交易容差。它区别于钱包余额<1e−6的触底统计。随机/B类型人数计的是身份，不保证仍有待执行订单。

固定目标且无裁剪时，E[d(t)]∝exp(−lambda*eta*t)，平均松弛100tick；E[d(t)²]∝exp(−lambda*(2eta−eta²)*t)，平方差额松弛55.6tick。这是理想静态目标推导，实际目标会变，不直接当作本实验测得的衰减时间。

## 指标、样本与统计限制

主指标采用r5001…r100000，共95000个自然对数收益。D和预测量同样取成交前tick5001…100000，与r逐点对应；库存用对应收盘。零收益保留在ACF/DFA中。总转换、总成交、价格范围用完整100000tick；新活动表的成交/金币量每tick均值仅用预热后。

收益标准差ddof=1，不年化。超额峰度为SciPy fisher=True,bias=False，高斯基准0；非零峰度只过滤r==0，不改其他指标时间轴。聚合收益按1/5/30/60/240/1000tick从预热后起不重叠求和，尾端不足块舍弃，240tick只有395样本、1000tick95样本。

ACF_x(k)=sum_(j=1…N−k)(x_j−mean(x))*(x_(j+k)−mean(x))/sum_(j=1…N)(x_j−mean(x))²，FFT计算，不乘N/(N−k)修正；原始r、|r|、r²及新D各自去全样本均值，最大滞后5000。101–1000均值包含900个lag，不是1000tick聚合。常数D返回NaN，在汇总表显示不适用。

DFA先对r或|r|去均值并累积，前后双向分块，每方向至少8块，拟合一阶/二阶趋势并对残差平方取平均开方。尺度16,24,32,48,64,96,128,192,256,384,512,768,1024,1536,2048,3072,4096,6144,8192；log尺度上OLS拟合32–256、512–8192、2048–8192。下面列512–8192的DFA1斜率，非认证Hurst参数；无置信区间或洗牌显著性。

每条独立计算后跨三个种子等权平均，不拼接收益。100tick块关系采用950个不重叠块，变量取块均值，不是滚动图。D与r²的相关性、方差预测比和库存分解均是描述量，不证明外生因果；三种子不足以推广所有参数。

## 市场指标（跨种子平均）

'''
    text+=table(displayed,[('label','组别',''),('return_sd','收益标准差','{:.6g}'),('signed_acf1','收益ACF1','{:.5f}'),
        ('abs_acf_101_1000','绝对收益ACF101–1000','{:.5f}'),('return_dfa_512_8192','收益DFA1','{:.4f}'),
        ('abs_dfa_512_8192','绝对收益DFA1','{:.4f}'),('aggregation_kurtosis_240','240tick超额峰度','{:.3f}')])+'\n\n'
    text+='## 订单活动与需求持续性（跨种子平均）\n\n'+table(displayed,[('label','组别',''),('demand_mean','D均值','{:.2f}'),
        ('strategy_trades_per_tick','策略成交/tick','{:.4f}'),('strategy_gold_volume_per_tick','策略金币量/tick','{:.2f}'),
        ('target_near_fraction','接近目标比例','{:.3f}'),('demand_acf_31_100','D ACF31–100','{:.4f}'),
        ('demand_acf_101_300','D ACF101–300','{:.4f}'),('demand_acf_301_1000','D ACF301–1000','{:.4f}')])+'\n\n'
    text+='## 预测诊断与库存分解（跨种子平均）\n\n'+table(displayed,[('label','组别',''),
        ('demand_r2_corr','D与r²相关','{:.3f}'),('demand_block_second_moment_corr','100tick块相关','{:.3f}'),
        ('predicted_actual_second_moment_ratio','预测/实际二阶矩','{:.3f}'),('predicted_residual_variance_ratio','V/实际残差²','{:.3f}'),
        ('strategy_inventory_sd','策略库存标准差','{:.1f}'),('noise_inventory_sd','噪声库存标准差','{:.1f}')])+'\n\n'
    decomposition = [dict(label=LABELS[mode],
        strategy_pct=100*means[mode]['strategy_impact_variance']/means[mode]['actual_return_variance'],
        noise_pct=100*means[mode]['noise_impact_variance']/means[mode]['actual_return_variance'],
        covariance_pct=100*means[mode]['two_impact_covariance']/means[mode]['actual_return_variance']) for mode in MODES]
    text+='线性成交流分解（各项跨种子方差均值/实际收益方差均值，百分比；不是因果贡献）：\n\n'+table(decomposition,
        [('label','组别',''),('strategy_pct','策略方差/总方差%','{:.2f}'),('noise_pct','噪声方差/总方差%','{:.2f}'),
         ('covariance_pct','2Cov/总方差%','{:.2f}')])+'\n\n'
    text+='策略组成交约占瞬时收益方差的95%–96%，噪声约4%–5%；但库存水平的慢变化不等于成交流方差。D、主动库存与噪声库存已分开保存，不能由身份人数直接替代。数值分解残差来自线性近似及量化，份额不要求精确合计100%。\n\n'
    text+='''完整种子级指标及方差/协方差分解保存在[response.csv](results/switching-v2/response.csv)和[response-summary.json](results/switching-v2/response-summary.json)；下面先保留关键种子差异。

'''
    text+=table([dict(label=LABELS[r['mode']],**r) for r in rows if r['mode'] in ('fixed_m4','fixed','fixed_m12','performance')],
        [('label','组别',''),('seed','种子','{:.0f}'),('return_sd','收益标准差','{:.6g}'),('demand_mean','D均值','{:.2f}'),
         ('abs_acf_101_1000','绝对收益ACF101–1000','{:.5f}'),('demand_acf_101_300','D ACF101–300','{:.4f}')])+'\n\n'
    text+='''## 证据、图与复算

- [固定配比的需求ACF、波动ACF、块关系](results/switching-v2/composition.png)
- [成绩B价格、库存与需求轨迹](results/switching-v2/inventory-demand.png)
- [配对及旧路径不变检查](results/switching-v2/validation.json)、[新成绩定标](results/switching-v2/calibration.json)
- [完整实验元数据](results/switching-v2/summary.json)，各组目录result.json包含7个源码SHA256、实际配置、账户期末余额/成绩；series.npz保存原始序列、response.json保存D ACF及身份检查。
- 旧结果仍在results/switching，旧实现已复制到其source目录。此次修正没有覆盖旧实验数据。

```bash
MPLCONFIGDIR=/tmp/sunyunbo/fx-matplotlib OPENBLAS_NUM_THREADS=1 python -m unittest discover -s research/fx/tests -v
MPLCONFIGDIR=/tmp/sunyunbo/fx-matplotlib OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m research.fx.switching_run --workers 3
MPLCONFIGDIR=/tmp/sunyunbo/fx-matplotlib OPENBLAS_NUM_THREADS=1 python -m research.fx.response_report
```

默认输出results/switching-v2。没有改生产市场，没有运行异质期限或C，没有通过寻找最佳窗口/阈值美化结果。

## 下一步建议与未验证项

评分问题已修，固定配比与需求传导已检查。接下来可以在相同资金/人数/阈值/到达率下，只引入少数相隔较大的个人观察期限，覆盖约100tick调仓尺度的两侧，再比较固定与转换；要回答的是D能否更持续，而不只比较峰度或类型人数。12M组已显示振荡，异质期限可能缓和同步，也可能只是换一个周期，不能预设有效。

本轮固定配比实验是三档长期稳态对照，没有额外施加外部信号脉冲或强制瞬时换类型，因此没有直接测得单次扰动响应/持仓期限分布。报告以D与波动ACF描述响应持续性，不把它说成脉冲响应实验。尚未做正式显著性、尾部拟合、D的独立因果识别、异质期限或C；这些不能由21条完成实验推断为已验证。

相关unittest共8项通过，包含期初敞口、新建/退出、诊断不改变state、匹配噪声与真实库存分解；21条模拟全部两种资产守恒。旧九条市场序列逐点一致，其他同种子组到达及原始噪声逐点配对。图已生成并查看。
'''
    (ROOT/'switching-v2-findings.md').write_text(text)
    for mode in MODES:
        print(mode,json.dumps(means[mode]))


if __name__ == '__main__':
    main()
