# AMM 与 DeFi 多主体模型：交易者动机、钱包和退出机制

2026-10-15。本轮是文献调研，未运行模拟、安装依赖或修改模型。先读了[研究方向](../DIRECTION.md)与[文献索引](README.md)。以下核读四项论文正文，另列一个有公开策略文档的执行框架作边界参考；只使用论文、作者公开稿、期刊及官方仓库/文档。没有把代码链接存在当成已复现。

## 对当前问题的结论

本轮最接近“记住买入价，持有后按风险/收益退出”的是 **TokenLab 2024**，但其价格由货币数量关系和流通供给变化计算，**没有通过恒乘积池逐笔成交**。最接近“恒乘积 AMM 中有跟随与反向用户”的是 **XRPL-AMM 2025**，但用户没有完整持仓生命周期，稳定性来自外部市场套利。**Angeris 等 2021**适合厘清这类稳定性结论的前提：有足够流动性的外部参考市场。

因此，优先细读顺序是 TokenLab §3.2–3.3、XRPL §IV.B、Angeris §4.1–4.3。它们分别提供生命周期规则、AMM 用户动作和外部锚边界，不能合并称为一个已验证的人类投资者模型。

| 项目 | 真正通过 AMM 成交 | 钱包与外部资金 | 入场—持有—退出 | 价格锚/回落来源 |
|---|---|---|---|---|
| TokenLab，2024 | 否；本文用 QTM 与供给控制 | 聚合交易量/供给输入；未给逐户封闭现金约束 | 每批投机买入记阈值，止盈/止损卖出 | 交易量、供给、服务用户持有期；非池内价格发现 |
| XRPL-AMM，2025 | 是，恒乘积现货池的模拟 swap | 正文未证明普通用户全程有限钱包；有外部套利交易 | 普通用户按概率交易；未给成本价退出 | GBM 或 Binance 外价，套利同步 |
| Angeris 等，2021 | 是；模拟链上 Uniswap 合约 | LP 有资产组合；套利可跨外部市场；不是封闭经济 | 随机需求交易者；LP 按历史收益/风险再平衡 | 随机参考市场且受交易影响，套利约束 |
| Hafner–Dietl，2024 稿 | 是，数值恒乘积现货池 | 回放历史订单；套利在竞争交易所买卖 | 普通交易量取历史数据，未生成成本价退出 | Binance 价/另设 GBM 价格 |
| Agent0，框架参考 | 是；Hyperdrive 合约交易 | 初始化账户余额，可另行加资；收益源在外部 | 随机持有期限、成熟退出等接口 | 固定/浮动利率与到期兑付；非两币现货 CPMM |

“未给出”表示在本轮读到的模型定义中没有得到证据，不推断成“软件一定没有”。四项论文均不是已用账户行为数据验证的完整人类生命周期模型。

## 1. TokenLab：可借退出规则，不能借本文价格机制

