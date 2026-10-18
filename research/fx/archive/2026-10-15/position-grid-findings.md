# 有界目标持仓网格实验

2026-10-14。沿用已批准的三因子、tanh目标持仓、泊松到达和生产Decimal AMM，只扫描参数，不改交易算法或线上配置。

完成174条运行，合计2100万tick；全部完成，零拒单，每个账户的净外币持仓均未越过自身上限，双币种含手续费守恒。按数值参数去重为169条：有基准跨扫描重复，另有整数/浮点表示导致的重复；五条重复序列逐数组完全相同，不能当作额外独立证据。初筛156条，确认18条。

**主要发现：有界持仓解决了连续同向交易累积巨额净头寸的问题，但没有消除集体反馈。原参数仍会产生很强的周期；降低趋势系数或调仓速度可以得到更温和的行情。上限250的一组出现跨种子重复的波动持续性线索，同时也有明显周期，尚不能判为幂律长记忆。**

典型Q250、seed21已抽出单独的 [价格图与针对性分析](figures/position-cap250-seed21/README.md)，包含局部收益、非重叠块波动、频谱及真实市场对照。

随后按用户要求单跑b=1、sigma=0.001，其他沿用Q250、seed21参数，结果见 [增强回归与减弱噪声对照](figures/position-strong-reversion-seed21/README.md)。这条追加运行不计入上面的174条网格数量。

## 模型和扫描范围

`gap = a*T + b*M + sigma*epsilon`，T为窗口平均单tick对数收益，M为历史对数均价减当前对数价。

`q_target = Q*tanh(100*gap)`，每次到达请求`alpha*(q_target-q_actual)`；Q单位是外币。买入实际数量受费用和滑点影响，下一次更新读取真实持仓。订单金额由调仓决定，200本币固定订单配置在本轮不生效。

默认20机器人，每机器人到达率0.05/tick，Q=500，alpha=0.2，a=30，b=0.1，sigma=0.005，趋势与回归共用窗口30。初始池100万本币/20万外币，汇率5，手续费0.05%，无央行注资。

| 扫描 | 运行数 | 每条tick | 参数 |
| --- | ---: | ---: | --- |
| 三因子与窗口 | 108 | 100,000 | a=0/3/10/30，b=0/0.1/1，sigma=0.001/0.005/0.02，L=30/100/300；seed11 |
| 容量与速度 | 30 | 100,000 | Q=50/100/250/500/1000，alpha=0.05/0.2/1；普通a30,b0.1,sigma0.005与极端a100,b0,sigma0.001；seed11 |
| 人数对照 | 18 | 100,000 | N=5/20/80，固定每人Q与rate，或固定NQ=10000与N*rate=1；seed11/12/13，含3条数值重复 |
| 长序列确认 | 18 | 300,000 | 下表6组，各seed21/22/23；尾部自举200次，60tick块打乱10次 |

初筛丢弃前2000tick，确认丢弃前5000tick。三个系数与窗口做联合网格，Q和alpha在两个预期参数组下联合扫描；没有穷举所有维度的笛卡尔积，也没有声称找到了全局最优参数。确认组覆盖弱趋势、原参数、容量中间区域、慢调仓和长窗口，避免只确认初筛的最大单项指标。

## 三个新种子的长序列结果

以下为三种子均值，b=0.1、sigma=0.005、20人及rate=0.05相同。价格区间定义为`max(P)/min(P)-1`，取完整30万tick；它包含费用导致的慢漂移，不是单轮振幅，更没有换算成6小时活动的涨跌幅。

| a | Q外币 | alpha | L | 价格区间 | 本币成交总额 | 收益ACF(1) | 归一化超额峰度 | 归一化绝对收益ACF(1) | R/S大块斜率q20 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 500 | 0.2 | 30 | 4.71% | 5537万 | -0.012 | 2.47 | -0.001 | 0.484 |
| 10 | 500 | 0.2 | 30 | 6.24% | 5598万 | 0.040 | 2.70 | 0.001 | 0.504 |
| 30 | 500 | 0.2 | 30 | 20.34% | 11094万 | 0.462 | 3.35 | 0.133 | 0.380 |
| 30 | 250 | 0.2 | 30 | 5.84% | 3428万 | 0.306 | 3.64 | 0.064 | 0.666 |
| 30 | 500 | 0.05 | 30 | 2.15% | 1341万 | 0.039 | 2.66 | 0.001 | 0.499 |
| 30 | 500 | 0.2 | 300 | 5.52% | 5532万 | -0.004 | 2.51 | -0.001 | 0.481 |

a=10的价格区间为6.02%～6.46%，收益ACF(1)为0.039～0.041；慢调仓组区间2.05%～2.22%。它们适合作为后续日常行情基线，波动聚集较弱。这里只推荐继续研究，不自动修改机器人默认值。

原a30、Q500、alpha0.2组在三个种子下都有约303tick的收益谱峰。峰邻近三个频率格占非零频谱功率34.0%～34.8%，相关曲线周期性正负交替；其厚尾和短期波动相关不能直接当作真实市场式波动聚集。这组适合检查集体反馈，作为日常基线则周期过强。

