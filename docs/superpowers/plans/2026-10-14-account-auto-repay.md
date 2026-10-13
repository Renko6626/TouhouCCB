# 账户自动还债开关

目标：账户级“自动还债”，默认开启。个人页面与 FX 操作选择按钮下方共用同一设置。FX 新增一行左侧为“自动还债”开关，右侧为“借款与还款”跳转现有贷款页面。

已确认语义：关闭时，LMSR 卖出和 FX 多头卖出所得留作现金，贷款继续计息；手动交易及挂单执行时遵循账户当前设置。外币回补必需还债、开空所得锁定及强平还款不受影响。保持现有风险计算和准入。

实现沿用现有 FastAPI/SQLModel/Alembic、Vue/Pinia，不增加依赖、算法或恢复机制。

- [ ] Backend：User.auto_repay 非空 bool 默认/server default true，新增可升级回滚迁移；auth/me、user summary、交易 AccountBasis 返回字段。PATCH /user/me/auto-repay 收严格 bool，使用现有 User 行锁/写权限/经济版本和账户变更通知，返回 {auto_repay: bool}。
- [ ] Backend：FX _simulate 与真实 spot 卖出、LMSR 正常卖出条件一致。挂单沿用共享执行核心。强平和外币 short ledgers 不改。用既有 fixtures 覆盖禁用后现金/债务/报价一致和默认行为，复用真实业务测试；API 自账号持久化与 strict bool 验证。仅必要测试。
- [ ] Frontend：auth User 类型/API/store 保存设置；一个简单共用 AutoRepaySwitch 在 Portfolio 和 Fx 复用，标题仅“自动还债”，失败提示并保持原值，保存中禁用，默认旧数据回退开启，不加请求队列。交易 AccountBasis 类型与 FX/LMSR 预览读取服务端设置；保存后使用既有刷新更新报价上下文。
- [ ] 集中相关 backend tests、frontend unit/typecheck/build、diff check；Sol medium 审实际变更与财务一致性。禁止浏览器自动交互、不扩展可靠性或新增架构；部署按既有流程。

审阅重点：默认行为保持，关闭后实际和两个产品预览一致，混合本币贷款/外币空头锁金不被用于还债，挂单读执行时设置，强平和回补不受用户偏好影响。
