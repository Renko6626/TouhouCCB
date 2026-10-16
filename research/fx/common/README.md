# 公共研究基础设施

只提供现有研究中已需要的数值与成交逻辑，模型状态和策略留在各自分支。生产市场不受此目录影响。

| 模块 | 接口与职责 |
|---|---|
| `amm.py` | `Account/Order/Pool`，内存钱包和真实生产恒定乘积报价；原包装器字节保留 |
| `execution.py` | `execute(pool, account, wanted, max_fraction)`，有符号外币意向，按即时储备、余额及6位精度裁剪；返回真实quote或不可成交的None |
| `clock.py` | `market_streams(seed, accounts)`与`draw_events(arrivals, rate_per_account, execution_rng)`，整数tick、独立账户Poisson到达及随机执行顺序；标记用对应账户mark流的`normal()` |
| `diagnostics.py` | 原样保留`acf/describe`、尾部及R/S研究函数；不自动运行尾部检验 |
| `scaling.py` | `DFA_SIZES/dfa/fits`，直接提取原DFA数值函数，移除旧实验CLI和绘图依赖 |
| `analysis.py` | `return_diagnostics(returns)`，原收益/绝对/平方收益ACF、分布、聚合与DFA；`state_diagnostics(state, returns)`，对已对齐状态的ACF与同/下一块关系 |
| `series.py` | `block_mean/correlation`，常数相关返回None |
| `reporting.py` | `table`，已有结果转Markdown，不混入模型或额外计算 |

调用方负责预热：例如`returns=np.diff(np.log(prices))[5000:]`恰好对应tick5001…100000。`state`必须为这些收益发生前已知、逐点对齐的状态，不由公共层猜测时点。固定状态的ACF仍为NaN、分块相关为None，不能虚构记忆。

报价使用金币/外币，`wanted>0`买外币，`wanted<0`卖；成交受真实钱包约束。Pool保留原默认费用，实验必须从各自配置显式传入，本次活动分支配置为0。

随机流沿用`seed*1000003+i*101+1/+2`分别到达/标记，执行顺序为`seed*1000003+4`。固定率的逐tick抽样需保持原20N序列；动态率时组间次数可以不同，不要求逐点配对。这里没有参与率公式、强度上限、补钱、目标库存、M/R或活动反馈状态。

兼容验证见[验证记录](compatibility.json)。旧分支及全部模型专属测试见[归档](../archive/mr-2026-10-15/ARCHIVE.md)。
