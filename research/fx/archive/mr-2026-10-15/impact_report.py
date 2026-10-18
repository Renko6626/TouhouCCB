"""Write a standalone report for the six new amplitude runs and reused v2 data."""
import argparse
import hashlib
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from .response_report import table

ROOT=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=ROOT/'results/impact-v3')
    args=parser.parse_args()
    summary=json.loads((args.source/'summary.json').read_text())
    existing=json.loads((args.source/'existing-analysis.json').read_text())['runs']
    impact_rows=[]
    for a in [125,250,500]:
        for seed in [41,42,43]:
            directory=(ROOT/'results/switching-v2' if a==500 else args.source/f'a{a}')/f'fixed-seed{seed}'
            with np.load(directory/'series.npz') as x:
                r=np.diff(np.log(x['prices']))[5000:]
                s=2/x['pool_foreign'][:-1][5000:]*x['strategy_flow'][5001:]
                n=2/x['pool_foreign'][:-1][5000:]*x['noise_flow'][5001:]
                impact_rows.append(dict(amplitude=a,seed=seed,strategy_var_share=float(s.var(ddof=1)/r.var(ddof=1)),
                    noise_var_share=float(n.var(ddof=1)/r.var(ddof=1)),two_cov_share=float(2*np.cov(s,n,ddof=1)[0,1]/r.var(ddof=1))))
    shares={a:float(np.mean([r['strategy_var_share'] for r in impact_rows if r['amplitude']==a])) for a in [125,250,500]}
    displayed=[dict(amplitude=a,**summary['means'][str(a)],strategy_var_share=shares[a]) for a in [125,250,500]]
    inventory=[]; states=[]
    for mode,label in [('fixed_m4','4M/12R'),('fixed','8M/8R'),('fixed_m12','12M/4R'),('performance','成绩B')]:
        selected=[r for r in existing if r['mode']==mode]
        for width in [100,240,1000,8192,16000]:
            curves=[next(c for c in r['inventory'] if c['width']==width) for r in selected]
            inventory.append(dict(label=label,width=width,**{key:float(np.mean([c[key] for c in curves]))
                             for key in ['strategy_var','noise_var','two_cov','net_inventory_var']}))
        states.append(dict(label=label,
            acf101=float(np.mean([r['persistence']['W_acf_bands']['101-300'] for r in selected])),
            acf301=float(np.mean([r['persistence']['W_acf_bands']['301-1000'] for r in selected])),
            drift_share=float(np.mean([r['persistence']['drift_share'] for r in selected])),
            **{f'{which}_{width}':float(np.mean([next(b for b in r['persistence']['blocks'] if b['width']==width)[f'{which}_block_corr']
                      for r in selected])) for which in ['same','next'] for width in [30,100,240,1000]}))
    fig,axes=plt.subplots(1,2,figsize=(12,4),constrained_layout=True)
    for mode,label,color in [('fixed_m4','4M/12R','#9333ea'),('fixed','8M/8R','#64748b'),('fixed_m12','12M/4R','#d97706'),('performance','B','#2563eb')]:
        selected=[r for r in existing if r['mode']==mode]
        for i,r in enumerate(selected):
            axes[0].semilogx(np.arange(1,5001),r['persistence']['W_acf'][1:],lw=.6,alpha=.6,color=color,label=label if i==0 else None)
            blocks=r['persistence']['blocks']
            axes[1].semilogx([b['width'] for b in blocks],[b['next_block_corr'] for b in blocks],'o-',lw=.7,alpha=.6,color=color)
    axes[0].set(title='Existing full W autocorrelation',xlabel='Lag (ticks)')
    axes[1].set(title='Existing current W block vs next squared-return block',xlabel='Block width (ticks)')
    for ax in axes:ax.axhline(0,lw=.7,color='#94a3b8')
    axes[0].legend(fontsize=8)
    fig.savefig(args.source/'existing-W.png',dpi=150);plt.close(fig)
    text='''# 第三轮：单笔冲击强度与跨期库存抵销

2026-10-15。按gpt-discussion-3执行。先分析第二轮原始序列，再仅改变固定8M/8R/4N的主动幅度A=125/250；A=500沿用第二轮三条路径。新增6条100000tick模拟，三个种子41/42/43，成绩B冻结，不运行异质期限或C。以下是实测结果及明确的统计口径，适合单独发给外部模型审阅。

## 主要发现

**减弱单笔反馈会降低波动和强信号频率，但三档A都没有形成持续的中长尺度高低波动状态。跨期库存数据支持“即时放大、累计抵销”的解释；它仍是当前参数下的机制证据，不是对所有固定目标模型的普遍证明。**

- A从500降至125，收益标准差从0.000874降到0.000254；M强信号比例从90.6%降到74.8%，R从74.0%降到43.3%。R候选变化率从19.1%降到14.5%/账户-tick；M候选变化率是9.19%/11.06%/10.72%（A125/250/500），没有单调关系。因此不宜说反馈减弱彻底消除了同步目标变化，或已证明单笔越阈值是唯一瓶颈。
- A125/250的策略线性冲击方差份额仍约51.9%/83.1%，不是策略几乎失去影响的纯噪声极限；但绝对收益ACF101–1000均值都约−0.00018，W到下一100tick块相关分别−0.072/−0.087，A500为−0.089，没有出现稳定的跨块正持续性。小A改善方向反转及部分有限尺度DFA，却不能称为波动聚集成功。
- 原8M/8R组的策略库存差分方差，在1000/8192/16000tick约0.752/0.751/0.736百万外币²，已接近平台；噪声差分方差对应0.274/1.434/1.916百万，继续增加。大量策略成交的长期净位移没有同步累积。这支持抵销解释，但没有做正式平稳性检验。
- 跨期2Cov不总能忽略：原4M/12R组240tick的2Cov约−94384，足以抵销两方单独方差和的一半以上；同tick协方差接近零不能替代跨期限检查。12M/4R组则以大幅策略库存振荡为主，策略差分方差从240tick约19.21百万回到16000tick约13.93百万。
- 完整W没有揭示之前D漏掉的持久状态。原12M组下一30tick块相关约+0.482，下一100/240tick块约−0.207/−0.360，符合有限尺度回摆，而非在各尺度持续为正。原A500和B也只在短块有较弱延续，同100tick块高相关约0.706/0.716不等于下一块高相关。

还需限制理论外推：原8M噪声库存差分方差在8192tick只有理想320*w的约55%，16000tick约37%。钱包约束和跨期结构已使它偏离未裁剪随机游走，不能把320*w无限延伸，或把讨论稿粗估的交叉期限当成实测DFA预测。

## 问题与设置

检验两个假说：①一单冲击相对阈值过强，同质M/R可能频繁切换候选目标；②策略库存往返造成即时波动，在较长持有期发生抵销。chi不是稳定性特征值，chi=1不是已证明的临界点。降低A即使让收益更接近噪声，也不能自动认定实现波动聚集。

主动幅度实验的三档均20账户（0–7为M、8–15为R、16–19为N），池初始金币1000000/外币200000，P0=5。每人现金5000、基准及初始库存1000外币，始终固定；改变A不改变钱包或基准。原代码base=2A的初始化绑定已解除并有回归检查。目标h*=1000+A*z，z∈{-1,0,+1}，每次到达q_want=0.2*(h*−h)。三档目标分别875/1000/1125、750/1000/1250、500/1000/1500。

每人每tick独立Poisson(0.05)到达，打乱全部事件，逐笔按即时池子和钱包真实成交。费用0，eta=0.2，订单外币数量先限于即时池外币储备0.5%，再裁剪余额；生产恒定乘积报价、6位量化保留。N每次独立提交40*N(0,1)外币，不因A调整噪声。不做空、借贷、外部注入或参与率反馈。

每tick交易信号只读上一收盘。M用30tick对数价差（不除窗口），R用当前对数价EMA减当前对数价，alpha=2/31，M/R系数都1，信号不加随机项。初始EMA=ln5、实际/影子状态及成绩0。|e|>0.0005强入场；已高配e<0.00025回基准，已低配e>−0.00025回基准；强反向优先反转，比较均严格。未到达不会改变实际z。本轮fixed不采用任何成绩重选，真实转换数0。

初始首次订单eta*A为25/50/100外币。chi=2*eta*A/(F0*theta_on)=0.5/1/2；无量化理想买单对数冲击为−2ln(1−eta*A/F0)，分别0.000250016/0.000500063/0.001000250。卖单略不同；实际后续订单不必等于首次量，受到库存差额和余额约束。

## 数据来源、重放与配对

第二轮七组共21条保存路径作跨期限和W分析：固定4M/12R、8M/8R、12M/4R（均4N）、随机转换、成绩B、16惰性＋4N匹配参照、20N参照。没有修改这些价格/成交序列，也没有重新运行B。这里旧B仅作诊断对象。

A=500原文件没有本轮信号/候选变动字段。用它的完整保存价格和原Poisson到达，重放EMA、滞回和下一唤醒候选目标；不调用AMM、不改变钱包、不生成新价格。信号同tick固定且状态映射幂等，重复唤醒与事件顺序不会改变本tick最后状态。重放包含启动/预热记忆，不在5000tick重置状态。用当前A500前1000tick验证原所有保存字段逐点一致，同时验证诊断重放与实时采集逐点一致；这1000tick只是验证前缀，不是新增完整市场实验。

新A125/250全部10万tick，与旧A500同种子到达总数及未裁剪原始噪声需求逐点相同。资产总量仍1100000金币、220000外币，两种资产分别守恒；每条钱包非负。原A500源码快照保存在第二轮source目录，新六条记录当前7个模拟源码hash；复用路径维持其原源码hash，不伪装成新生成结果。

## 跨期限库存分解：口径与结果

使用收盘库存S_t=sum_(M/R)(h_i−1000)和N_t=sum_(N)(h_i−1000)，预热边界S5000…S100000，共95001个状态点。对w=1/5/10/30/60/100/240/500/1000/2000/4000/8192/16000取所有重叠差分DeltaS=S_(t+w)−S_t、DeltaN=N_(t+w)−N_t；每个w有95000−w+1个样本，样本方差和协方差均ddof=1。

逐条验证Var(DeltaS+DeltaN)=Var(DeltaS)+Var(DeltaN)+2Cov(DeltaS,DeltaN)。表中方差单位外币²，不是收益方差，跨种子对各项先独立计算再算术平均；没有把重叠窗口当独立实验，也不拼接收益。另保存实际累计对数收益方差及(2/F0)²乘净库存差分方差的初始池深线性近似。近似忽略储备变化和k量化漂移，不当作精确价格分解。

静态平稳库存假设下的2Var(S)平台只是参考；实际用曲线检查，不直接假设库存已经平稳。四N未裁剪独立需求的理想噪声差分方差320*w仅作参考，钱包反射、跨期反馈和边界会偏离它；20N参照对应1600*w，不使用320*w归一化。

'''
    text+=table(inventory,[('label','原组别',''),('width','期限tick','{:.0f}'),('strategy_var','策略差分方差','{:.0f}'),
        ('noise_var','噪声差分方差','{:.0f}'),('two_cov','2Cov','{:.0f}'),('net_inventory_var','净差分方差','{:.0f}')])+'\n\n'
    text+='''## 完整W的持续性与跨块关系

W_t=V_t+mu_t²，在tick t成交前计算，预测r_t²；V=(2/F_(t−1))²*(D+N_noise*lambda*40²)，D=sum_(M/R)lambda*eta²*(候选目标−真实库存)²；mu=(2/F_(t−1))*sum lambda*eta*(候选目标−库存)。忽略裁剪、曲线非线性和同tick多次到达后的差额缩小。漂移份额为mean(mu²)/mean(W)，不能漏掉这一项。

使用W5001…W100000与r5001…r100000²，ACF公式为全样本去均值的滞后乘积和除零滞后平方和，FFT到5000，不乘N/(N−k)修正。101–300及301–1000为含端点的lag算术平均。

块宽30/100/240/1000，每块取W和r²算术均值，不重叠、不跨预热，尾部不足一块舍弃；分别3166/950/395/95个完整块，下一块配对为3165/949/394/94个。Same相关是Corr(Wbar_b,r²bar_b)，Next是Corr(Wbar_b,r²bar_(b+1))。Next仍是描述性序列相关，不是因果识别或交易预测收益；块宽预先固定，不挑最好尺度。跨三个种子的相关系数取等权算术平均，无Fisher-z变换、置信区间或显著性检验。

'''
    text+=table(states,[('label','原组别',''),('drift_share','漂移在W中份额','{:.3f}'),('acf101','W ACF101–300','{:.4f}'),
        ('acf301','W ACF301–1000','{:.4f}'),('same_100','同100tick块相关','{:.3f}'),
        ('next_30','下一30tick块','{:.3f}'),('next_100','下一100tick块','{:.3f}'),
        ('next_240','下一240tick块','{:.3f}'),('next_1000','下一1000tick块','{:.3f}')])+'\n\n'
    text+='''## A单因素实验：市场指标与同步变化

主统计仍为95000个自然对数收益，保留零收益，不年化；标准差ddof=1。绝对收益ACF101–1000是900个lag平均。DFA1为去均值累计剖面、前后双向分块、每方向至少8块，一阶去趋势；固定尺度16…8192，与前两轮同列表，log(s)/log(F)上OLS拟合512–8192。是有限尺度斜率，非认证Hurst参数。

strong_M/R为|e_i|>theta_on的账户-时点比例，各类型8人；同质信号下，同类型同时越界，所以也是该规则强信号tick比例。candidate_changes_M/R是本tick成交前候选z相对上一tick成交前候选z不同的账户-时点比例，分母8*95000；包含强反转或回基准，不要求账户实际唤醒、不等于已执行目标/类型转换次数。候选观察不改实际state；阈值相等按严格规则不计强信号。

策略方差份额使用逐tick线性价格冲击r_S≈2/F_(t−1)*DeltaS的样本方差除实际r样本方差，逐种子先算再平均；不是成交额份额或因果贡献，噪声及2Cov另存。不要求份额相加严格为1。

'''
    text+=table(displayed,[('amplitude','A','{:.0f}'),('chi','chi','{:.1f}'),('return_sd','收益标准差','{:.6g}'),
        ('signed_acf1','收益ACF1','{:.4f}'),('abs_acf_101_1000','绝对收益ACF101–1000','{:.5f}'),
        ('return_dfa_512_8192','收益DFA1','{:.4f}'),('abs_dfa_512_8192','绝对收益DFA1','{:.4f}'),
        ('strategy_var_share','策略方差份额','{:.3f}')])+'\n\n'
    text+=table(displayed,[('amplitude','A','{:.0f}'),('strong_M','M强信号比例','{:.3f}'),('strong_R','R强信号比例','{:.3f}'),
        ('candidate_changes_M','M候选变化率/tick','{:.4f}'),('candidate_changes_R','R候选变化率/tick','{:.4f}'),
        ('strategy_gold_volume_per_tick','策略金币量/tick','{:.2f}'),('W_acf_101_300','W ACF101–300','{:.4f}'),
        ('W_next_block_100','W到下一100tick块','{:.3f}')])+'\n\n'
    text+='种子级结果（不以均值替代重复实验差异）：\n\n'+table(summary['runs'],[('amplitude','A','{:.0f}'),('seed','种子','{:.0f}'),
        ('return_sd','收益标准差','{:.6g}'),('abs_acf_101_1000','绝对收益ACF101–1000','{:.5f}'),
        ('strong_M','M强信号','{:.3f}'),('strong_R','R强信号','{:.3f}'),('W_next_block_100','下一100tick块相关','{:.3f}')])+'\n\n'
    text+='''## 证据、复算和限制

- [跨期限库存方差/协方差图](results/impact-v3/inventory-horizons.png)、[原组完整W曲线与下一块关系](results/impact-v3/existing-W.png)
- [三档A的W/波动ACF与下一块关系](results/impact-v3/amplitude-persistence.png)
- [全部九条比较与配对记录](results/impact-v3/summary.json)、[旧21条完整跨期限/W分析](results/impact-v3/existing-analysis.json)
- 新六条各有result.json、series.npz，A500另有三个target-replay文件；report-analysis.json包含线性方差份额及报告源码hash。

```bash
MPLCONFIGDIR=/tmp/sunyunbo/fx-matplotlib OPENBLAS_NUM_THREADS=1 python -m unittest discover -s research/fx/tests -v
MPLCONFIGDIR=/tmp/sunyunbo/fx-matplotlib OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m research.fx.impact_study --workers 3
MPLCONFIGDIR=/tmp/sunyunbo/fx-matplotlib OPENBLAS_NUM_THREADS=1 python -m research.fx.impact_report
```

仅修改离线研究。没有将更小A、接近0.5的DFA或单个块相关性当作模型成功。三种子、单一参数区域、重叠库存差分、有限钱包都限制推广。没有正式平稳性、显著性、尾部幂律或脉冲响应实验；没有测试异质期限，也没有重新优化B。

## 下一步与停止条件

本轮完成了反馈强度和累计库存检查。下一步若继续，按讨论稿只做一轮定义清楚的异质期限实验，并先明确阈值口径：保留绝对阈值的完整变体，或用匹配噪声参照离线校准各窗口信号尺度的机制实验。长窗口会改变信号幅度，不能把所有变化归因为时间结构；不加入在线波动归一化。

异质期限尚未选择或运行。如果这一轮之后仍主要是短期放大、库存回摆和缺少持久W，就暂停固定目标A/B路线，不继续调beta或叠加零散参数。届时再单独提出能够持续调制订单强度的机制。当前没有自动开启参与率C。

相关unittest共11项通过，覆盖钱包固定、原滞回/评分、协方差抵销与下一块时点；六条新增模拟资产分别守恒，与复用A500到达及原始噪声配对。A500诊断重放和原轨迹1000tick前缀检查通过，图已生成并查看。
'''
    (ROOT/'impact-v3-findings.md').write_text(text)
    (args.source/'report-analysis.json').write_text(json.dumps(dict(impact_variance=impact_rows,
        report_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()),indent=2)+'\n')
    for row in displayed: print(json.dumps(row),flush=True)


if __name__=='__main__':main()
