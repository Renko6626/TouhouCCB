<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue'
import {
  NTable, NInput, NButton, NSpin, NAlert, useMessage, useDialog,
  NInputNumber, NDivider, NSwitch, NTooltip, NTag,
} from 'naive-ui'
import { adminSiteConfigApi, type SiteConfigItem } from '@/api/loan'
import { compareConfigKeys, getConfigMeta, groupLabel, groupOrder, type ConfigGroup } from '@/utils/configMeta'

// ─── 杠杆预设套餐 ─────────────────────────────────────────────────────────
// 一次性 update 6 个配套 site_config keys，避免 admin 手动逐项调时漏配。
// 数学约束（已验证不会"借满即死"）：
//   - 1/k > hard_threshold (借满 margin > 触发线)
//   - k × hard < 0.5 (留 LMSR 买入滑点 buffer)
//   - target > hard (partial 收敛有空间)
//   - emergency < hard × 0.5 (紧急救援线足够低)

type PresetKey = 'conservative' | 'moderate' | 'aggressive' | 'extreme' | 'fx10x'

interface Preset {
  name: string
  label: string
  warn: string
  values: Record<string, string>  // site_config key → value
}

const PRESETS: Record<PresetKey, Preset> = {
  conservative: {
    name: 'conservative',
    label: '🟢 保守 (1x)',
    warn: '借满需 LCV 跌 80% 才触发，最稳。适合默认 / 新手期。',
    values: {
      loan_leverage_k: '1.0',
      loan_daily_rate: '0.01',
      liquidation_hard_threshold: '0.2',
      liquidation_target_margin: '0.3',
      liquidation_emergency_threshold: '0.05',
      liquidation_partial_pct: '0.1',
    },
  },
  moderate: {
    name: 'moderate',
    label: '🟡 中等 (2x)',
    warn: '借满需 LCV 跌 55% 触发，戏剧性 + 安全度平衡。推荐活动日开局。',
    values: {
      loan_leverage_k: '2.0',
      loan_daily_rate: '0.01',
      liquidation_hard_threshold: '0.2',
      liquidation_target_margin: '0.3',
      liquidation_emergency_threshold: '0.05',
      liquidation_partial_pct: '0.05',
    },
  },
  aggressive: {
    name: 'aggressive',
    label: '🟠 激进 (3x)',
    warn: '借满需 LCV 跌 30% 触发，强 cascade 风险。建议同步降 daily_rate 到 0.008。',
    values: {
      loan_leverage_k: '3.0',
      loan_daily_rate: '0.008',
      liquidation_hard_threshold: '0.15',
      liquidation_target_margin: '0.25',
      liquidation_emergency_threshold: '0.03',
      liquidation_partial_pct: '0.04',
    },
  },
  extreme: {
    name: 'extreme',
    label: '🔴 极限 (5x)',
    warn: '借满需 LCV 跌 12% 触发，极敏感 + 死户激增。需配套做 admin 豁免 endpoint！',
    values: {
      loan_leverage_k: '5.0',
      loan_daily_rate: '0.005',
      liquidation_hard_threshold: '0.1',
      liquidation_target_margin: '0.15',
      liquidation_emergency_threshold: '0.02',
      liquidation_partial_pct: '0.03',
    },
  },
  fx10x: {
    name: 'fx10x',
    label: 'FX 名义 10x',
    warn: '名义 10x 对应最多借入净值的 9 倍；维持率 0.04，初始率约 0.111。价格小幅波动即可触发强平，请先确认 FX 抵押与统一信贷已启用。',
    values: {
      loan_leverage_k: '9.0',
      loan_daily_rate: '0.005',
      liquidation_hard_threshold: '0.04',
      liquidation_target_margin: '0.08',
      liquidation_emergency_threshold: '0.01',
      liquidation_partial_pct: '0.02',
    },
  },
}

const PRESET_KEYS_TRACKED = Object.keys(PRESETS.conservative.values)

