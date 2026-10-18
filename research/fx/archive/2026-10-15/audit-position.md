# Bounded target-position 与账本独立核查

2026-10-14。只读核查 `position.py`、`experiments.py` 及真实AMM报价；未修改生产代码，未新增永久测试，未安装依赖，未执行长网格。本报告对应核查时的实现，后续统计标签修正由主agent处理。

## 仓位与批量成交边界

`position = account.foreign - initial_foreign` 是相对初始外币余额的变化。初始 `2e11` 外币用于承接交易，不是策略允许新增的仓位，也不是盈利。目标 `cap × tanh(signal)` 在±cap内。一次到达规划的变化为 `adjustment × (target-planned)`，0<adjustment≤1，使planned沿当前仓位到目标之间移动，向下量化又减少调整量。

同一bot的一批到达只有一个固定目标和方向，Simulation连续执行该bot返回的全部订单，中间没有其他bot插入。卖出实际扣账户的外币恰为规划quantity，包括外币手续费，因此实际仓位与planned同步；买入以向下量化的 `quantity × batch起始price` 支付本币，手续费和滑点使外币成交量不超过quantity，后续同方向买单抬高池价，仍不会超过规划上界。下一tick重新读取实际余额，不沿用计划仓位。

因此，初始delta在边界内时，每笔实际成交后仍在±cap内。每bot每个batch的外币累计成交量≤目标与batch起始实际仓位之差≤2cap；每笔外币量≤2×adjustment×cap。N个bot每tick的外币gross≤2Ncap。这些界针对当前连续执行顺序和外币量，不代表固定本币额度；本币成交量取决于池价。

整个运行累计gross不受同一个cap常数约束。目标来回切换可反复换手，同时净仓始终有界。手续费在输入币种从池资产中分离，外币守恒关系是：

`pool_foreign = initial_pool_foreign - sum(bot foreign_delta) - accumulated_foreign_fees`

所以即使净仓有界，长期换手收费仍可耗掉池外币储备；不能据bounded position声称所有期限下池深和价格也有固定边界。真实账户余额约2e11/1e12主要反映预置backing，解读交易结果应使用change、净仓、gross及费用。

## 实际验证

运行已有测试：

```bash
python -m unittest research.fx.tests.test_position -v
```

2个行为测试通过，覆盖持续信号停止增加仓位，以及反复往返gross增长而仓位有界。

临时控制命令：

```bash
PYTHONPATH=. python /tmp/fx-position-audit.py
```

真实AMM，pool初始本币50000/外币10000，cap5000，手续费5%，Poisson rate40，90tick，每3tick反转目标，seed127。每笔成交后断言仓位界、每batch断言外币gross≤10000。结果：

| adjustment | 最大绝对仓位delta | 最大batch外币gross | 运行外币gross | 到达数 | 最大batch订单数 | 外币费累计 | 最终池外币 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.2 | 4999.999992 | 9676.044725 | 266706.207175 | 3561 | 59 | 6597.429460 | 593.514455 |
| 1 | 5000.000000 | 9677.067593 | 266732.276716 | 3561 | 1 | 6598.077925 | 592.762081 |

这里使用较高手续费是为短时间展示费用与净仓的区别，不是推荐参数。验证未发现多Poisson到达导致实际仓位越界。

## 账本replay与拒单快照

逐record重放的方向正确：buy账户扣完整本币input、增加外币output，pool增加扣费后的本币；sell账户减少完整外币input、增加本币output，pool只增加扣费后的外币。gross按实际账户外币输入/输出绝对值累加，net按带符号变化累加，两者没有混淆。个体峰值在每笔成交后更新，包含batch内部峰值，不只查看tick终点。

拒单record不改变账户；第一次拒单前快照在重放到该record时取得，因此不是运行结束账户的冒充快照。账户附带的min/max change也只到该时刻。后续运行记录不影响已保存字符串快照。

临时独立重放一个真实fixed策略小样本：20tick，1bot，rate10，order_size=8e11，seed1，trend/reversion=0，noise_sigma=0.1；从捕获的实际Simulation.records独立逐笔累计，并断言最终账户、gross、net、外币守恒和首拒单快照。结果117笔成交、70笔拒单，gross外币 `219266860381.331080`，净外币delta `-54630424.099830`。首拒单发生tick1，价格 `3196805597201.399300349825087`，拒单前账户为本币 `200000000000.0`、外币 `200000199999.749875`；最终账户为本币 `992509745594.114115`、外币 `199945369575.900170`。各独立断言通过。这也说明巨大的gross不能解释为新增同样巨大的净资产。

## 需要修正的统计说明

核查时发现并已通知主agent两处标签问题，未修改其负责文件：

- position策略不按order_size生成订单，因此 `N×rate×order_size` 不应继续报告为该策略的理论名义成交额；应标为固定订单策略量，或在position策略设为空。
- `factors['arrivals']` 当时用 `len(orders)` 加权，position在饱和/量化后可有到达但无订单。这是提交订单加权，不是全部到达加权。应更名或用 `total_arrivals` 的增量，明确统计范围。

未发现账本方向错误、首拒单快照时序错误或position多到达越界。未验证所有参数、长期价格行为、无拒单保证或金融市场拟合；这些不由本次局部核查得出。

## 标签修正与18组产物结束复查

只读确认后续代码已将position策略 `expected_nominal_gold_per_tick` 设为None，另报单次外币量上界 `2×position_cap×adjustment`；因子加权范围改为 `submitted_orders`，固定订单策略仍为 `arrivals`。这两处标签问题已解决，未改变既有因子或交易数值。

读取 `results/position-comparison/summary.json` 并逐项对照18个 `result.json`，内容完全同步。12个position结果均complete、拒单数0、最大绝对仓位delta≤各自cap；position标签、单次上界字段均正确，并保留 `reporting_corrections` 说明可变仓位订单没有固定理论名义成交额、因子权重为提交订单。6个fixed结果中3个complete、3个invalid_model_regime；无效配置没有混入有效配置的通过统计。本复查确认已有记录及标签一致，不声称额外运行了18组模拟或对市场统计特征作整体验收。
