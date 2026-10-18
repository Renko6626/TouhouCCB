# 半小时波动与冲击回吐

独立离线实验，比较15随机＋5短线和10随机＋5慢参考库存调节＋5短线。短线每轮200外币；库存调节目标另设±500，初始钱包仍为5000金圆券/1000外币。200解释为单轮建仓规模，不是20户统一的总持仓硬上限。

- [10000tick连续冲击及20人钱包跟踪](repeated/results/report.md)
- [完整报告与走势图](results/exploration/report.md)
- [参数](config.json)、[历史目标](../MARKET_TARGETS.md)
- 重现：`python -m research.fx.reversion.run` → `python -m research.fx.reversion.audit` → `python -m research.fx.reversion.report`。

有短期回吐，也允许较晚价格保留偏移；本轮未同时稳定实现30分钟约2%与双向冲击10分钟约半幅目标。结果保留，未替换冻结基线。
