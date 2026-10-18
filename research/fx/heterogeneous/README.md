# 随机个人三因子与活动反馈

每户开局随机趋势、回归和噪声参数，之后固定；保持原类型的平均权重及平均噪声方差，沿用共享价格活动反馈。两档差异程度、三批人口与三个行情种子交叉，包含反馈开关、原两类和v1参照，共45条10万tick路径。

本轮两档异质性仅小幅改变方向相关，没有恢复v1的中尺度波动持续性；活动强度仍接近底值.75。三因子组裁剪为0，成交尺度分段稳定。结果只针对当前参数中心和分布。

- [报告及图表](results/experiment/report.md)
- [实验方案与执行记录](PLAN.md)
- [个人参数](results/experiment/profiles.csv)
- [逐路径指标](results/experiment/metrics.csv)
- [独立数值核对](results/experiment/audit.json)

```bash
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m research.fx.heterogeneous.run
OPENBLAS_NUM_THREADS=1 MPLCONFIGDIR=/tmp/fx-matplotlib python -m research.fx.heterogeneous.report
```

复用既有公共设施、信号/执行规则与分析，旧源、归档及冻结v1未修改。本轮没有随机窗口、个体活动状态、学习、外部注资或玩家交易。
