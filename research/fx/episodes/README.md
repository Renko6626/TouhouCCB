# 一次建仓、持有后退出

独立离线探索，沿用真实AMM与有限双币钱包。个人三因子仅用于入场，之后按真实开仓成本止盈、止损或移动退出。

- [6/24小时局长参数探索](session/results/report.md)
- [更短线的低阈值与查看频率探索](rapid/results/report.md)
- [实验报告与走势图](results/exploration/report.md)
- [历史方案](../BEHAVIOR_PROPOSAL.md)、[实验参数](config.json)
- 重现：`python -m research.fx.episodes.run` → `python -m research.fx.episodes.audit` → `python -m research.fx.episodes.report`。

全行为组常态下停止成交；15随机＋5行为组能持续完成交易轮。注资后有止盈卖出和止损回补，没有可靠的旧价恢复。仅为固定人口、三个行情种子的探索结果。