**准确条目：** Mengjue Wang、Stylianos Kampakis，*Modeling Speculative Trading Patterns in Token Markets: An Agent-Based Analysis with TokenLab*，2024，arXiv:2412.07512v1；[原文 HTML](https://arxiv.org/html/2412.07512v1)、[论文标识](https://arxiv.org/abs/2412.07512)、[作者指定代码](https://github.com/stelios12312312/TokenLab)。

**核读范围：** §2.4，式(1)–(2)；§3.1–3.3；§4、图2和表1；§5.4。本文式(2)为 `M × C = T × H`：供给、交易额、持有期进入价格关系。§3.2 的 controller 每期分配一部分交易量给投机买入，将这批代币暂时移出流通供给；持续检查此前各批交易的买入价阈值，达到止盈或止损价时卖出、增加流通供给。这里有入场成本记忆，没有外部 CEX 套利者，也没有 AMM 报价/滑点成交。[正文 §2.4–3.3](https://arxiv.org/html/2412.07512v1#S3)

§3.3 比较五组投机占比与止盈止损参数，使用 LINK 的真实供给、交易量、初始价格，服务用户参考持有期另行设定。没有给逐户现金、逐次扣款和资金耗尽规则，也没有单独持仓费用或有认知解释的入场信号。§4 按事后划定的上升、下跌与稳定阶段比较模拟价格偏差；**这不能识别现实中的五类人，也不证明五类人混合后会内生切换**，动态调整投机者比例仍列为未来工作。[正文 §3.3–5.4](https://arxiv.org/html/2412.07512v1#S4)

**复用判断（本笔记推断）：**值得借的是“每笔/每批建仓留下成本与退出条件”的小规则。论文的大幅阈值属于案例参数，不能当游戏推荐值。现有目标仓位规则若改为逐笔仓位、部分退出、重入与资金分配，须先决定最小需求；不能为了搬 controller 改用 QTM 或新增供给销毁。当前官方仓库 README 已有 scenario/YAML 等后来 API；本轮只核到代码入口，未确定 2024 论文对应 commit、controller 文件或结果复现状态。[官方仓库](https://github.com/stelios12312312/TokenLab)

## 2. XRPL-AMM：有模仿/反向行为，但“价格平衡者”是外部套利者

**准确条目：** Walter Hernandez Cruz、Firas Dahi、Yebo Feng、Jiahua Xu、Aanchal Malhotra、Paolo Tasca，*AMM-based DEX on the XRP Ledger*，2025 IEEE ICBC；DOI [10.1109/ICBC64466.2025.11114626](https://doi.org/10.1109/ICBC64466.2025.11114626)；[作者公开 PDF](https://yebof.github.io/assets/pdf/hernandez2025icbc.pdf)、[UCL 稿](https://discovery.ucl.ac.uk/id/eprint/10216028/1/XRPL_AMM.pdf)、[论文脚注4指定代码](https://github.com/dlt-science/xrpl-amm)。

**核读范围：** 作者 PDF 的 §III、§IV.A–C（尤其 §IV.B，PDF 第4页）、§V 图2–8与 §VII。模型比较 XRPL 协议级 AMM 与类似 Uniswap V2 的通用 AMM，实际订单改变恒乘积池储备。普通用户每轮以 80% 概率交易，20% 不动；60% 概率模仿前面动作，40% 反向，订单 0.01–2 ETH。它提供的是订单方向延续性，没有给持有期间的成本价、止盈止损或投资期限规则。[§IV.B](https://yebof.github.io/assets/pdf/hernandez2025icbc.pdf)

另一类 agent 比较池价与外部市场价，计算套利量，只有扣除交易/网络费后的收益超过安全利润门槛才下单，并在外部市场卖出。常规 swap 费为 0.3%；CAM 拍卖持有人有费率优惠。外价来自 GBM 或 Binance。正文没有证明普通用户全程受固定钱包约束；套利也显然不局限于池内两币存量。没有面向普通投资者的持续持仓成本。[§IV.B–C](https://yebof.github.io/assets/pdf/hernandez2025icbc.pdf)

作者评价价格同步、滑点、价格冲击、套利利润与 LP 收益；§VII 明列固定费用、简化拍卖和价格路径等限制。**这些价格偏差收敛不是无锚人群自己拉回旧价的证据**。可借普通用户的“做/不做、跟随/反向”对照，但当前研究更需要补持有与退出，不能把概率跟单直接叫完整人类模型。[§V、§VII](https://yebof.github.io/assets/pdf/hernandez2025icbc.pdf)

**代码状态：**官方仓库可见 `amms/`、`simulation/`、数据及 notebook；本轮未完整核读策略实现、逐户余额检查或对齐发表结果，没有安装或运行。若引入外价、跨市场套利、CAM、区块时序或手续费，就是新机制，需另提方案；本轮均不建议默认加入。[官方仓库](https://github.com/dlt-science/xrpl-amm)

## 3. Angeris 等：有真实合约成交的基础 ABM，稳定性有明确外部前提

**准确条目：** Guillermo Angeris、Hsien-Tang Kao、Rei Chiang、Charlie Noyes、Tarun Chitra，*An Analysis of Uniswap markets*，Cryptoeconomic Systems 1(1)，2021-04-05；首个预印本 2019，arXiv:1911.03380，2021 最新稿 v7。DOI [10.21428/58320208.c9738e64](https://doi.org/10.21428/58320208.c9738e64)；[正式全文](https://cryptoeconomicsystems.pubpub.org/pub/angeris-uniswap-analysis/release/15)、[arXiv](https://arxiv.org/abs/1911.03380)。**官方仿真代码：本轮未找到可核验的公开发布链接。**正文用 Gauntlet DSL，不能把其他 Uniswap 模拟仓库代充作者代码。

**核读范围：** §2.1 式(1)–(4)、§4.1–4.3、式(14)、图1–4。§4 通过 Python 绑定调用模拟链上的 Uniswap 合约，费率 0.3%。普通 trader 随机抽交易量，仅在成交价相对参考市场不差过给定比例时执行。套利者跨参考市场做带二次风险惩罚的利润最大化；初始 LP 持有至结束，理性 LP 则用历史收益和协方差做 Markowitz 非负组合再平衡。[§4](https://cryptoeconomicsystems.pubpub.org/pub/angeris-uniswap-analysis/release/15)

参考市场本身有交易冲击，且每步额外乘上随机收益项；它不是单纯固定基本价，但**始终是外部交易场所和价格参照**。LP 有组合预算，套利分析允许借入后跨市场回款；§4 还允许新 agent 进入，因此不能归为固定人数、封闭有限钱包。普通 trader 没有买入成本驱动的退出；二次惩罚是套利交易风险，不等于人类持仓成本。[§2.1、§4.1–4.2](https://cryptoeconomicsystems.pubpub.org/pub/angeris-uniswap-analysis/release/15)

验证针对无套利价格界与 LP 收益，图1–3的跟踪依赖参考市场及套利；未验证行为生命周期或玩家可理解性。值得保留它对“稳定”的限定：参考市场有充分流动性。对本项目最有用的是这条边界说明和“先看实际成交价再决定是否下单”的规则思路；引入参考市场或优化器须另外确认。[§4.3、结论](https://cryptoeconomicsystems.pubpub.org/pub/angeris-uniswap-analysis/release/15)

## 4. Hafner–Dietl：订单流来自历史回放，用于 LP 动态损益边界

**准确条目：** Matthias Hafner、Helmut Dietl，*Impermanent Loss Conditions: An Analysis of Decentralized Exchange Platforms*，arXiv:2401.07689v3，2024-02-09；[本轮读到的15页 PDF](https://arxiv.org/pdf/2401.07689v3)、[元数据](https://arxiv.org/abs/2401.07689)。另有 2026-08-10 Ledger 11:7–30 正式发表版，DOI [10.5195/ledger.2026.384](https://doi.org/10.5195/ledger.2026.384)；[期刊页](https://ledgerjournal.org/ojs/ledger/article/view/384)。**本节节号和公式号指2024稿，未把它当作2026版已逐页核对。官方仿真代码：本轮未找到。**

**核读范围：** §2 式(2)–(9)、§3.1–3.3、图3、式(10)–(13)、§4.1–4.3、图4–5。普通 trader 将历史 WETH/USDC 订单方向/数量交给恒乘积池；数据来自2021-05至2022-05。套利者每次价格更新和普通成交后检查机会，扣除费用和交易成本后选择最优数量，在竞争交易所买入、池内交换并在外部卖出。LP 在仿真期间不主动调仓。[2024稿 §3](https://arxiv.org/pdf/2401.07689v3)

初始储备、费用和订单参考 Uniswap V2，外价取 Binance，增长率实验换为 GBM。正文未给普通 trader 逐户现金/持仓或成本价退出；套利的外部买卖破坏封闭钱包前提。费用随交易累积并改变乘积，另调整套利交易成本；没有普通投资者的持有费或外部事件信念更新。[2024稿 §3.2–3.3](https://arxiv.org/pdf/2401.07689v3)

实验验证 LP 利润对交易量、池规模、外部价格趋势和套利成本的敏感性，并复现零费用下的标准无常损失形状；不识别人类行为，也不检验机器人经济体能否持续运营。它适合作为“回放订单＋外价套利”的边界对照，不能提供需要的 PvE 建仓动机。本项目零手续费，不能直接移用该文“套利费弥补 LP 损失”的收益结论。[2024稿 §4](https://arxiv.org/pdf/2401.07689v3)

## 5. Agent0：有持仓执行轮子，证据层级是框架而非人类模型

**条目：** DELV，*A Framework for Hyperdrive Trading Strategies / Agent0*；官方仓库版权标2024，本轮查看当前 README 及 v0.26.2 策略 API 文档；**没有为该软件虚配论文作者、DOI 或 arXiv**。[官方代码/说明](https://github.com/delvtech/agent0)、[官方文档](https://agent0.readthedocs.io/en/latest/)。

README 明确 Hyperdrive 面向固定/浮动收益，代币可到期兑付，提供 open/close long、short、LP 等合约交易。示例先给账户 base 与 ETH，再开仓、推进时间、平仓和查已关闭持仓；不是仅记录“意向成交”。但收益来自底层 yield source，且不是两币恒乘积现货池；long/short 名称也不能当作普通现货买卖。[官方 README](https://github.com/delvtech/agent0)

官方 `RandomHold` API 定义随机开仓、抽取持有时间、仅关闭已经到可关闭时间的仓位，并在成交后更新持仓账目；`SmartLong` 根据 fixed rate 与 variable rate 差值及风险阈值开仓。**核读限制：**本轮搜索工具返回了官方文档正文条目，直接打开同一 API URL及源码失败；没有核过实现中余额上限和每条退出分支。[RandomHold 文档](https://agent0.readthedocs.io/en/latest/autoapi/agent0/core/hyperdrive/policies/random_hold/index.html)、[SmartLong 文档](https://agent0.readthedocs.io/en/latest/autoapi/agent0/core/hyperdrive/policies/smart_long/index.html)

可借“策略提出交易、合约执行、结果更新持仓”的职责划分及按持有时间退出作为候选规则。此处没有人类数据验证或封闭经济统计结果，不能据此声称逼真；无需为本项目搬入 Foundry、Docker、数据库、利率协议或杠杆系统。

## 对注资回落与下一轮阅读的约束

以下是结合当前研究设置的推断，不是上述作者已证明的结论：

1. 外价套利模型产生回落，是因为池外仍能按另一价格买卖。当前直接向本币储备永久加10万改变储备和乘积，又没有外部锚；这些论文不能推出必然回旧价。
2. 成本价止盈可能在涨后产生反向卖单，但既有持仓数量有限，卖完即停止；止损还可能强化跌势。TokenLab 的退出逻辑值得研究，但不保证任意注资幅度都修复，也没有回答钱包长期消耗。
3. 随机持有期限会给退出提供时间理由，但不会自动产生价值判断。跟随/反向概率则只说明方向选择，未回答为什么某人这时开始持有。
4. 下一轮若继续，应先描述少量交易者的建仓动机、持有观察量和退出理由，再决定成本记忆、持有期限等最小规则；是否新增仓位数据、外价、费用或跨市场机制都须单独确认。本轮未选择参数、组合比例或架构。

本轮检索并核读的范围内，**未找到同时具备“封闭有限钱包、无外部基本价/参考市场、人类式建仓—持有—退出生命周期”的恒乘积 AMM 模型**。TokenLab满足较明确的成本价退出却不经 CPMM；XRPL、Angeris 和 Hafner–Dietl 经 CPMM 却依赖外部市场/历史订单；Agent0 是另一类利率 AMM 的执行框架。这个检索结果不等于此类模型不存在。
