# 统一信贷成为唯一内核

用户于 2026-10-10 确认彻底删除旧贷款模式，旧接口字段一起删除，仓库内前后端同步更新；执行使用 GPT-6.1 Sol low，审阅 medium。

## 目标与边界

统一信贷是唯一账户风控、借贷和清算路线。不保留模式总闸、不把模式总闸固定 true 留作兼容，不保留旧执行器。复用已有真实金债和外币借贷、账户级净值与保证金、统一强平 run/action。无需改变经济表结构或重建用户数据库。

保留 `User.cash/debt`、外币欠币、锁金、利息、账本、审计、用户经济版本、账户停用及授信冻结。保持 AMM/LMSR 数学、按市场串行执行、所有权、只读实例、并发版本重验、未知回补成本和坏账保护。所有经济写入始终要求写所有权；SQLAdmin 经济模型原始 CRUD 始终禁用。市场 writer 仍按现有生命周期运行，有写权限实例始终启动它。

独立运营开关 `fx_enabled`、`fx_short_enabled`、`loan_enabled`、`liquidation_enabled`、`pve_enabled` 与 `credit_new_risk_frozen` 保留现值，不自动开市。新库借款默认关闭，重建工具继续关闭业务开关并冻结新增风险。

## 配置与升级

删除 `unified_credit_enabled`、`loan_leverage_k`、`liquidation_hard_threshold`、`liquidation_soft_threshold`、`liquidation_target_margin`、`liquidation_emergency_threshold`。保留共享 `liquidation_partial_pct`、扫描间隔和总闸、名义杠杆、维持率、日利率和风险重试次数。

新库直接种 `credit_leverage=2`、`credit_maintenance_ratio=0.2`，不先种旧参数再派生。升级已有库保留已配置的合法统一参数；若缺失，从旧合法参数一次性映射（名义杠杆 = 旧 k + 1，维持率 = 旧 hard），之后删除旧键。完全空库使用上述默认值。非法显式参数/非法映射须报错，不静默降低风控。所有实例加载合法统一参数，配置不合法拒绝启动。迁移不改账户/持仓/债务/权益，也不改历史 migration。

## 接口与界面

删除响应中的模式标志、`leverage_k`、`legacy` 和旧保证金字段及分支。贷款额度统一返回 `credit_leverage`（Decimal 字符串）以及现有 `r_initial/r_maintenance`、现金/锁金/净值/风险状态/额度。强平 policy 保留 `enabled`、`credit_leverage`、`r_initial`、`r_maintenance`、费率、`partial_pct`、`sweep_interval_sec`，去掉旧 hard/soft/target/emergency 与 legacy 结构。用户 summary 保留已有统一风险字段，去掉模式标志和纯旧模式字段。

前端贷款、FX/LMSR 交易、资产/风险卡仅展示统一口径，不显示逐仓杠杆。后台套餐直接定义名义杠杆与维持率；保持降低杠杆先改杠杆、提高杠杆先改维持率的合法保存顺序。删除旧模式文案、旧配置 metadata、旧测试状态；日利率/费率维护限制与重启提示保留。

## 验证

复用现有测试验证：新库与已有库升级、参数保留与非法拒绝、所有权/只读、借还款、真实 FX 借币与回补、LMSR/FX 有债风险、账户展示、强平幂等与恢复、重建权益保留。只覆盖具体新增缺口，不复制算法、不加源码字符串测试。删除只针对被移除执行器/模式的测试，不放宽仍有效的财务断言。前端运行现有测试、类型检查、构建；无自动浏览器交互。

用户随后明确要求任务完成后审阅、提交推送、开 PR、合并并实装。采用现有 CI/CD 部署，新 migration 只清理配置；不再次重建或重置用户数据库。
