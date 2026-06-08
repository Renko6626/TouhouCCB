/**
 * site_config 前端 metadata：按前缀分组 + label/description/unit。
 *
 * 后端 keys 都按前缀命名 (loan_* / liquidation_* / bot_* / activity_mode_*)，
 * 这里按前缀派生分组；description 是为 admin review 时显示的人读说明。
 *
 * 加新 key 时按命名约定 `<group>_<name>` 就自动归入对应组。
 * label/description/unit 留空 fallback 默认显示 key。
 */

export type ConfigGroup =
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
  // ── 借款系统 ─────────────────────────────────────────────────────
  loan_enabled: {
    group: 'loan',
    label: '借款功能',
    description: '关闭后所有用户无法 /loan/borrow 借款；已有债务的还款不受影响',
  },
  loan_daily_rate: {
    group: 'loan',
    label: '日利率',
    description: '每日复利率，每次 borrow/repay 时按经过的天数 accrue',
    unit: '%/day',
  },
  loan_leverage_k: {
    group: 'loan',
    label: '杠杆倍率',
    description: 'max_borrow = max(0, k × net_worth - debt)；k=2 表示用户能借到净值的 2 倍',
    unit: 'x',
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
  liquidation_hard_threshold: {
    group: 'liquidation',
    label: '强平触发线',
    description: 'LCV margin 低于此值时触发强平 (margin = LCV NW / debt)',
  },
  liquidation_soft_threshold: {
    group: 'liquidation',
    label: '警戒线',
    description: 'LCV margin 低于此值时给 UI 警告 banner，但不强平',
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
    description: 'bot_detection scheduler 多久扫一次',
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
    description: '卖出时按成交 gross 收取的手续费比例，0 表示免费。范围 [0, 0.2)。买入不收费',
    unit: '比例',
  },
  initial_balance: {
    group: 'economy',
    label: '新用户初始余额',
    description: '新用户首次 SSO 登录注册时获得的初始现金。仅影响新注册用户，不改动存量用户',
    unit: '金',
  },
}

const GROUP_ORDER: ConfigGroup[] = ['loan', 'liquidation', 'anti_bot', 'economy', 'general']

const GROUP_LABELS: Record<ConfigGroup, string> = {
  loan: '借款系统',
  liquidation: '强制平仓',
  anti_bot: '反脚本',
  economy: '经济参数',
  general: '其他',
}

/** 推断未注册 key 的 group。从前缀派生。 */
function inferGroup(key: string): ConfigGroup {
  if (key.startsWith('loan_')) return 'loan'
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