const configs = ref<SiteConfigItem[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const drafts = ref<Record<string, string>>({})
const msg = useMessage()
const dialog = useDialog()
const unifiedCreditEnabled = computed(() => configs.value.some(
  c => c.key === 'unified_credit_enabled' && c.value.toLowerCase() === 'true',
))

function presetLabel(preset: Preset): string {
  return unifiedCreditEnabled.value
    ? `名义 ${Number(preset.values.loan_leverage_k) + 1}x${preset.name === 'fx10x' ? '（FX）' : ''}`
    : preset.label
}

function presetValues(preset: Preset): Record<string, string> {
  if (!unifiedCreditEnabled.value) return preset.values
  return {
    credit_maintenance_ratio: preset.values.liquidation_hard_threshold!,
    credit_leverage: String(Number(preset.values.loan_leverage_k) + 1),
  }
}

// 已保存套餐检测：各项配置匹配 PRESETS[name].values 则视为该套餐
// 否则为 null（自定义）
const currentPresetKey = computed<PresetKey | null>(() => {
  if (configs.value.length === 0) return null
  for (const [k, p] of Object.entries(PRESETS) as [PresetKey, Preset][]) {
    const allMatch = Object.entries(presetValues(p)).every(([key, val]) => {
      const cfg = configs.value.find(c => c.key === key)
      // 数值字符串比较：归一化避免 "0.10" vs "0.1" 误判
      return cfg && normalizeDecimal(cfg.value) === normalizeDecimal(val)
    })
    if (allMatch) return k
  }
  return null
})

function normalizeDecimal(v: string): string {
  // 字符串 → number → 字符串，去掉尾零差异
  const n = Number(v)
  if (Number.isNaN(n)) return v
  return String(n)
}

// 套餐应用：sequential PUT，不动后端
const applying = ref(false)

async function applyPreset(presetKey: PresetKey) {
  const preset = PRESETS[presetKey]
  const values = presetValues(preset)

  // 算出真正会改的字段（避免 0 改动也弹确认）
  const diffs = Object.entries(values).map(([key, newVal]) => {
    const cur = configs.value.find(c => c.key === key)
    return {
      key,
      label: getConfigMeta(key).label,
      oldVal: cur?.value ?? '?',
      newVal,
      changed: !cur || normalizeDecimal(cur.value) !== normalizeDecimal(newVal),
    }
  })

  const changedCount = diffs.filter(d => d.changed).length
  if (changedCount === 0) {
    msg.info(`当前已经是 "${presetLabel(preset)}" 套餐，无改动`)
    return
  }

  // 弹 NDialog 显示 diff 表 + 警告 + 确认按钮
  dialog.warning({
    title: `应用套餐: ${presetLabel(preset)}`,
    content: () => buildDiffContent(diffs, unifiedCreditEnabled.value
      ? '本套餐调整名义杠杆和维持率，保存后需重启后端生效。日利率请单独在停写维护时结清旧率利息后调整。'
      : preset.warn),
    positiveText: `确认应用 (${changedCount} 项改动)`,
    negativeText: '取消',
    onPositiveClick: async () => {
      await doApply(presetKey, preset, values)
    },
  })
}

async function doApply(presetKey: PresetKey, preset: Preset, values: Record<string, string>) {
  applying.value = true
  let succeeded = 0
  let failed = 0
  const failedKeys: string[] = []
  const entries = Object.entries(values)
  if (unifiedCreditEnabled.value) {
    const currentLeverage = Number(configs.value.find(c => c.key === 'credit_leverage')?.value)
    const nextLeverage = Number(values.credit_leverage)
    if (nextLeverage < currentLeverage) {
      entries.sort(([a], [b]) => Number(b === 'credit_leverage') - Number(a === 'credit_leverage'))
    }
  }
  for (const [key, value] of entries) {
    try {
      await adminSiteConfigApi.update(key, value)
      succeeded++
    } catch (e: unknown) {
      failed++
      failedKeys.push(key)
      const detail = (e as { data?: { detail?: string }; message?: string })?.data?.detail
        ?? (e as { message?: string })?.message
        ?? '未知错误'
      console.error(`apply preset ${presetKey} - ${key} failed:`, detail)
      // 前置门槛更新失败后停止，避免继续应用依赖它的杠杆值。
      break
    }
  }
  applying.value = false

  if (failed === 0) {
    msg.success(`套餐 "${presetLabel(preset)}" 已保存 (${succeeded} 项)${unifiedCreditEnabled.value ? '，重启后端后生效' : ''}`)
  } else {
    msg.warning(`应用已停止：${succeeded}/${entries.length} 成功，失败 keys: ${failedKeys.join(', ')}，其余未应用`)
  }
  await load()
}

// 用 vnode 在 NDialog content slot 渲染 diff 表 + 警告
function buildDiffContent(
  diffs: Array<{ key: string; label: string; oldVal: string; newVal: string; changed: boolean }>,
  warn: string,
) {
  return h('div', { style: 'font-size: 13px;' }, [
    h('table', { style: 'width: 100%; border-collapse: collapse; margin-bottom: 12px;' }, [
      h('thead', null, h('tr', null, [
        h('th', { style: 'text-align:left; border-bottom: 2px solid #000; padding: 4px 8px;' }, '参数'),
        h('th', { style: 'text-align:left; border-bottom: 2px solid #000; padding: 4px 8px;' }, '现值'),
        h('th', { style: 'text-align:left; border-bottom: 2px solid #000; padding: 4px 8px;' }, '新值'),
      ])),
      h('tbody', null, diffs.map(d =>
        h('tr', null, [
          h('td', {
            style: `padding: 4px 8px; border-bottom: 1px solid #eee; ${d.changed ? 'font-weight: 600;' : 'color: #999;'}`,
          }, [
            d.label,
            h('br'),
            h('code', { style: 'font-size: 10px; color: #999;' }, d.key),
          ]),
          h('td', {
            style: `padding: 4px 8px; border-bottom: 1px solid #eee; font-variant-numeric: tabular-nums; ${d.changed ? 'color: #888;' : ''}`,
          }, d.oldVal),
          h('td', {
            style: `padding: 4px 8px; border-bottom: 1px solid #eee; font-variant-numeric: tabular-nums; ${d.changed ? 'color: #cc0000; font-weight: 700;' : 'color: #999;'}`,
          }, d.changed ? `→ ${d.newVal}` : '不变'),
        ]),
      )),
    ]),
    h('div', {
      style: 'padding: 10px 12px; background: #fff5e5; border: 2px solid #aa6600; color: #553300; font-size: 12px; line-height: 1.5;',
    }, [
      h('strong', null, '⚠️ 提醒：'),
      ' ' + warn,
    ]),
  ])
}

async function load() {
  loading.value = true
  error.value = null
  try {
    configs.value = await adminSiteConfigApi.list()
    drafts.value = Object.fromEntries(configs.value.map(c => [c.key, c.value]))
  } catch (e: any) {
    error.value = e?.message ?? '加载失败'
  } finally {
    loading.value = false
  }
}

async function save(key: string) {
  try {
    await adminSiteConfigApi.update(key, drafts.value[key] ?? '')
    msg.success(`${key} 已更新`)
    await load()
  } catch (e: any) {
    msg.error(e?.message ?? '更新失败')
  }
}

// bool 类型直接 toggle 立即保存（不走 draft），UX 更顺
async function toggleBool(c: SiteConfigItem) {
  const next = c.value === 'true' ? 'false' : 'true'
  drafts.value[c.key] = next
  await save(c.key)
}

// 把 configs 按 group 分组
const configsByGroup = computed<Record<ConfigGroup, SiteConfigItem[]>>(() => {
  const groups: Record<ConfigGroup, SiteConfigItem[]> = {
    display: [], fx: [], loan: [], liquidation: [], anti_bot: [], economy: [], general: [],
  }
  for (const c of configs.value) {
    // FX 开关统一在 FX 管理中操作；站点配置接口不支持保存 fx_enabled。
    if (c.key === 'fx_enabled' || c.key === 'fx_short_enabled') continue
    const meta = getConfigMeta(c.key)
    groups[meta.group].push(c)
  }
  // 常用开关与相关参数相邻，未登记的配置保留在各组末尾。
  for (const group of groupOrder()) {
    groups[group].sort((a, b) => compareConfigKeys(a.key, b.key))
  }
  return groups
})

function shouldChangeFromDraft(c: SiteConfigItem): boolean {
  return drafts.value[c.key] !== c.value
}

onMounted(load)
</script>

<template>
  <div class="admin-site-config">
    <NSpin :show="loading">
      <NAlert v-if="error" type="error" :title="error" />

      <!-- ── 杠杆预设套餐 ────────────────────────────────────────────── -->
      <section class="panel preset-panel">
        <div class="preset-head">
          <h2>杠杆预设套餐</h2>
          <span class="preset-sub">{{ unifiedCreditEnabled ? '统一信贷：杠杆与维持率' : '旧模式：借款系数、强平门槛与日利率等 6 项参数' }}</span>
        </div>

        <div class="preset-current">
          <span class="preset-label">已保存配置：</span>
          <NTag v-if="currentPresetKey" type="success" size="small">
            {{ presetLabel(PRESETS[currentPresetKey]) }}
          </NTag>
          <NTag v-else type="warning" size="small">自定义（不匹配任何预设）</NTag>
        </div>

        <div class="preset-buttons">
          <NButton
            v-for="(p, key) in PRESETS"
            :key="key"
            :type="currentPresetKey === key ? 'primary' : 'default'"
            :loading="applying"
            :disabled="applying"
            size="medium"
            @click="applyPreset(key as PresetKey)"
          >
            {{ presetLabel(p) }}
          </NButton>
        </div>

        <details class="preset-details">
          <summary>查看 {{ Object.keys(PRESETS).length }} 个套餐完整数值</summary>
          <NTable :bordered="true" :single-line="false" size="small">
            <thead>
              <tr>
                <th>参数</th>
                <th v-for="(p, key) in PRESETS" :key="key">{{ presetLabel(p) }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="key in (unifiedCreditEnabled ? ['credit_leverage', 'credit_maintenance_ratio'] : PRESET_KEYS_TRACKED)" :key="key">
                <td>
                  <span class="preset-row-label">{{ getConfigMeta(key).label }}</span>
                  <code class="preset-row-key">{{ key }}</code>
                </td>
                <td v-for="(p, pk) in PRESETS" :key="pk" class="preset-cell">
                  {{ presetValues(p)[key] }}
                </td>
              </tr>
            </tbody>
          </NTable>
        </details>
      </section>

      <NDivider />

      <section class="panel">
        <h2>站点配置</h2>
        <p class="config-description">
          FX 交易总闸和开空闸请前往 <RouterLink to="/admin/fx">FX 管理</RouterLink> 设置。
          PvE 参数请前往 <RouterLink to="/admin/pve">PvE 管理</RouterLink> 设置。
        </p>
        <p class="config-description">
          统一信贷模式、名义杠杆、强平维持率和风险检查重试次数修改后需重启后端生效。
          冻结新增风险可即时生效。
        </p>

        <div
          v-for="group in groupOrder()"
          :key="group"
          class="config-group"
        >
          <template v-if="configsByGroup[group].length > 0">
            <h3 class="group-title">{{ groupLabel(group) }} <span class="group-count">({{ configsByGroup[group].length }})</span></h3>

            <NTable :bordered="true" :single-line="false" size="small">
              <thead>
                <tr>
                  <th class="col-key">配置项</th>
                  <th class="col-value">值</th>
                  <th class="col-ts">更新时间</th>
                  <th class="col-action">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="c in configsByGroup[group]" :key="c.key">
                  <td>
                    <div class="config-label-cell">
                      <span class="config-label">{{ getConfigMeta(c.key).label }}</span>
                      <NTooltip v-if="getConfigMeta(c.key).description" trigger="hover" :style="{ maxWidth: '320px' }">
                        <template #trigger>
                          <span class="config-help">ⓘ</span>
                        </template>
                        {{ getConfigMeta(c.key).description }}
                      </NTooltip>
                      <code class="config-key-mono">{{ c.key }}</code>
                    </div>
                    <p v-if="c.key.startsWith('fx_') || c.key.startsWith('credit_') || c.key === 'unified_credit_enabled' || c.key === 'homepage_fx_enabled'" class="config-description">
                      {{ getConfigMeta(c.key).description }}
                    </p>
                  </td>
                  <td>
                    <!-- bool 用 Switch 立即保存 -->
                    <template v-if="c.value_type === 'bool'">
                      <NSwitch
                        :value="c.value === 'true'"
                        @update:value="toggleBool(c)"
                        size="small"
                      />
                      <span class="value-text-bool">{{ c.value === 'true' ? '启用' : '关闭' }}</span>
                    </template>

                    <!-- int / decimal 用 NInputNumber，按需选 step -->
                    <template v-else-if="c.value_type === 'int' || c.value_type === 'decimal'">
                      <NInputNumber
                        :value="Number(drafts[c.key])"
                        :disabled="c.key === 'loan_leverage_k' && unifiedCreditEnabled"
                        @update:value="(v) => drafts[c.key] = v === null ? '' : String(v)"
                        size="small"
                        :precision="c.value_type === 'int' ? 0 : undefined"
                        style="width: 160px"
                      />
                      <span v-if="getConfigMeta(c.key).unit" class="config-unit">{{ getConfigMeta(c.key).unit }}</span>
                    </template>

                    <!-- string 默认 -->
                    <template v-else>
                      <NInput v-model:value="drafts[c.key]" size="small" />
                    </template>
                  </td>
                  <td class="ts">{{ new Date(c.updated_at).toLocaleString() }}</td>
                  <td>
                    <!-- bool 已用 toggle 立即保存，不需要按钮 -->
                    <NButton
                      v-if="c.value_type !== 'bool'"
                      size="small"
                      type="primary"
                      :disabled="!shouldChangeFromDraft(c) || (c.key === 'loan_leverage_k' && unifiedCreditEnabled)"
                      @click="save(c.key)"
                    >保存</NButton>
                    <span v-else class="ts">—</span>
                  </td>
                </tr>
              </tbody>
            </NTable>
          </template>
        </div>
      </section>

    </NSpin>
  </div>
</template>

<style scoped>
.admin-site-config {
  padding: 16px;
}
.panel {
  margin-bottom: 16px;
  border: 2px solid #000;
  padding: 16px;
  background: #fff;
}
.row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin: 8px 0;
  flex-wrap: wrap;
}
.lbl {
  font-weight: 600;
}
.ts {
  font-size: 12px;
  color: #666;
  white-space: nowrap;
}
/* ── 配置项分组 ── */
.config-group {
  margin-bottom: 24px;
}
.config-group:last-child {
  margin-bottom: 0;
}
.group-title {
  font-size: 14px;
  font-weight: 700;
  margin: 16px 0 8px;
  padding: 6px 10px;
  background: #f5f5f5;
  border-left: 4px solid #000;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.group-count {
  font-size: 12px;
  color: #888;
  font-weight: 400;
  margin-left: 6px;
  text-transform: none;
  letter-spacing: 0;
}
.config-label-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.config-label {
  font-weight: 600;
  color: #000;
}
.config-description {
  margin: 4px 0 0;
  max-width: 48rem;
  color: #555;
  font-size: 12px;
  line-height: 1.45;
}
.config-help {
  color: #888;
  cursor: help;
  font-size: 14px;
  user-select: none;
}
.config-key-mono {
  font-family: monospace;
  font-size: 10px;
  color: #999;
  margin-left: 4px;
}
.config-unit {
  margin-left: 6px;
  font-size: 12px;
  color: #666;
}
.value-text-bool {
  margin-left: 8px;
  font-size: 12px;
  color: #666;
}
/* 列宽 */
.col-key { width: 30%; }
.col-value { width: 35%; }
.col-ts { width: 20%; }
.col-action { width: 15%; }
h2, h3 {
  margin: 0 0 8px 0;
}
code {
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 13px;
}
.user-snapshot {
  margin: 8px 0;
  padding: 8px 12px;
  background: #f5f5f5;
  border: 1px solid #ccc;
  font-size: 13px;
}
.hint {
  font-size: 12px;
  color: #666;
  margin-top: 8px;
}

/* ── 杠杆预设套餐 panel ──────────────────────────────────────── */
.preset-panel {
  background: #fafafa;
  border-color: #000;
}
.preset-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 6px;
  border-bottom: 1px solid #ccc;
  padding-bottom: 8px;
  margin-bottom: 12px;
}
.preset-sub {
  font-size: 11px;
  color: #888;
  letter-spacing: 0.02em;
}
.preset-current {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  font-size: 13px;
}
.preset-label {
  font-weight: 600;
  color: #000;
}
.preset-buttons {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.preset-details {
  margin-top: 12px;
  font-size: 12px;
}
.preset-details summary {
  cursor: pointer;
  user-select: none;
  font-weight: 600;
  padding: 6px 10px;
  background: #f0f0f0;
  border: 1.5px solid #000;
  display: inline-block;
  margin-bottom: 8px;
}
.preset-details summary:hover {
  background: #e8e8e8;
}
.preset-row-label {
  font-weight: 600;
}
.preset-row-key {
  display: block;
  font-family: monospace;
  font-size: 10px;
  color: #999;
  margin-top: 2px;
}
.preset-cell {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
  text-align: center;
}
</style>
