/**
 * site_config 前端 metadata：业务分组、展示顺序与 label/description/unit。
 *
 * 后端 keys 都按前缀命名 (loan_* / liquidation_* / bot_* / activity_mode_*)，
 * 这里按前缀派生分组；description 是为 admin review 时显示的人读说明。
 *
 * 加新 key 时按命名约定 `<group>_<name>` 就自动归入对应组。
 * label/description/unit 留空 fallback 默认显示 key。
 */

export type ConfigGroup =
  | 'display'
  | 'fx'
  | 'loan'
  | 'liquidation'
  | 'anti_bot'
  | 'economy'
  | 'general'

export interface ConfigMeta {
  group: ConfigGroup
  label: string         // 人读名
  description: string   // tooltip 解释
  unit?: string         // 后缀单位 "sec" / "%" / "金" / "笔" / ""
}

/** 显式 metadata。未列出的 key 用 fallback (group=general, label=key)。*/
const META: Record<string, ConfigMeta> = {
  homepage_fx_enabled: {
    group: 'display',
    label: '首页使用 FX 模式',
    description: '开启展示外汇首页，关闭恢复原预测市场首页。保存后刷新首页即可生效；只改变首页展示，不改变交易或借款开关。',
  },
  credit_leverage: {
    group: 'loan',
    label: '名义杠杆',
    description: '1 < 倍数 <= 50；预测市场与 FX 共用此上限。50x 对应最多借入净值的 49 倍，维持率须先调至低于 1/49；修改后重启后端生效。',
    unit: 'x',
  },
  credit_maintenance_ratio: {
    group: 'loan',
    label: '强平维持率',
    description: '清算净值低于风险基数乘以此比例时触发强平；必须低于初始保证金率 1/(名义杠杆-1)，修改后重启后端生效。',
    unit: '比例',
  },
  credit_new_risk_frozen: {
    group: 'loan',
    label: '冻结新增风险',
    description: '开启后停止新增借款和买入等增险操作；还款与减仓仍应保留。',
  },
  credit_risk_retry_limit: {
    group: 'loan',
    label: '风险检查重试次数',
    description: '并发更新时风险检查允许重试的次数；通常无需调整，修改后重启后端生效。',
    unit: '次',
  },
  fx_short_enabled: { group: 'fx', label: 'FX 开空闸', description: '默认 false；只控制新增空头，关闭后仍允许正常回补。', unit: 'true / false' },
  fx_enabled: {
    group: 'fx',
    label: 'FX 交易总闸',
    description: '关闭时不允许 FX 成交，只读行情仍可用。',
  },
  // ── 借款系统 ─────────────────────────────────────────────────────
  loan_enabled: {
    group: 'loan',
    label: '借款功能',
    description: '关闭后所有用户无法 /loan/borrow 借款；已有债务的还款不受影响',
  },
  loan_daily_rate: {
    group: 'loan',
    label: '日利率',
    description: '每日利率按小数填写，0.01 表示每天 1%。统一信贷运行期间禁止修改，须在停写维护中结清旧率利息后调整。',
    unit: '比例/天',
  },
  loan_sweep_interval_sec: {
    group: 'loan',
    label: '利息复利扫描间隔',
    description: 'loan_sweep scheduler 多久跑一次 (定期 accrue 长期未操作用户的利息)',
    unit: 'sec',
  },

  // ── 强制平仓 ─────────────────────────────────────────────────────
  liquidation_enabled: {
    group: 'liquidation',
    label: '强制平仓总开关',
    description: '关闭时 sweep 不会触发任何强平动作 (但仍记录 BotSuspicion 等)。生产默认 false',
  },
  liquidation_sweep_interval_sec: {
    group: 'liquidation',
    label: 'sweep 扫描间隔',
    description: 'liquidation_sweep scheduler 多久扫一次有 debt 的用户',
    unit: 'sec',
  },

  // ── 反脚本 ───────────────────────────────────────────────────────
  activity_mode_enabled: {
    group: 'anti_bot',
    label: '活动模式 (L2)',
    description: '启用后所有 /market/{buy,sell,quote} 必须带有效 X-Client-Token，白名单 user 除外。活动当天开，活动后关',
  },
  quant_whitelist_user_ids: {
    group: 'anti_bot',
    label: '白名单 user_ids',
    description: 'csv 格式 "1,5,12"。这些用户跳过 L2 + L4 (anti-bot 不限制自己的 quant)',
  },
  bot_detection_enabled: {
    group: 'anti_bot',
    label: '行为监控 (L4) 开关',
    description: '关闭时 scheduler 跳过扫描。默认 true，平时也运行收集行为数据',
  },
  bot_detection_interval_sec: {
    group: 'anti_bot',
    label: '扫描间隔',
    description: '行为监控扫描间隔，实际使用范围为 60–7200 秒；修改后重启后端生效。',
    unit: 'sec',
  },
  bot_detection_window_sec: {
    group: 'anti_bot',
    label: '扫描窗口',
    description: '每次扫近多久的 Transaction (信号在此窗口内统计)',
    unit: 'sec',
  },
  bot_freq_threshold: {
    group: 'anti_bot',
    label: '高频信号阈值',
    description: '窗口内总笔数超过此值 → 触发 high_freq 信号',
    unit: '笔',
  },
  bot_late_night_threshold: {
    group: 'anti_bot',
    label: '凌晨信号阈值',
    description: '03-06 CST 时段笔数超过此值 → 触发 late_night 信号',
    unit: '笔',
  },
  bot_interval_stddev_ms_threshold: {
    group: 'anti_bot',
    label: '间隔规律性阈值',
    description: '交易间隔的标准差低于此值 (越规律越像 bot) → 触发 regular_interval 信号',
    unit: 'ms',
  },
  bot_fast_follow_trigger_cost: {
    group: 'anti_bot',
    label: 'fast_follow 大单门槛',
    description: '何为"大单"：单笔 abs(cost) 超过此值的交易作为 fast_follow 的 trigger',
    unit: '金',
  },
  bot_fast_follow_latency_ms: {
    group: 'anti_bot',
    label: 'fast_follow 跟进延迟',
    description: 'trigger 大单后多久内跟进算作"快速跟单"',
    unit: 'ms',
  },
  bot_fast_follow_count_threshold: {
    group: 'anti_bot',
    label: 'fast_follow 触发次数',
    description: '窗口内 fast_follow 事件次数超过此值 → 触发信号',
    unit: '次',
  },

  // ── 经济参数 ─────────────────────────────────────────────────────
  sell_fee_rate: {
    group: 'economy',
    label: '卖出手续费率',
    description: '预测市场卖出手续费，0 表示免费，范围 [0, 0.2)。FX 费率在各货币对配置；统一信贷运行期间须停写维护才能调整此项。',
    unit: '比例',
  },
  initial_balance: {
    group: 'economy',
    label: '新用户初始余额',
    description: '新用户首次 SSO 登录注册时获得的初始现金。仅影响新注册用户，不改动存量用户',
    unit: '金',
  },
}

