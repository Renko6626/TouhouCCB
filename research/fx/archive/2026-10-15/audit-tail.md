# 尾部诊断独立审查

审查范围：`diagnostics.py` 的 `pareto_fit`、`pareto_bootstrap`、`tail_alternatives`，以及 `study.py` 的调用和已有长期实验数据。2026-10-14，只读算法审查；没有修改模拟器、诊断代码或永久测试，没有重新运行长期模拟。

## 结论

连续 Pareto 的指数 MLE、KS 距离以及重新拟合阈值的半参数 bootstrap 主流程正确。当前最小 p 值支持的范围是：在预设阈值网格及连续 iid 假设下，观察到的单步绝对收益与精确 Pareto 尾部不相容。它不能单独判定策略未产生厚尾、不能认定模型整体不符合真实市场，也不能排除更高阈值上的渐近幂律。

实际发现一项需要修正或明确限制的数值问题：截断对数正态优化使用任意参数边界，已有样本触界，部分 AIC 排名会受该边界影响。其他主要问题属于方法适用范围和报告解释，不能靠修改正确公式消除。

## 文献对照

[Clauset、Shalizi、Newman](https://arxiv.org/html/0706.1062) 的连续密度指数为 α，CCDF 指数为 α−1；式(9)对应当前 β 的 MLE。第4.1节要求每份合成样本重新拟合参数和阈值，采用半参数主体重采样；其 p≤0.1 是经验判定规则，较大 p 仍不能确立幂律。本文第2节区分连续与整数幂律，第3.3节强调阈值选择的重要性。

[Chiarella、Iori、Perelló](https://arxiv.org/html/0711.3581) 第III节使用绝对收益 DDF，并分别报告左右尾 Hill 指数，以100份不同 seed 重复估计误差。Appendix B 式(20)的 Hill 指数对应生存函数指数；其文字将 F 称作 pdf 存在记号不一致，不能据此给现有 β 多加或少减1。现项目不是该订单簿模型的复现，三 seed 的不同结果也不是原论文的误差估计。

## 逐函数判断

### pareto_fit：公式正确，搜索范围有限

- `beta=n_tail/sum(log(x/xmin))` 是连续 Pareto 的 CCDF 指数；PDF 指数为 β+1。
- KS 同时比较 ECDF 跳跃前后，计算正确；有重复值时也包含跳跃两端。它比较的理论 CDF 是连续函数。
- 排除非正数后拟合，目标明确是正绝对收益的条件尾分布，不是全体收益分布。零收益比例必须由其他指标保留报告。
- 十个候选阈值是有限网格近似，不能称对所有阈值完整执行 CSN。bootstrap 使用同一网格，避免了直接把拟合后的 KS 当作固定参数 KS 检验，但不增加网格外尾部的信息。
- `min_tail=500` 是研究者的样本保留规则，不是文献给出的成立条件。抽稀时100个尾点尤其难区分近似 Pareto 与其他光滑尾部。
- 现有指数的条件 iid 标准误量级为 β/√n_tail；这没有计入阈值选择、相关性和非平稳性，不能直接给当前序列当正式置信区间。
- `tail_decades` 描述跨度，不能设“少于一个数量级即不符合”的硬门槛。例如 β=3、n_tail≈500 的真 Pareto，典型最大值本来仅为 xmin 的约500^(1/3)倍，即不足一个数量级。跨度应结合样本量、阈值稳定性、拟合误差解释。

### pareto_bootstrap：实现正确，iid 校准不适合直接裁决当前序列

- 二项式尾部计数、经验主体重采样、Pareto 尾部生成等价于逐点混合采样；随后重新估计 β、xmin 和 KS，主流程正确。
- 加一 p 修正避免输出0；200次全部未超越只说明 p 分辨率为1/201、Monte Carlo 概率95%上界约0.0183。现报告对此解释正确。
- 该区间仅描述模拟次数误差，不包含金融时间序列依赖、阈值网格和模型假设的不确定性。
- `[::60]` 抽取的是每60 tick的一条单步收益，不是60 tick聚合收益。它可能降低短程依赖，但不会平滑订单计数的阶梯结构，也不会消除随池深变化的非平稳尺度。
- 当前 AMM 固定名义订单在小冲击近似下形成离散净买卖计数冲击；池深和舍入又使阶梯位置变化，不能直接套整数 zeta-Pareto。因此不建议未经论证把连续检验替换为整数幂律检验。
- 观察到阶梯结构时，拒绝精确连续 Pareto 是检验结果本身，不是计算错误；若目标是裁决潜在连续尾部，就需要明确的观测生成模型。为此新增机制超出局部修正，应另行确认。

### tail_alternatives：似然定义正确，边界限制影响排名

- Pareto、从 xmin 开始的条件指数分布、左截断对数正态的归一化似然公式均正确，且在同一尾样本上比较。
- AIC 对应1、1、2个条件形状参数；这是固定所选 xmin 下的条件排名。没有处理阈值选择的不确定性，也没有显著性检验。
- 对数正态参数以 `y=log(x/xmin)` 表示，设 μ→−∞、σ²≈−μ/β 时，其条件尾部趋近 Pareto。因此硬限制 μ≥−50 会把实际似然上确界挡在外面。
- 重新拟合已有 noise seed3 得 μ=−50，现 ΔAIC(lognormal−Pareto)=7.923；其中超过约2的参数罚项的部分来自受限模型拟合误差。expectation seed3 也触界，ΔAIC=3.983。
- expectation seed1 原始单起点解 μ≈−44.562，ΔAIC=4.166；独立起点可得到 μ=−50，ΔAIC=3.928，表明单次 `success=True` 不保证找到同一界限内的最佳值。
- 应至少报告参数、触界情况和优化是否可靠，优化失败或不可靠时停止给出确定的 `favored`。若继续进行正式模型比较，应使用稳定的参数化并解释边界极限；不能仅扩大任意 μ 范围就宣称问题解决。
- AIC最低只意味着候选集合内的相对拟合排名，不能证明该分布正确，更不能把较低 AIC 当作统计显著的排除证据。现 findings 已有这层提醒，可保留。

## 实际校验及命令证据

使用当前 NumPy/SciPy 和真实生产诊断函数，没有安装依赖。主要合成控制命令：

```bash
python - <<'PY'
import numpy as np
from research.fx.diagnostics import pareto_fit, pareto_bootstrap
ps = []
for seed in range(20):
    x = 1 + np.random.default_rng(seed).pareto(3, 10000)
    fit = pareto_fit(x)
    p = pareto_bootstrap(x, fit, 99, seed + 200)['p_iid']
    ps.append(p)
print(ps, sum(p <= .1 for p in ps))
x = 1 + np.random.default_rng(1).pareto(3, 20000)
for label, sample in [('integer_rounded', np.round(x)),
                      ('repeat20', np.repeat(x[:1000], 20))]:
    fit = pareto_fit(sample)
    print(label, fit, pareto_bootstrap(sample, fit, 199, 30))
PY
```

结果：20份 β=3 的独立连续 Pareto 中1份 p≤0.1，未见公式导致系统性误拒绝的证据；此样本量不是完整的校准研究。seed0 的 β=2.988、p=0.81；seed1 的 β=3.022、p=0.25。整数舍入后的 Pareto β被估为5.607、KS=0.6415、p=0.005；每个独立值重复20次的相关 Pareto，β=2.931、KS=0.04547、p=0.005。后两项直接说明离散观测机制或依赖可以触发对连续 iid 零假设的拒绝，即使底层来自 Pareto。

另运行相同函数检查 light-tail 反例：`rng=np.random.default_rng(243)`，依次生成10000个 `1+rng.pareto(3)`、`rng.exponential(1)`、`abs(rng.normal())`、舍入到一位小数的 Pareto；各自用 `pareto_bootstrap(...,repetitions=199,seed=29)`。指数和半高斯均 p=0.005，AIC分别偏向指数和截断对数正态。该批真 Pareto p=0.045，属于有限样本允许发生的拒绝，不能用单份正例要求 p 必须高于0.1；也不能通过挑种子掩盖它。

读取现有数据的命令：

```bash
python - <<'PY'
import json, numpy as np
from pathlib import Path
p = Path('research/fx/results/long-run-200')
d = json.loads((p/'diagnostics.json').read_text())
for kind, rows in d.items():
    for seed, result in rows.items():
        prices = np.load(p/f'{kind}-{seed}.npz')['prices']
        x = np.abs(np.diff(np.log(prices))[2000:])
        print(kind, seed, len(x), sum(x > 0), result['pareto'],
              result['thinned_pareto'])
PY
```

六条序列均为198000收益点，约125000个正绝对收益；原始尾部 KS=0.0921～0.3192，抽稀尾部 KS=0.1369～0.3519，200次均零超越，原报告表格与数据相符。

优化边界检查使用 `scipy.optimize.minimize` 重建原始负对数似然，固定原 bounds 与 L-BFGS-B，比较起点 `[y.mean(),log(y.std())]`、`[-10,0]`、`[-50,log(sqrt(50/beta))]`；结果见前述 AIC 数值。没有替换现有结果文件。

## 必要修正与后续范围

1. 优先把判定文案明确为“当前单步样本及候选阈值下，连续 iid Pareto 精确拟合被拒绝；该诊断受序列依赖和订单阶梯结构限制”。“尚无稳定幂律的充分证据”可保留；“无厚尾”“策略不符合市场”没有得到本检验支持。
2. AIC 输出补充优化参数、触界和可靠性；在数值问题处理前，将对数正态结果称为受限拟合排名，避免确定性排除替代模型。稳定重参数化属于算法修复，不要求重建模拟系统。
3. 保留 β定义、正样本分母、min_tail 和候选网格的明示说明。不要新增 β≈3、单数量级跨度或 p>0.1 的整体“符合/不符合”二元门槛。
4. 若用户需要进一步判断厚尾，先确定具体问题：相对高斯的尾概率增强、与纯噪声基线的差异、精确连续 Pareto、或渐近正则变化尾部。它们是不同命题。现超额峰度约1及聚合下降与复合泊松尖峰相容，不能独立建立数学意义的幂律或重尾。
5. 参数、订单规则、采样尺度、相关 bootstrap、左右尾校准、更密阈值扫描或现实数据对照均是新的实验选择，应说明成本后确认。本审查没有把这些扩展自动实施。

未验证：非 iid 条件下的正式显著性校准、不同订单金额/到达强度、现实市场数据、多于三 seed 的模拟重复以及更高阈值渐近性质。现有证据足以审查公式和限缩结论，不能完成整个市场模型的产品验收。

## 修复后只读复审

主 agent 随后修改了 `tail_alternatives`，本节审查当前代码；上文的 μ硬边界问题和数值记录描述修复前版本，不再代表当前代码。审查者仅追加本报告，没有修改代码或保存数据。

当前以 `y=log(x/xmin)` 上的 `exp(-a*y-b*y²)/Z` 拟合对数正态条件尾部，b≥0，允许a为负；b=0仅允许a>0，并显式比较精确Pareto边界。b>0下a=−μ/σ²、b=1/(2σ²)，与原模型等价。正a分支的erfcx公式、负a分支的log_ndtr公式和边界归一化均正确。取消任意μ硬边界，保留多起点，并报告边界/参数及描述性AIC限制，解决了本次有实际证据的数值问题。

直接重新调用真实函数，结果为：

| 保存样本 | 修复前 ΔAIC(lognormal−Pareto) | 修复后 ΔAIC | 修复后参数边界 |
| --- | ---: | ---: | --- |
| noise seed3 | 7.923208 | 2.000000 | b=0，a=13.306719 |
| expectation seed3 | 3.982967 | 2.000000 | b=0，a=9.914131 |
| expectation seed1 | 4.165866 | 2.000000 | b=0，a=9.368331 |

这三项的似然现在与Pareto相同，额外AIC差仅为保留的2参数与1参数罚项之差。其余noise seed1、noise seed2、expectation seed2均得到内点解且 `lognormal_converged=True`；新旧对数正态AIC分别相差约4.8e−8、2.6e−10、1.2e−6。六条原有 `favored` 排名未变。

合成内点控制：μ=1、σ=0.4的正截断正态y，x=exp(y)，固定xmin=1，5000点，seed127。恢复μ=0.996790647、σ=0.393043603，`lognormal_pareto_boundary=False`，AIC=14625.892638054。用原始μ/logσ似然独立BFGS拟合，AIC=14625.892638053，差1.39e−9，说明自然参数化没有牺牲有效内点拟合。

实际命令：

```bash
python - <<'PY'
import json, numpy as np
from scipy import stats, optimize
from pathlib import Path
from research.fx.diagnostics import tail_alternatives
p = Path('research/fx/results/long-run-200')
d = json.loads((p/'diagnostics.json').read_text())
for kind, rows in d.items():
    for seed, result in rows.items():
        x = np.abs(np.diff(np.log(np.load(p/f'{kind}-{seed}.npz')['prices']))[2000:])
        print(kind, seed, tail_alternatives(x, result['pareto']))
y = stats.truncnorm.rvs(-2.5, np.inf, loc=1, scale=.4,
                       size=5000, random_state=np.random.default_rng(127))
x = np.exp(y)
out = tail_alternatives(x, {'xmin': 1., 'beta': len(x)/sum(y)})
print('known lognormal', out)
def original_nll(par):
    mu, logs = par
    sigma = np.exp(logs)
    return -np.sum(stats.norm.logpdf((y-mu)/sigma)-logs-y-stats.norm.logsf(-mu/sigma))
f = optimize.minimize(original_nll, [y.mean(), np.log(y.std())], method='BFGS')
print('independent interior AIC', 4+2*f.fun)
PY
```

复审未发现阻断本次修复的数值错误。AIC仍只适用于当前阈值的描述性比较；Pareto位于非正则边界，不能以普通2参数规则据此计算显著性或声称排除任一分布。当前所有实际检查优化成功；若将来返回 `lognormal_converged=False`，下游应忽略确定排名并提示拟合未确认，无需为本次未出现的情况扩展算法。此前离散性、序列相关、有限样本和阈值搜索范围的限制仍然成立。
