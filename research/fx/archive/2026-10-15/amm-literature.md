# AMM 行情统计特征：文献初查

2026-10-14。本次搜索直接涉及恒定乘积AMM、Uniswap收益统计和AMM仿真的原始研究。这里区分链上实证、真实订单重放和自主交易者仿真；三类证据不能互相替代。未运行新模型。

## 直接相关的研究

| 研究 | 类型与本次阅读范围 | 对我们最相关的内容 | 证据边界 |
| --- | --- | --- | --- |
| [Deviations from Tradition: Stylized Facts in the Era of DeFi](https://arxiv.org/html/2510.22834v1)，Di Nosse、Gatta、Lillo、Jaimungal，2025预印本版本 | Uniswap v3实证；读取第4节及引言 | 2023～2024年24池；非高斯、聚合趋近高斯；多数普通/波动资产池的绝对收益相关衰减与长记忆一致，稳定币/合成资产池多为短记忆 | v3集中流动性、区块与MEV结构有额外影响；作者的ACF幂律拟合不能直接当作我们的严格长记忆验收；不是封闭机器人仿真 |
| [Stylized facts in Web3](https://arxiv.org/abs/2408.07653)，Chen、Silva、Tung，2024，arXiv标注published version | 多市场实证；读取全文中5.6节及尾部、相关部分 | 比较Uniswap与中心化交易所；研究认为套利连接了两边价格，样本中CEX领先DEX | AMM相似性可能包含外部价格输入；不能作为AMM内部独立涌现的证明 |
| [A Shuffled Replay of Events on Uniswap](https://www.frontiersin.org/journals/blockchain/articles/10.3389/fbloc.2022.745101/full)，Imon Palit，Frontiers in Blockchain，2022 | 同行评审论文；真实事件打乱重放 | 保留AMM规则，改变订单时序；打乱组仍非高斯且波动更大，价格更接近随机游走 | 不以复现六项特征为目标；交易规模来自真实历史，不能说明轻尾独立订单也会生成稳定幂律 |
| [Price Dynamics of Automated Market Makers: Simulation-based Approach](https://dspace.cuni.cz/handle/20.500.11956/186114?locale-attribute=en)，Jan Kubal，Charles University，2023 | 已答辩硕士论文；本次只核查仓储元数据与英文摘要 | 10交易对、15天数据；恒定乘积仿真模仿成交，并加入hype与herding；摘要报告分布、相关及波动聚集的相似性 | 非期刊论文；全文请求受限，算法、参数与检验尚未核查，不认定已复现长期记忆或稳定幂律 |

Kubal论文的[可读取英文摘要](https://dspace.cuni.cz/bitstream/handle/20.500.11956/186114/120455332.pdf?isAllowed=y&sequence=3)只有一页，不是论文全文。本次仓储访问出现429/抓取失败，未据摘要编造hype和herding的实现。

## 相邻的AMM模型研究

[An analysis of Uniswap markets](https://arxiv.org/pdf/1911.03380)，Angeris等，2019/2021版本，提供AMM理论与agent-based模拟，主要讨论套利、外部参考价格跟踪及稳定性。它适合核对AMM市场结构，但不提供“同质机器人内生复现六项特征”的结论。

[An arbitrage driven price dynamics of Automated Market Makers in the presence of fees](https://arxiv.org/abs/2401.01526)，Najnudel等，2024，引入几何布朗运动参考价格及费用决定的套利边界。它研究AMM价格动力学，但参考价格是外生输入，不能当作内生波动聚集机制。

## 可以从文献得出的判断

确实有同行直接研究AMM的非高斯收益、聚合分布和波动记忆，也有真实事件重放与相关仿真。因此AMM这种成交机制本身并不排斥出现这些现象。本次没有找到已经核查、采用与我们相同的封闭恒定乘积池、共同三因子参数、恒定泊松到达和tanh持仓，且可靠复现全部六项特征的研究；这只是本次检索范围内的结果，不是声称这样的研究不存在。

我们要回答的是：不输入真实订单或外部价格时，机器人互动能生成什么？外部市场里的聚集性经套利传入AMM，和我们的内生复现问题不同。链上样本还会受到流动性变化、MEV及时间采样口径影响，不能直接拿单tick峰度比较不同市场。

共同窗口可能促成我们观察到的周期，但“只有一个典型时间尺度”尚未被证明是缺失特征的原因。异质性、活跃度变化及流动性反馈是可能机制，不是看到论文关键词后就应一起加入的功能。若后续研究，优先用分别改变窗口分布、到达机制或群体行为的对照来识别作用，保留当前AMM和净持仓约束；任何一个机制都需独立检验，不能为追求指标而同时加入。

尤其要区分：非高斯、超额峰度、收益尾部幂律、绝对收益ACF幂律衰减，是四种不同的陈述。上述文献的发现可作为研究线索，不能降低本项目已经审查过的拟合与验证要求。

## 下一步阅读顺序

1. Di Nosse等第4.3节：理解真实AMM的波动相关及池类型差异。
2. Palit的订单重放方法：理解交易时序与定价规则的分离，不把随机化等同于简单高斯订单。
3. 获得并核查Kubal全文后，确认其hype/herding是外加状态、从真实订单校准的规律，还是自主交易者互动。
4. 用Web3实证论文核对外部价格传导、费用和采样尺度，避免将输入市场的特征误作自身涌现。

以上是后续研究方向，本次只整理文档与文献证据。
