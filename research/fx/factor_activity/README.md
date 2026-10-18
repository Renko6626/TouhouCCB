# 三因子与活动反馈比较

恢复归档三因子的价格判断和目标仓位规则，比较三因子固定率、冻结活动反馈v1、三因子加活动反馈。20个有限钱包、零手续费、相同AMM；每组三个种子、每条10万tick，预热5000，未调参。

当前固定组合没有保留v1的中尺度波动持续性，且短期收益方向相关更强。组合成交规模更小，平均活动强度接近底值；未单独识别各项原因，不能推论所有三因子模型无效。

- [完整报告与图表](results/comparison/report.md)
- [162组参数网格及独立种子验证](results/grid/report.md)：部分参数全程波动相关更强，但方向相关或运行退化尚未解决。
- [固定配置](config.json)
- [实验配置](config.json)
- [种子级指标](results/comparison/metrics.csv)
- [验证证据](results/comparison/validation.json)

运行：

```bash
OPENBLAS_NUM_THREADS=1 python -m research.fx.factor_activity.run
OPENBLAS_NUM_THREADS=1 MPLCONFIGDIR=/tmp/fx-matplotlib python -m research.fx.factor_activity.report
OPENBLAS_NUM_THREADS=1 python -m unittest discover -s research/fx/tests -v
```

本分支独立于冻结v1；未修改archive、common或生产市场。当前数据没有央行注资或玩家订单。
