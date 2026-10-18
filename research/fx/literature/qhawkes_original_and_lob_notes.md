# QHawkes原文及直接后续：对当前AMM实验的含义

2026-10-15。本轮是文献核验，没有改模型、参数或模拟数据。主agent读原文和直接后续，另派subagent查行为交易者路线；其笔记见同目录qhawkes_behavior_agent_notes.md。

## 原模型的定位需要说清

Blanc、Donier、Bouchaud，*Quadratic Hawkes processes for financial prices*，2015预印、2017期刊，Quantitative Finance 17(2),171–188。[原文](https://arxiv.org/html/1509.07710)。§2.3直接假设不可预测、独立同分布、零均值且有限方差的价格跳幅；§2.2用lambda=lambda_inf+H+Z²控制跳动强度。它没有显式账户、报价、财富、库存或策略选择。导论和结尾把行为微观基础列为未解决问题。

我们v1是该思路的离散、有限钱包AMM改编；并非原论文的随机交易者程序。三因子接入后，买卖需求依赖历史与库存，价格由真实执行产生，已改变原来的跳幅组织方式。原文的鞅/方向结论不能不经验证地转移。

## 直接后续确实更接近订单行为，但仍有简化

Fosset、Bouchaud、Benzaquen，*Endogenous liquidity crises*，2019预印、2020期刊，J. Stat. Mech.063401，doi:10.1088/1742-5468/ab7c64。[正文](https://arxiv.org/html/1912.00359)。§2实证检查价格历史对挂单、撤单和市价单的反馈；§3仍用零智能Santa Fe模型。§3.1 Eq(4)的数值简化主要提高撤单率nu=nu0+alpha*Z²，市价买卖各半。流动性减少可放大波动；这不是有因子预期和有限钱包的策略模型，也不应称已同时解决收益相关/长期稳定性。

同三作者，*Non-parametric Estimation of Quadratic Hawkes Processes for Order Book Events*，2020预印。[正文](https://arxiv.org/html/2005.05730)。§2.1对买卖侧的挂单、撤单、市价单六类事件建模，§3.2区分波动与趋势平方反馈。重要口径：§2.2.2 Eq(5)–(6)用micro-price并扣掉线性可预测部分，形成surprise price；不能把校准变量较弱的方向记忆当作原始成交价格自发涌现的证据。该文无逐户收益最大化或封闭钱包过程。

## 一个与当前失败解释有关的微观结构提示

Bouchaud、Farmer、Lillo，*How markets slowly digest changes in supply and demand*，2008预印、2009书章。[正文](https://arxiv.org/html/0809.0822)，§6.5.1–6.5.4。相关订单流可以与弱收益相关并存：更可预期的订单方向获得较小影响，或历史影响逐渐衰减。该综述的模型是解释框架，不是有限钱包AMM验证。

对我们而言，这是调查提示：CPMM价格响应由当前储备和订单决定，没有单独按可预期订单流调节的报价/撤单行为；仅增加交易者因子或个人参数，不一定实现文献中订单流与流动性之间的配合。这是机制比较及待检验推断，未证明它就是我们失败的原因，也不等于现在应改掉AMM。

## 有行为交易者的阳性例子：先读dealer模型

Yamada、H. Takayasu、M. Takayasu，*Characterization of foreign exchange market using the threshold-dealer-model*，2006预印、2007 Physica A382,340–346，doi:10.1016/j.physa.2007.02.027。[10页原文](https://arxiv.org/pdf/physics/0608099)。这是早于QHawkes的相关行为路线，不是其直接后续。

§2.1–2.4中，每人有自己的报价调整速度和趋势反应；报价交叉才成交，成交后买卖角色翻转。model-4把趋势反应与过去成交间隔驱动的时钟自调合在一起。Fig5C显示短期负相关后快速衰减，D为绝对价格变化相关长尾，E/F为变动分布和非泊松间隔，G为扩散性质。无需外部基本价值，但没有完整现金/财富约束；时钟也不是直接由vol或z²控制。精确复现前还需澄清初始角色、ci符号与图示参数等实现细节。

它是“有行为规则也能得到目标统计特征”的存在性证据，不能直接作为我们CPMM已验证配方。建议先把它作为文献阳性对照读懂，再比较交易时钟、持仓循环和报价成交与当前目标仓位调仓的差别。

## 最近的相近路线：Chiarella系模型的报价反馈

Hawley、LeBaron、Paddrik、Palmer，*Dynamic order dispersion and volatility persistence in a simple limit order book model*，2024工作稿、2026 JEDC188,105334。[期刊](https://www.sciencedirect.com/science/article/pii/S0165188926000801)；[作者完整稿](https://bdlebaron.github.io/pdfs/limit_order_book.pdf)。作者稿§3.4 Eq(12)用短/长波动比放大报价距离，§4.3 Fig5显示低方向相关与持续绝对收益相关。模型基于Chiarella–Iori，基础预测有回归基本价和噪声，加入风险相关报价后才得到持久波动。

这符合“保留简单判断、让行为改变波动”的思路，但仍是近零智能订单簿模型，使用固定基本价（可有小幅异质信念），没有封闭钱包；成交机会及市价概率未由波动加速。不能当作无基本价、有限钱包或QHawkes直接变体。公式/图号以作者2024稿为准，最终期刊版本应另核。

## 本轮的判断

1. 已有实验只否定当前几种移植组合；不能否定有策略交易者或异质性整个路线。
2. 在核验的QHawkes直接谱系中，尚未找到可直接复用的“无独立基本价＋有判断账户＋封闭有限钱包＋CPMM＋全部目标统计”的成熟变体；这不是不存在性证明。
3. 优先读dealer model-4及行为/自适应流动性模型，后者的独立基本价值等条件必须标明。先校准文献与我们执行语义的差别，再决定新实验，避免继续无依据堆参数。

核验范围：正文/公式和图注已读；原PDF截图接口缓存失败，没有根据图片像素估计ACF或尾指数。未复现论文模拟，未做全领域系统性综述。
