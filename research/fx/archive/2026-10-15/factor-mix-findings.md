# 趋势主导与回归主导账户对照

2026-10-14。用户批准不同账户使用不同因子权重。本轮只改变a、b的账户分布，窗口统一30tick，避免与上一轮窗口异质性混合。账户权重在开始时固定，不模仿、不切换策略。

## 原论文中的组合

[Chiarella、Iori、Perelló，公式1～4](https://arxiv.org/pdf/0711.3581)逐人抽取趋势、基本价值回归与噪声权重，预期先归一化再乘预测期限；期限与风险厌恶也和权重有关。它是连续的个体差异，不仅是若干纯策略群体。我们采用两种固定a/b作为可识别的简化实验，回归参考仍是历史均价，未移植基本价值、风险方差需求或权重归一化，因此不称为完整复现。

## 设计与结果

四组各seed41/42/43、每条10万tick、预热5000，共120万tick。均为20人、Q250外币、alpha0.2、rate0.05、sigma0.001、灵敏度100，初始池100万本币/20万外币、费用0.05%。总容量5000外币、总到达1/tick相同，同seed四组到达数组逐tick相同。尾部筛查bootstrap=0，60tick块打乱10次。

| 组 | a/b及人数 | 价格区间均值 | 收益ACF(1) | 归一化绝对收益ACF(1) | q20大块R/S斜率 |
| --- | --- | ---: | ---: | ---: | ---: |
| 全员趋势主导 | 30/0.1，20人 | 5.333% | 0.473 | 0.153 | 0.589 |
| 全员回归主导 | 3/1，20人 | 0.291% | -0.038 | 0.0033 | 0.494 |
| 全员平均系数 | 16.5/0.55，20人 | 0.385% | -0.0007 | 0.0021 | 0.493 |
| 混合两种人 | 30/0.1与3/1各10人 | 0.385% | -0.0009 | 0.0043 | 0.501 |

价格区间max/min-1覆盖整条运行，其余指标取95000个预热后收益。趋势组出现强周期；混合组弱相关，没有跨种子重复的明显波动聚集或长记忆线索。混合组R/S三种子分别约0.464/0.497/0.543，不能凭一个偏高值宣称复现。

关键是混合组和平均系数组非常相近。小信号下tanh近似线性、每人Q和反应参数相同，群体响应可以近似由平均系数决定。账户分组仍存在不同头寸和异步成交，因此不是严格等价；但本轮没有观察到强烈的异质性新增效应。

## “主导”是否真实成立

混合组seed41的实际因子标准差：

| 账户组 | 趋势 | 回归 | 噪声 |
| --- | ---: | ---: | ---: |
| a30/b0.1 | 0.000324 | 0.0000183 | 0.001000 |
| a3/b1 | 0.0000324 | 0.000183 | 0.001000 |

其他两个种子类似。趋势/回归在确定性信号中确实分别主导，但噪声的实际量级仍大于两组各自的确定性信号。不能将标签解读为“压倒所有三因子的主导”。分组标准差保存在每条result.json的group_factors中。

本轮表明，静态不同权重可以削弱共同趋势震荡，但没有自动形成持续变化的群体活跃程度或风险状态。它并不排除其他配比、权重分布或非线性区域的异质性效应。本轮未做配比网格、噪声进一步调节、窗口混合或风险适应，未追加长序列确认。

## 图、数据和验证

![四组行情与相关曲线](results/position-factor-mix/overview/factors.png)

图固定取seed41的5000～9000tick，各面板使用各自纵轴尺度。收益及绝对归一化收益ACF使用完整预热后数据，排除lag0。

[配置](configs/position-factor-mix.json)、[逐组CSV](results/position-factor-mix/overview/comparison.csv)、[三种子汇总](results/position-factor-mix/overview/summary.json)。factor_mix按编号交错分配等人数的(a,b)参数，空值保持同质配置及旧ID；只扩展研究配置和观测，PositionTrader与生产AMM没有改算法。

```bash
python -m research.fx.experiments --config research/fx/configs/position-factor-mix.json --output research/fx/results/position-factor-mix --workers 8 --bootstrap 0 --shuffles 10
python -m research.fx.window_mix_report --source research/fx/results/position-factor-mix
python -m unittest discover -s research/fx/tests -q
```

已有14项测试通过；12条模拟正常完成、零拒单、净持仓均未超过250外币。检查配置集合、20人共同窗口及实际分组、同seed到达数组、100001正且有限价格点、外币账本与费用、独立重算收益标准差；分组确定性信号主导关系也逐种子核对。图已查看，脚本compileall及diff检查通过。没有提供尾部拟合优度或现实市场校准结论。

## 追加：统一将噪声从0.001降到0.0005

用户要求迅速压低噪声。本轮四组、seed41/42/43、10万tick及分析设置保持相同，只改变sigma，另完成12条运行、120万tick。

| 组 | 原价格区间→新价格区间 | 原绝对收益ACF(1)→新值 | 原q20大块斜率→新值 |
| --- | --- | --- | --- |
| 全员趋势主导 | 5.333%→5.402% | 0.1533→0.1578 | 0.5887→0.5810 |
| 全员回归主导 | 0.291%→0.146% | 0.00330→0.00335 | 0.4936→0.4938 |
| 全员平均系数 | 0.385%→0.194% | 0.00209→0.00212 | 0.4934→0.4936 |
| 混合两种人 | 0.385%→0.195% | 0.00426→0.00439 | 0.5012→0.5017 |

均为三个种子均值。趋势主导组维持强周期，内生趋势信号标准差0.00795→0.00801，输入噪声减半没有明显压低价格区间。其他三组的绝对波动大约减半，标准化相关与R/S几乎不变，符合弱反馈、近线性区域的幅度缩放解释；降低噪声没有让聚集或长期记忆变得明显。它也没有破坏混合与平均系数组的相似性。

[低噪声配置](configs/position-factor-mix-low-noise.json)、[逐组数据](results/position-factor-mix-low-noise/overview/comparison.csv)、[汇总](results/position-factor-mix-low-noise/overview/summary.json)、[新图](results/position-factor-mix-low-noise/overview/factors.png)。

```bash
python -m research.fx.experiments --config research/fx/configs/position-factor-mix-low-noise.json --output research/fx/results/position-factor-mix-low-noise --workers 8 --bootstrap 0 --shuffles 10
python -m research.fx.window_mix_report --source research/fx/results/position-factor-mix-low-noise
```

本轮没有改Python算法；12条全部完成，零拒单、净持仓未超250，核对配置只差sigma、与原序列的到达数组完全相同、价格长度及有限正价、外币含费用账本和独立重算的收益标准差。PNG已查看，文档diff检查通过。尾部仍为描述性筛查，没有新增拟合优度结论。

## 追加：低噪声混合组将每人Q从250提高到500

沿用上述sigma0.0005、两种人各10个、窗口30、alpha0.2、rate0.05、seed41/42/43，各跑10万tick。只提高每人净持仓参数Q，AMM池100万本币/20万外币不变。总同向容量由5000提高到10000外币，占初始外币池5%。

| 指标（三种子均值） | Q250 | Q500 |
| --- | ---: | ---: |
| 完整价格区间 | 0.195% | 0.980% |
| 单tick收益标准差 | 0.00276% | 0.00685% |
| 收益ACF(1) | -0.00075 | 0.03881 |
| 归一化绝对收益ACF(1) | 0.00439 | 0.06013 |
| 归一化绝对收益ACF(100～1000)均值 | -0.000080 | 0.001826 |
| q20大块R/S斜率 | 0.5017 | 0.6375 |
| 归一化超额峰度 | 3.378 | 7.146 |

三个种子的绝对收益一阶相关为0.0693/0.0557/0.0554，R/S为0.5898/0.6524/0.6702。seed41的斜率仅略高于10次打乱上界0.5895，另外两条高于各自上界0.5725/0.5398。这是较有希望的有限尺度波动持续性候选，尚未验证长记忆或稳定幂律。价格谱仍有256～273tick宽峰，三格功率约0.82%～0.92%；应继续排除短期结构、周期和池深漂移，不能只看平均指标。

每人实际净持仓峰值约108.5～119.2外币，远未触及500。因为q_target=Q*tanh(100gap)，Q同时控制上限和目标仓位的尺度；近零灵敏度为100Q。扩大Q即使没有触及旧上限也会增强反馈。因此不能解释成“原来250上限卡住了仓位”，也不能解释成AMM池深变化。

[Q500配置](configs/position-factor-mix-cap500.json)、[逐组CSV](results/position-factor-mix-cap500/overview/grid.csv)、[完整摘要](results/position-factor-mix-cap500/overview/grid.json)、[seed41价格图](results/position-factor-mix-cap500/overview/seed41/trajectory.png)、[seed41统计图](results/position-factor-mix-cap500/overview/seed41/diagnostics.png)。图固定取最小种子，统计用95000个预热后收益；同时修正通用绘图脚本写死的295000样本标题和30万tick坐标，使不同长度的运行按真实数据标注。

```bash
python -m research.fx.experiments --config research/fx/configs/position-factor-mix-cap500.json --output research/fx/results/position-factor-mix-cap500 --workers 3 --bootstrap 0 --shuffles 10
python -m research.fx.position_grid_report research/fx/results/position-factor-mix-cap500 --output research/fx/results/position-factor-mix-cap500/overview
python -m research.fx.inspect_position_case --source research/fx/results/position-factor-mix-cap500/9ba1dfd4f0cac047 --output research/fx/results/position-factor-mix-cap500/overview/seed41
```

3条正常完成、零拒单、净持仓未超500。核对配置只差Q、原新到达序列相同、全长100001有限正价、外币账本含费用及独立重算的样本标准差；PNG已查看，绘图脚本compileall通过。未运行长序列确认或尾部自举。