Q250组的大块R/S斜率为0.649～0.679，三个种子均高于各自10次块打乱的参考上界0.5576/0.5593/0.5611（按对应种子阅读原始数据）。归一化绝对收益100～1000滞后的平均相关0.00326～0.00354，但约248～256tick谱峰仍集中11.4%～12.3%功率。可以保留“有限尺度波动持续性候选”，不能将这几个量升级成正式长记忆检验或Hurst估计。图中可见周期振幅时强时弱，收益相关到数千tick仍含正负摆动；尚未验证绝对收益ACF的幂律衰减、百万tick稳定性和更大尺度下的持续性。

![不同趋势与窗口的行情和相关曲线](results/position-grid-overview/prices-and-correlations.png)

![容量250候选的三个确认种子](results/position-grid-overview/candidate-persistence.png)

## 人数、成交量和时间尺度

固定每人Q=500、rate=0.05，N=5/20/80的三种子平均价格区间为1.77%/16.12%/116.34%，收益ACF(1)为0.029/0.459/0.780。人数增加时总净持仓容量和总到达率都增加，不能把它解释成单纯账户数效应。80人仍未越过净持仓上限，但价格波动显然过强。

固定总容量NQ=10000外币、总到达率N*rate=1，N=5/20/80的平均区间为27.39%/16.12%/1.80%，归一化超额峰度为8.21/3.45/2.60。分成更多小账户减小单次调仓；每个账户到达更慢，目标追随速度也改变，因此这不是完全保持动力学不变的账户拆分实验。

这里至少有四个时间量：历史窗口L、平均每人到达间隔1/rate=20tick、恒定目标下期望误差衰减尺度1/(rate*alpha)=100tick，以及共同反馈产生的约250或300tick周期。前三个是设定，最后一个是涌现结果；窗口长度不等于行情周期。上述固定总容量与总到达率的N5/20/80对照，个人误差衰减尺度分别为25/100/400tick。

净持仓有界依然不限制无限时间的往返成交总额。普通参数Q500、alpha0.2确认运行平均成交1.11亿本币，外币手续费约5389；慢调仓组成交1341万，手续费约669。流动性随手续费改变，长时间研究仍需记录池深，不能把持仓界当作永不耗尽的证明。

## 检验口径、复算和验证

全部18条确认数据用连续iid Pareto尾部自举。a10组p分别约0.005/0.184/0.104，其余确认组均低于0.1。拟合的绝对收益尾部跨度仅约0.28～0.59个数量级，各种子beta也不稳定；当前无稳定幂律证据。某个p较大只表示该次未拒绝特定连续iid尾部假设，不能证明幂律；拒绝该假设也不等于没有厚尾。三组弱反馈的归一化超额峰度约2.5～2.8，在没有明显波动聚集时仍出现，泊松稀疏成交和调仓分布已有影响。

继续复用已审查的ACF、尾部与Lo修正R/S实现。新增谱指标仅是描述：对burn-in后的单tick对数收益用Welch平均谱，段长8192、常数去均值、默认Hann窗和50%重叠，排除零频，报告最大谱格的倒数及邻近三格功率占比。频率分辨率1/8192；峰值是离散谱格读数，不给周期置信区间或周期显著性检验。IID控制三格占比约0.0012，已知256tick正弦控制精确读到256且占比接近1；控制不能替代市场真实性验证。

```bash
python -m research.fx.experiments --config research/fx/configs/position-factor-grid.json --output research/fx/results/position-factor-grid --workers 16
python -m research.fx.experiments --config research/fx/configs/position-capacity-grid.json --output research/fx/results/position-capacity-grid --workers 8
python -m research.fx.experiments --config research/fx/configs/position-population-grid.json --output research/fx/results/position-population-grid --workers 4
python -m research.fx.experiments --config research/fx/configs/position-grid-confirmation.json --output research/fx/results/position-grid-confirmation --workers 12 --bootstrap 200 --shuffles 10
python -m research.fx.position_grid_report research/fx/results/position-factor-grid research/fx/results/position-capacity-grid research/fx/results/position-population-grid research/fx/results/position-grid-confirmation --output research/fx/results/position-grid-overview
python -m unittest discover -s research/fx/tests -v
```

[完整CSV](results/position-grid-overview/grid.csv)和[JSON](results/position-grid-overview/grid.json)含每组参数、因子实际标准差、成交量、仓位峰值及指标。每组原始序列和完整诊断保留在相应results目录；results被git忽略，配置和报告可重算。

实际验证：上述四组运行均正常退出；逐条核对配置集合、全长数组、正价、到达与成交计数、Decimal净持仓边界、外币池与账户及费用账本、指标样本数、独立重算的样本标准差；174条均通过。重复配置逐数组相同。已有14项unittest通过，报告脚本compileall通过。未进行真实市场数据校准、六小时tick映射、央行冲击网格或真人交易对照。
