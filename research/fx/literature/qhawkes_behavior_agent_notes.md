# QHawkes 与有行为交易者：文献核查笔记

检索、正文核读日期：2026-10-15。只做文献研究，未改模型、生产代码或既有数据，未运行模拟。本文由受委派的文献调研 agent 创建；直接 QHawkes 谱系的原文与 Fosset 两篇由主 agent 另行核查。

## 结论与证据边界

**有判断的交易者与弱收益方向相关、波动聚集可以共存。** 本轮最明确的阳性例子是 Yamada–Takayasu–Takayasu 的 2006 预印／2007 期刊 threshold-dealer model：它不设独立基本价值，显式加入异质交易者的趋势反应与成交间隔反馈，并同时画出收益方向相关快速消退、绝对价格变动长尾相关、成交间隔分布。它早于 QHawkes，属于机制相关的 ABM，不是把 QHawkes 中随机符号换掉的直接后续。[原文 §2.4、Eq.(5)、Fig.5](https://arxiv.org/pdf/physics/0608099)

**检索范围内未找到**同时具备“直接继承 Blanc–Donier–Bouchaud QHawkes、独立策略账户、有限现金和库存、无外部基本价值、恒乘积 AMM，并展示弱方向相关和波动聚集”的现成模型。这个结果不等于文献中不存在。下列候选分别满足部分要求；没有一篇能证明我们的 20 钱包 AMM 版本直接移植即可成功。

本轮主要查阅了 QHawkes＋agent/order book 的引用线索、作者预印本与作者网站，涉及 Wakatuki/Kanazawa、Vodret、Kanazawa、Kononovicius/Ruseckas、Yamada/Takayasu 与 Hawley/LeBaron/Paddrik/Palmer。正文优先于摘要；“未展示”只指所读版本未找到相应结果。

## 1. Yamada、Takayasu、Takayasu：threshold-dealer model（最贴近的阳性证据）

- **准确文献：** Kenta Yamada, Hideki Takayasu, Misako Takayasu, *Characterization of foreign exchange market using the threshold-dealer-model*，2006 预印，Physica A **382** (2007), 340–346。[arXiv:physics/0608099v2](https://arxiv.org/abs/physics/0608099)；[DOI:10.1016/j.physa.2007.02.027](https://doi.org/10.1016/j.physa.2007.02.027)
- **模型定位：** §2.1–2.4，Eqs.(1)–(5)。每位 dealer 有 bid (b_i)、固定 spread (L)，报价向成交靠拢；报价相交才成交，价格取在匹配 bid/ask 之间。成交后买卖身份翻转。趋势项为 \(\epsilon_i d_i\langle dP\rangle_\eta\)，\(\epsilon_i=+1/-1\) 允许跟随／逆向者；没有外生基本价值。[正文 §2.1、§2.3](https://arxiv.org/pdf/physics/0608099)
- **活动反馈：** Eq.(5) 把报价速度除以 \(G_k\langle T_k\rangle_\tau\)，并令 \(dt=G_k\langle T_k\rangle_\tau dt'\)。这是过去成交间隔的自调时钟，**不是**过去波动直接提高 Hawkes 到达率；model-4 合并趋势反应和该时钟。[§2.2、§2.4](https://arxiv.org/pdf/physics/0608099)
- **账户边界：** 有单单位交易后翻转方向的交易循环；没有完整现金、净值、保证金、破产或多单位有限库存账。不能称作有限钱包模型。
- **实际结果：** Fig.5C 的价格变动 ACF 在一阶负值后很快衰减，5D 的绝对变动 ACF 长尾；5E/F 为厚尾、非 Poisson 间隔；5G 比较扩散。其时间 lag 多为成交 tick，不应直接与本项目固定时间桶 ACF 对齐。Fig.5 未单列 \(\epsilon_i\) 的人口比例，不能声称该图已证明某个跟随／逆向混合比例。[§2.4、Fig.5、Table I](https://arxiv.org/pdf/physics/0608099)
- **复现保留项：** \(c_i\) 的符号说明与图中的带符号范围、\(G_k\) 的实现及时间归一化仍需代码级复现时处理；本轮不作参数移植承诺。

## 2. Vodret 等：有适应性策略的 GARCH 微观基础（同团队，非直接 QHawkes 变体）

- **准确文献：** Michele Vodret, Iacopo Mastromatteo, Bence Toth, Michael Benzaquen, *Microfounding GARCH Models and Beyond: A Kyle-inspired Model with Adaptive Agents*，2022 预印／Journal of Economic Interaction and Coordination **18** (2023), 599–625。[arXiv:2206.06764](https://arxiv.org/abs/2206.06764)；[正文 PDF](https://arxiv.org/pdf/2206.06764)
- **显式策略与定价：** 三类代表 agent：知情者 IT、噪声 NT、做市者 MM。§2 Eqs.(4)–(6)：\(p_t=\Lambda_tq_t\)，IT 用上轮冲击估计做收益最优化，\(q_t^{IT}=p_t^F/(2\Lambda_{t-1})\)；MM 用自己的方差信念过滤总订单。§2.2 Eq.(10) 用近期价格方差修正基本价方差信念。[§2、§2.2](https://arxiv.org/pdf/2206.06764)
- **反馈及证据：** 近期波动→MM 信念→冲击系数→后续波动，§3.1 Eq.(12) 得 Kesten 方差过程；Fig.5/6 画方差 ACF 和尾部分布，证明间歇波动而非仅由基本创新聚集输入。[§3](https://arxiv.org/pdf/2206.06764)
- **不满足处：** 有外部 Gaussian 基本价和噪声订单；每轮交易，不含 vol 高→事件到达加速；无有限钱包／累计库存约束。未看到收益方向 ACF 的独立展示；结尾将相关 excess demand 列为未来扩展。§4.2 Eq.(27) 的成本敏感 NT 反而在冲击高时缩单：\(q_t^{NT}=q_t^{tgt}/(1+\phi\Lambda_{t-1})\)。不能把它描述为趋势交易者 QHawkes，也不能用它保证放大到达量。[§4.2、结论](https://arxiv.org/pdf/2206.06764)

## 3. Kanazawa 等：由真实 FX 逐户轨迹支持的趋势做市商

- **准确文献：** Kiyoshi Kanazawa, Takumi Sueshige, Hideki Takayasu, Misako Takayasu, *Derivation of the Boltzmann Equation for Financial Brownian Motion: Direct Observation of the Collective Motion of High-Frequency Traders*，2017 预印／PRL **120**, 138301 (2018)。[arXiv:1703.06739](https://arxiv.org/abs/1703.06739)；[DOI:10.1103/PhysRevLett.120.138301](https://doi.org/10.1103/PhysRevLett.120.138301)
- **配套完整理论：** 同作者，*Kinetic Theory for Finance Brownian Motion from Microscopic Dynamics*，2018。[arXiv:1802.05993](https://arxiv.org/abs/1802.05993)
- **行为与形成价格：** PRL Eqs.(1)–(4) 先验证个体报价变化的饱和趋势反应，再设 \(dz_i/dt=c\tanh(\Delta p/\Delta p^*)+\sigma\eta_i\)。每人同时提供单位 bid/ask、个人 spread 固定；双方报价碰撞成交后重新报价。没有独立基本价值；没有资金／累计库存账。[PRL 正文 pp.2–3](https://arxiv.org/pdf/1703.06739)
- **实际展示及限制：** 完整理论 §VI.D Fig.13 的边界趋势情形给轻微负一阶价格相关、较长期正常扩散；强趋势 Fig.12 有明显正方向相关。主要验证簿形、层状流动性、短窗口指数价格尾与不同扩散。§VII/Table III 的稳定参数成交间隔尾仍为指数；厚尾跨窗口混合涉及非平稳交易者数量等。未证明稳定参数内生长期波动聚集，也未展示有记忆的订单符号和弱价格方向相关的同时成立。[§VI.D、§VII.A、§VII.C](https://arxiv.org/pdf/1802.05993)
- **定位：** 是真实行为证据很强的 ABM，非 QHawkes 直接后续；适合参考报价判断，单独不够回答活动聚集。

## 4. Kononovicius、Ruseckas：情绪／策略人口驱动的活动时钟

- **准确文献：** Aleksejus Kononovicius, Julius Ruseckas, *Order book model with herd behavior exhibiting long-range memory*，2018 预印／Physica A **525** (2019), 171–191。[arXiv:1809.02772](https://arxiv.org/abs/1809.02772)；[DOI:10.1016/j.physa.2019.03.059](https://doi.org/10.1016/j.physa.2019.03.059)
- **agent 与价格：** §4.2 Eqs.(17)–(23) 有双边报价 chartists、情绪控制随机方向的市价单、按固定基本价 \(P_f\) 判断划算与否的 fundamentalists。市场单在簿中成交；共享估值更新为成交价。明确把 Kanazawa 原趋势报价规则简化掉，不能称作保留完整趋势判断。[正文 §4.2](https://arxiv.org/html/1809.02772v2#S4.SS2)
- **反馈：** Eq.(14) 为 \(\tau^{-1}=y^\alpha\)，\(y=N_c/(N-N_c)\) 是绝对回报的慢调制代理；Eqs.(20),(23) 将它用于交易率，(24),(25) 用于策略切换率，有限人口时修正见(26)。这是内生人口／情绪状态调时钟，非直接观察历史收益的 z²；无钱包／库存约束。[§4.1–4.2](https://arxiv.org/html/1809.02772v2)
- **结果：** §5 Fig.6/7 对 Bitcoin／NYSE 的绝对回报与每分钟成交数 PDF、PSD 做比较，展示活动和幅度的长程统计。未找到 raw-return ACF 结果，不能仅凭标题 long-range memory 宣称已满足弱方向相关；有固定外部基本价值。[§5](https://arxiv.org/html/1809.02772v2#S5)

## 5. Wakatuki／Wakatsuki、Kanazawa：直接 QHawkes 微观化线索，但策略含量有限

- **准确文献：** Taiki Wakatuki, Kiyoshi Kanazawa, *Market price simulation and theoretical analysis based on trader-level micro-models*（日文：トレーダーレベルのミクロモデルに基づく市場の価格シミュレーションと理論的解析），JSAI2024，2F1-GS-5-02，2024。[官方两页正文](https://www.jstage.jst.go.jp/article/pjsai/JSAI2024/0/JSAI2024_2F1GS502/_pdf/-char/ja)
- **直接谱系：** 引 Blanc et al. 2017，称从推广 Santa Fe 簿推导 QHawkes。§2.1 每人被他人的市价单通过二次核改变强度；§2.2 限价单按固定强度取消、按离 mid-price 距离重新放单。不是自主价值判断、趋势预测或收益优化账户；未给财富约束或完整方程。[§2](https://www.jstage.jst.go.jp/article/pjsai/JSAI2024/0/JSAI2024_2F1GS502/_pdf/-char/ja)
- **展示边界：** §3 报告交易者人数增加时单次价格变动的期望幅度减小，以及强度分布幂尾；两页稿没有收益 ACF／绝对回报 ACF 曲线与完整校准。可称“带个人身份的交互订单微观基础”，不能宣传为已找到用户要求的正常策略替代品。

## 6. Hawley 等：近期最清楚的波动→报价→流动性反馈

- **准确文献：** Andrew Hawley, Blake LeBaron, Mark Paddrik, Nathan Palmer, *Dynamic order dispersion and volatility persistence in a simple limit order book model*，2024 工作稿／Journal of Economic Dynamics and Control **188** (2026), 105334。[DOI:10.1016/j.jedc.2026.105334](https://doi.org/10.1016/j.jedc.2026.105334)；[作者公开 2024 完整稿](https://bdlebaron.github.io/pdfs/limit_order_book.pdf)。下面公式／图号以该 2024 稿为准，不能假定最终刊文图号相同。
- **策略与价格：** §3.2 为有异质固定基本价值的均值回复预测者＋Gaussian 噪声预测者；取消了原 Chiarella/Iori 趋势者。按预测判断买卖，单位订单；§3.3 的价格来自订单簿匹配。无有限现金／库存过程。[作者稿 §3.1–3.3](https://bdlebaron.github.io/pdfs/limit_order_book.pdf)
- **关键机制：** §3.4 Eq.(12) 把报价折让 \(k_i\) 变为 \(\tilde k_i=(\sigma_S/\sigma_L)k_i\)，波动高时离中间价更远。参与机会随机抽取，市价单概率固定，故非波动提高到达率；反馈在可执行流动性。[作者稿 §3.4](https://bdlebaron.github.io/pdfs/limit_order_book.pdf)
- **实际证据：** Fig.5：raw-return ACF 近零、绝对回报 ACF 持续为正及厚尾；Fig.7 看不同采样跨度的方向 ACF，短跨度有 bid/ask bounce；§5 延长订单存活、增加深度等可关掉持续性。自称 near-zero-intelligence，是有有限行为规则的模型，不能包装成完善策略优化。[作者稿 §4.3–4.4、§5](https://bdlebaron.github.io/pdfs/limit_order_book.pdf)

## 额外线索的处理

- Yamada, Takatoshi Ito, Hideki Takayasu, Misako Takayasu，*Order book model with feedback effects*，2014：[会议官方一页稿](https://webpark2049.sakura.ne.jp/smsec2014/index.files/Papers/135.pdf)。全文文字明确讨论波动影响挂单距离、趋势和心理时间反馈，也说观察到聚集；但无模型公式、数值图、账户约束，不能作为独立可复现方案主证据。
- Cont 的阈值模型很相关，但本轮没有核到正式合著版本全文；单作者 arXiv:cond-mat/0407626 最新版本已因过早初稿撤回。**不把该撤回预印当成熟已核正文证据**。2005 Ghoulmié–Cont–Nadal 正式文 DOI 为 10.1088/0953-8984/17/14/015；如继续研究，应另读期刊正文／作者正式稿。
- 检索中还出现 Chiarella/Iori/Perelló、有限杠杆基金等模型；未在本轮扩展为主候选，避免把部分摘要线索当已核全条件的证据。

## 两条有证据的研究路线，以及 AMM 的实质差别

### 路线 A：先参考 dealer 的报价判断与交易循环，再考虑活动自调

**文献确证：** Yamada 2007 model-4 在无外部基本价的同一模型中同时给出趋势反应、活动自调、弱方向相关和持续幅度相关。Kanazawa 2018 说明双边报价的趋势反应还能在真实逐户 FX 数据中被观察到，但其更强的趋势并不自动有利于低方向相关。[Yamada §2.4](https://arxiv.org/pdf/physics/0608099)；[Kanazawa Fig.12/13](https://arxiv.org/pdf/1802.05993)

**对本项目的推断：** “有判断”不必等价于把预测直接变成一个长时间追逐的净目标仓位。文献中的判断主要改变保留报价／可接受成交价，成交后又重新报价或翻转买卖任务；这能提供持续交易循环，可能避免固定净目标差额很快耗尽。这里只是机制解释，没有证明该循环在本项目公平、盈利或不会资金退化。

**移植差别：** dealer 模型由双方报价接触形成价格；恒乘积 AMM 的价格已由储备决定。若把 dealer 报价改成“AMM 可执行报价满足我的阈值才成交”，就已改变成交事件与报价相对运动机制；再加有限钱包，原文方程、参数和统计证据均不再直接适用。成交后翻方向也不能冒充完整的钱包风险管理。保留原始成交间隔时钟或保留当前 z² 时钟是另一个模型选择，不能声称二者等价。

### 路线 B：让行为作用在可执行流动性，分开方向预测与幅度反馈

**文献确证：** Vodret 的主要反馈在 MM 的冲击函数；Hawley 的反馈在波动相关的挂单距离，且动态报价下方向 ACF 仍近零。它们说明持续波动可以来自流动性对风险感知的反应，不需要通过持续同向净买单实现。[Vodret §2.2–§3.1](https://arxiv.org/pdf/2206.06764)；[Hawley §3.4、Fig.5/7](https://bdlebaron.github.io/pdfs/limit_order_book.pdf)

**对本项目的推断：** 目前保留恒乘积曲线、只改变交易者目标仓位，缺少上述自主改报价、撤单或收缩流动性的自由度。恒乘积储备的经济深度本来会随价格／储备变化，但它不会因 dealer 主观风险上升而撤掉 near-price orders；所以不能把固定 AMM 当成文献的适应性 MM 或订单簿。若希望复制此路线，必须先讨论是否允许风险相关的流动性供给或其他价格形成改变，这涉及已约定需用户确认的技术／模型路线。单纯加大预测、噪声或到达强度不是文献保证的等价替代。

## 对当前失败实验能、不能下的判断

可下的判断是：旧三因子→净目标→每次调差额这一具体闭环，与 dealer 的报价／成交任务循环及风险相关流动性是不同机制；当前失败不能推出“有判断的交易者与 QHawkes 式统计不兼容”。不能下的判断是：换成任一篇规则即可在 20 个有限钱包、封闭 AMM 里稳定复现。本文没有发现替该系统完成有限财富长期稳态论证的文献。