const GROUP_ORDER: ConfigGroup[] = ['display', 'fx', 'loan', 'liquidation', 'economy', 'anti_bot', 'general']

// 按管理任务排列；未登记的新配置仍显示在所属组末尾。
const CONFIG_ORDER = [
  'homepage_fx_enabled',
  'fx_enabled', 'fx_short_enabled',
  'loan_enabled', 'credit_new_risk_frozen',
  'credit_maintenance_ratio', 'credit_leverage', 'loan_daily_rate',
  'loan_sweep_interval_sec', 'loan_sweep_min_accrual_sec', 'credit_risk_retry_limit',
  'liquidation_enabled', 'liquidation_partial_pct', 'liquidation_sweep_interval_sec',
  'initial_balance', 'sell_fee_rate',
  'activity_mode_enabled', 'quant_whitelist_user_ids', 'bot_detection_enabled',
  'bot_detection_interval_sec', 'bot_detection_window_sec',
  'bot_freq_threshold', 'bot_late_night_threshold', 'bot_interval_stddev_ms_threshold',
  'bot_fast_follow_trigger_cost', 'bot_fast_follow_latency_ms', 'bot_fast_follow_count_threshold',
]
const CONFIG_RANK = new Map(CONFIG_ORDER.map((key, index) => [key, index]))

export function compareConfigKeys(a: string, b: string): number {
  return (CONFIG_RANK.get(a) ?? CONFIG_ORDER.length) - (CONFIG_RANK.get(b) ?? CONFIG_ORDER.length)
    || a.localeCompare(b)
}

const GROUP_LABELS: Record<ConfigGroup, string> = {
  display: '页面展示',
  fx: 'FX 交易',
  loan: '借款与统一信贷',
  liquidation: '强制平仓',
  anti_bot: '反脚本',
  economy: '经济参数',
  general: '其他',
}

/** 推断未注册 key 的 group。从前缀派生。 */
function inferGroup(key: string): ConfigGroup {
  if (key.startsWith('fx_')) return 'fx'
  if (key.startsWith('loan_') || key.startsWith('credit_')) return 'loan'
  if (key.startsWith('liquidation_')) return 'liquidation'
  if (key.startsWith('bot_') || key === 'activity_mode_enabled' || key === 'quant_whitelist_user_ids') return 'anti_bot'
  if (key === 'sell_fee_rate' || key === 'initial_balance') return 'economy'
  return 'general'
}

export function getConfigMeta(key: string): ConfigMeta {
  const explicit = META[key]
  if (explicit) return explicit
  return {
    group: inferGroup(key),
    label: key,
    description: '',
  }
}

export function groupLabel(group: ConfigGroup): string {
  return GROUP_LABELS[group]
}

export function groupOrder(): ConfigGroup[] {
  return GROUP_ORDER
}
