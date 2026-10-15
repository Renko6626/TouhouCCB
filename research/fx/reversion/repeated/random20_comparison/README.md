# 本轮实验归档

本目录保存 2026-10-16 新跑的 12 条离线路径：10 随机＋5 库存＋5 短线，对照 20 个纯随机。结果与限制见 [report.md](report.md)，独立审阅见 [review.md](review.md)。

- `comparison.csv`、`metrics.json`、`summary.json`：逐种子指标和等权汇总。
- `traces.tar.gz`：12 个路径目录的逐 tick 数据、钱包、成交和轮次记录。`archive.json` 记录归档及每个原文件的 SHA256，打包后逐文件核对与实验产物完全一致。
- `manifest.json`、`validation.json`、`model.diff`：执行前设置、输入哈希、核对结果及相对旧模型的改动。
- `finalize.py`：仅从本目录已保存的数据重建报告和图，不重新模拟。

## 完整重跑的依赖边界

`run.py` 和 `model.py` 复用了此前本地研究中的 `research.fx.common`、`activity`、`episodes`、`factor_activity`、`reversion.inventory`、`timing_metrics`，并读取父目录的 `FROZEN_CONFIG.json` 和旧 `profit_trail04` trace 作一致性核对。这些旧研究输入在本次提交前未被版本控制，本次未将整个既有研究目录纳入提交。因此从干净克隆完整重跑还需提供原研究基线；相关原文件哈希已记入 manifest。

仅查看、核对保存数据或执行 `python -m research.fx.reversion.repeated.random20_comparison.finalize` 不需要旧模拟器和旧 trace，需现有 Python、NumPy、Matplotlib 环境。归档不接入生产应用或部署启动过程。

需要读取逐 tick 数据时，在本目录执行 `tar -xzf traces.tar.gz`；原始路径目录会恢复到本目录中。主工作区的原始实验文件没有移动或删除。
