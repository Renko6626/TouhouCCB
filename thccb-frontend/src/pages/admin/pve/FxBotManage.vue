<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  NAlert,
  NButton,
  NCheckbox,
  NModal,
  NSpin,
  NTag,
  NInput,
  NInputNumber,
  NSelect,
  NSwitch,
  NTable,
  useMessage,
} from 'naive-ui'
import {
  pveFxApi as api,
  type Bot,
  type Config,
  type Overview,
  type Patch,
  type Field,
} from '@/api/pveFx'
import { pveApi } from '@/api/pve'
const msg = useMessage(),
  ov = ref<Overview>(),
  cfg = ref<Config>({ enabled: false, pair_configs: {} }),
  bots = ref<Bot[]>([]),
  loading = ref(false),
  busy = ref(false),
  error = ref('')
const pairId = ref<number | null>(null),
  filter = ref<string | null>(null),
  status = ref<string | null>(null),
  selected = ref<number[]>([]),
  edit = ref<Bot | null>(null),
  strategy = ref(''),
  params = ref<Record<string, unknown>>({}),
  logs = ref<unknown[]>([]),
  logsLoading = ref(false),
  logsLoaded = ref(false)
const cash = ref('5000'),
  foreign = ref('1000'),
  naming = ref('lowkey'),
  counts = ref<Record<string, number>>({ random: 10, inventory: 5, episode: 5 }),
  fund = ref('100'),
  minOut = ref('0'),
  shared = ref<Record<string, boolean>>({})
const labels: Record<string, string> = {
  random: '随机',
  inventory: '库存调节',
  episode: '短线轮次',
  enabled: '启用交易对',
  activity_b: '热度权重',
  tau_seconds: '热度衰减（秒）',
  mean_arrivals_per_20: '每20户每tick平均到达次数',
  psi: '收益率尺度',
  max_pool_fraction: '单笔池比例上限',
  death_floor_gold: '死亡水位（现金）',
  inventory_baseline_foreign: '实际初始外币基准（只读）',
  order_sigma_foreign: '订单标准差（外币）',
  inventory_window_seconds: '库存窗口（秒）',
  inventory_position_cap: '净库存上限（外币）',
  inventory_sensitivity: '库存敏感度',
  inventory_adjustment: '库存调整比例',
  trend_window_seconds: '趋势窗口（秒）',
  mean_window_seconds: '均价窗口（秒）',
  trend_multiplier: '趋势倍率',
  entry_quantity: '每轮外币数量',
  take_profit: '止盈比例',
  stop_loss: '止损比例',
  trail_arm: '移动止盈启动比例',
  trail_drawdown: '移动止盈回撤比例',
  trend: '个人趋势系数',
  reversion: '个人回归系数',
  sigma: '个人噪声标准差',
}
const options = computed(
  () =>
    ov.value?.templates.map((t) => ({
      label: labels[t.template] ?? t.template,
      value: t.template,
    })) ?? [],
)
const visible = computed(() =>
  bots.value.filter(
    (b) =>
      (!pairId.value || b.pair_id === pairId.value) &&
      (!filter.value || b.template === filter.value) &&
      (!status.value || b.status === status.value),
  ),
)
const targets = computed(() => visible.value.filter((b) => selected.value.includes(b.profile_id)))
const batchOK = computed(
  () =>
    targets.value.length > 0 &&
    new Set(targets.value.map((b) => `${b.pair_id}/${b.template}`)).size === 1,
)
const pair = computed(() => ov.value?.pairs.find((p) => p.id === pairId.value)),
  draft = computed(() => (pairId.value ? cfg.value.pair_configs[String(pairId.value)] : undefined))
const fields = computed(
  () =>
    ov.value?.templates.find((t) => t.template === strategy.value)?.params_schema.properties ?? {},
)
const live = computed(() => Boolean(edit.value?.strategy_state.episode)),
  canEdit = computed(
    () =>
      edit.value &&
      edit.value.status !== 'retired' &&
      ((edit.value.template !== 'episode' && strategy.value === edit.value.template) ||
        (edit.value.status === 'paused' && !live.value)),
  )
const total = computed(() => Object.values(counts.value).reduce((a, b) => a + b, 0)),
  estimated = computed(
    () =>
      total.value * (Number(cash.value) + Number(foreign.value) * Number(pair.value?.price ?? 0)),
  )
function err(e: unknown) {
  const x = e as { data?: { detail?: unknown }; message?: string }
  return typeof x.data?.detail === 'string'
    ? x.data.detail
    : JSON.stringify(x.data?.detail ?? x.message ?? '操作失败')
}
function ensure() {
  if (pairId.value && ov.value && !cfg.value.pair_configs[String(pairId.value)])
    cfg.value.pair_configs[String(pairId.value)] = Object.fromEntries(
      Object.entries(ov.value.pair_config_schema.properties).map(([k, s]) => [k, s.default]),
    )
}
async function load() {
  loading.value = true
  error.value = ''
  try {
    const [o, c, b, s] = await Promise.all([
      api.overview(),
      api.config(),
      api.bots(),
      pveApi.getConfig(),
    ])
    ov.value = o
    cfg.value = c
    bots.value = b
    shared.value = {
      leaderboard_include_bots: s.leaderboard_include_bots?.value === 'true',
      wealth_stats_include_bots: s.wealth_stats_include_bots?.value === 'true',
    }
    pairId.value ??= o.pairs[0]?.id ?? null
    ensure()
    selected.value = []
    if (edit.value) edit.value = b.find((x) => x.profile_id === edit.value?.profile_id) ?? null
  } catch (e) {
    error.value = err(e)
  } finally {
    loading.value = false
  }
}
async function act(fn: () => Promise<unknown>, text = '已保存') {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await fn()
    msg.success(text)
    await load()
  } catch (e) {
    error.value = err(e)
  } finally {
    busy.value = false
  }
}
watch(pairId, () => {
  edit.value = null
  logs.value = []
  logsLoaded.value = false
  ensure()
  selected.value = []
})
watch([filter, status], () => (selected.value = []))
function preset() {
  if (!ov.value) return
  cash.value = ov.value.preset.initial_cash
  foreign.value = ov.value.preset.initial_foreign
  counts.value = Object.fromEntries(ov.value.preset.items.map((i) => [i.template, i.count]))
}
function open(b: Bot) {
  edit.value = b
  strategy.value = b.template
  params.value = { ...b.params }
  logs.value = []
  logsLoaded.value = false
}
function change(s: string) {
  strategy.value = s
  params.value = Object.fromEntries(Object.entries(fields.value).map(([k, v]) => [k, v.default]))
  if (edit.value) params.value.inventory_baseline_foreign = edit.value.inventory_baseline_foreign
}
function writable() {
  return Object.fromEntries(
    Object.entries(params.value).filter(([k]) => !fields.value[k]?.readOnly),
  )
}
function batch(p: Patch) {
  return act(() =>
    api.batch(
      targets.value.map((b) => b.profile_id),
      p,
    ),
  )
}
function numeric(s: Field) {
  return s.anyOf?.find((x) => x.type === 'number' || x.type === 'integer') ?? s
}
function num(v: unknown) {
  return v == null ? null : Number(v)
}
function toggle(id: number, checked: boolean) {
  selected.value = checked ? [...selected.value, id] : selected.value.filter((x) => x !== id)
}
const pairBots = computed(() => bots.value.filter((b) => b.pair_id === pairId.value))
const statusLabel = (s: string) =>
  ({ active: '在编', paused: '已暂停', dead: '已死亡', retired: '已退役' })[s] ?? s
const statusType = (s: string) =>
  s === 'active' ? 'success' : s === 'paused' ? 'warning' : s === 'dead' ? 'error' : 'default'
const format = (v: unknown) =>
  Number(v ?? 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 6 })
const creationHint = computed(() =>
  !pair.value
    ? '请选择交易对后创建账户。'
    : pair.value.archived
      ? '当前交易对已归档，无法创建账户。'
      : pair.value.status !== 'trading'
        ? '当前交易对未开放交易，无法创建账户。'
        : '',
)
const editPair = computed(
  () => ov.value?.pairs.find((p) => p.id === edit.value?.pair_id)?.code ?? '',
)
const logRows = computed(() =>
  logs.value.map((value) => {
    const row = value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
    const date = new Date(String(row.ts ?? ''))
    const extra = Object.fromEntries(
      Object.entries(row).filter(([k]) => !['ts', 'event', 'msg'].includes(k)),
    )
    return {
      time: Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false }),
      event: String(row.event ?? '记录'),
      message: String(row.msg ?? '查看详情'),
      detail: JSON.stringify(Object.keys(row).length ? extra : value, null, 2),
      hasDetail: !Object.keys(row).length || Object.keys(extra).length > 0,
    }
  }),
)
async function loadLogs() {
  if (!edit.value || logsLoading.value) return
  const id = edit.value.profile_id
  logsLoading.value = true
  try {
    const result = await api.log(id)
    if (edit.value?.profile_id === id) {
      logs.value = result.log
      logsLoaded.value = true
    }
  } catch (e) {
    msg.error(err(e))
  } finally {
    logsLoading.value = false
  }
}
onMounted(load)
</script>
<template>
  <div class="pve-manage">
    <NSpin :show="loading">
      <NAlert v-if="error" type="error" :title="error" class="error-alert" />
      <section class="panel stats-panel">
        <div class="stats-row">
          <div class="stat-item">
            <span class="stat-label">FX 引擎</span
            ><NSwitch v-model:value="cfg.enabled" :disabled="busy || !ov"
              ><template #checked>运行中</template><template #unchecked>已停止</template></NSwitch
            ><span class="param-doc">保存运行设置后生效</span>
          </div>
          <div class="stat-item">
            <span class="stat-label">当前交易对</span
            ><NSelect
              v-model:value="pairId"
              :options="ov?.pairs.map((p) => ({ label: p.code, value: p.id })) ?? []"
              placeholder="选择交易对"
              size="small"
              style="width: 180px"
            />
          </div>
          <div class="stat-item">
            <span class="stat-label">在编 / 暂停 / 死亡</span
            ><span class="stat-value"
              >{{ pairBots.filter((b) => b.status === 'active').length }} /
              {{ pairBots.filter((b) => b.status === 'paused').length }} /
              <span class="danger">{{
                pairBots.filter((b) => b.status === 'dead').length
              }}</span></span
            >
          </div>
          <div class="stat-item">
            <span class="stat-label">当前边际价</span
            ><span class="stat-value">{{ pair ? format(pair.price) : '—' }}</span>
          </div>
          <NButton size="small" :loading="loading" :disabled="busy" @click="load">刷新</NButton>
        </div>
      </section>
      <template v-if="ov">
        <section class="panel">
          <h2>策略图鉴</h2>
          <p class="hint">三类策略使用各自的参数模板，创建后可在账户管理中调整。</p>
          <div class="persona-grid">
            <details v-for="t in ov.templates" :key="t.template" class="persona-card">
              <summary class="persona-head">
                <span class="persona-title">{{ labels[t.template] ?? t.template }}</span
                ><code class="persona-code">{{ t.template }}</code>
              </summary>
              <p class="persona-summary">{{ t.description }}</p>
              <p class="persona-desc">
                {{
                  t.editable_when === 'paused_without_episode'
                    ? '须暂停且没有未结束轮次时修改参数。'
                    : t.editable_when === 'next_tick'
                      ? '参数可运行中修改，下一 tick 生效。'
                      : t.editable_when
                }}
              </p>
            </details>
          </div>
        </section>
        <section class="panel">
          <div class="section-head">
            <h2>运行设置</h2>
            <NButton
              size="small"
              type="primary"
              :loading="busy"
              @click="act(() => api.saveConfig(cfg))"
              >保存运行设置</NButton
            >
          </div>
          <p class="hint">
            固定每 5 秒一个 tick，保存后下一 tick 生效。普通 FX 交易总闸与交易对状态仍约束成交。
          </p>
          <p class="hint">
            当前交易对：{{ pair?.code ?? '未选择'
            }}<template v-if="pair"
              >，{{
                pair.archived ? '已归档' : pair.status === 'trading' ? '交易中' : '已暂停交易'
              }}</template
            >
          </p>
          <div v-if="draft" class="table">
            <NTable size="small" :single-line="false"
              ><thead>
                <tr>
                  <th>参数 / 说明</th>
                  <th class="config-value">值</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(s, k) in ov.pair_config_schema.properties" :key="k">
                  <td>
                    <div class="config-cell">
                      <span class="field-label">{{ labels[k] ?? s.title ?? k }}</span
                      ><code class="param-doc"
                        >{{ k }}<template v-if="s.unit">（{{ s.unit }}）</template></code
                      >
                    </div>
                  </td>
                  <td>
                    <NSwitch
                      v-if="s.type === 'boolean'"
                      :value="Boolean(draft[k])"
                      :disabled="busy"
                      @update:value="draft[k] = $event"
                    /><NInputNumber
                      v-else
                      size="small"
                      :value="num(draft[k])"
                      :min="numeric(s).minimum"
                      :max="numeric(s).maximum"
                      :disabled="busy"
                      @update:value="draft[k] = $event"
                    />
                  </td>
                </tr></tbody
            ></NTable>
          </div>
          <details class="technical-detail">
            <summary>热度与运行状态</summary>
            <pre>{{ JSON.stringify(ov.engine, null, 2) }}</pre>
          </details>
          <div class="row">
            <label
              v-for="k in ['leaderboard_include_bots', 'wealth_stats_include_bots']"
              :key="k"
              class="inline-field"
              ><span class="field-label">{{
                k === 'leaderboard_include_bots' ? '排行榜包含机器人' : '财富统计包含机器人'
              }}</span
              ><NSwitch
                :value="shared[k]"
                :disabled="busy"
                @update:value="act(() => pveApi.putConfig({ [k]: String($event) }))"
            /></label>
          </div>
          <p class="hint top-gap">两个统计开关影响全部机器人；排行榜缓存到期后更新。</p>
        </section>
        <section class="panel">
          <div class="section-head">
            <h2>创建账户</h2>
            <NButton size="small" :disabled="busy" @click="preset">填入冻结研究预设</NButton>
          </div>
          <p class="hint">预设可修改，新账户保持暂停。库存基准采用实际初始外币量。</p>
          <div class="row creation-fields">
            <label v-for="t in ov.templates" :key="t.template"
              ><span class="field-label">{{ labels[t.template] }}人数</span
              ><NInputNumber
                size="small"
                :value="counts[t.template]"
                :min="0"
                :max="50"
                :precision="0"
                @update:value="counts[t.template] = $event ?? 0" /></label
            ><label
              ><span class="field-label">每户现金</span
              ><NInput v-model:value="cash" size="small" /></label
            ><label
              ><span class="field-label">每户外币</span
              ><NInput v-model:value="foreign" size="small" /></label
            ><label
              ><span class="field-label">命名风格</span
              ><NSelect
                v-model:value="naming"
                size="small"
                :options="[
                  { label: '低调', value: 'lowkey' },
                  { label: 'NPC', value: 'npc' },
                  { label: '短语', value: 'phrase' },
                ]"
            /></label>
          </div>
          <p class="hint top-gap">
            共 {{ total }} 户，现金发放 {{ format(Number(cash) * total) }}，外币发放
            {{ format(Number(foreign) * total) }}，按当前边际价估算总初始市值
            {{ format(estimated) }}。
          </p>
          <p v-if="creationHint" class="hint danger">{{ creationHint }}</p>
          <NButton
            size="small"
            type="primary"
            :loading="busy"
            :disabled="busy || !total || !!creationHint"
            @click="
              act(
                () =>
                  api.generate({
                    pair_id: pairId,
                    items: Object.entries(counts)
                      .filter(([, c]) => c > 0)
                      .map(([template, count]) => ({ template, count })),
                    initial_cash: cash,
                    initial_foreign: foreign,
                    naming_style: naming,
                  }),
                '已创建暂停账户',
              )
            "
            >创建暂停账户</NButton
          >
        </section>
        <section class="panel">
          <h2>账户池（{{ pairBots.length }}）</h2>
          <div class="row">
            <NSelect
              v-model:value="filter"
              :options="options"
              clearable
              placeholder="全部策略"
              size="small"
              style="width: 180px"
            /><NSelect
              v-model:value="status"
              :options="
                ['active', 'paused', 'dead', 'retired'].map((s) => ({
                  label: statusLabel(s),
                  value: s,
                }))
              "
              clearable
              placeholder="全部状态"
              size="small"
              style="width: 150px"
            /><NButton
              size="small"
              :disabled="busy || !batchOK"
              @click="batch({ status: 'active' })"
              >启用所选 {{ targets.length }} 户</NButton
            ><NButton size="small" :disabled="busy || !batchOK" @click="batch({ status: 'paused' })"
              >暂停所选 {{ targets.length }} 户</NButton
            >
          </div>
          <p class="hint top-gap">
            已选 {{ targets.length }} 户，批量操作须为同一交易对、同一策略。
          </p>
          <div v-if="visible.length" class="table">
            <NTable size="small" :single-line="false"
              ><thead>
                <tr>
                  <th>选择</th>
                  <th>机器人</th>
                  <th>模板</th>
                  <th>状态</th>
                  <th class="num">现金</th>
                  <th class="num">外币</th>
                  <th class="num">钱包 MTM</th>
                  <th class="num">钱包清算所得</th>
                  <th class="num">现金＋MTM / 现金＋清算</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="b in visible" :key="b.profile_id">
                  <td>
                    <NCheckbox
                      :checked="selected.includes(b.profile_id)"
                      :disabled="b.status === 'retired'"
                      :aria-label="`选择 ${b.username}`"
                      @update:checked="toggle(b.profile_id, $event)"
                    />
                  </td>
                  <td>
                    <strong>{{ b.username }}</strong>
                  </td>
                  <td>
                    <NTag size="small">{{ labels[b.template] ?? b.template }}</NTag>
                  </td>
                  <td>
                    <NTag size="small" :type="statusType(b.status)">{{
                      statusLabel(b.status)
                    }}</NTag>
                  </td>
                  <td class="num mono">{{ format(b.cash) }}</td>
                  <td class="num mono">{{ format(b.wallet) }}</td>
                  <td class="num mono">{{ format(b.mtm) }}</td>
                  <td class="num mono">{{ format(b.liquidation_cash) }}</td>
                  <td class="num mono">
                    <strong>{{ format(Number(b.cash) + Number(b.mtm)) }}</strong> /
                    {{ format(Number(b.cash) + Number(b.liquidation_cash)) }}
                  </td>
                  <td><NButton size="tiny" :disabled="busy" @click="open(b)">管理</NButton></td>
                </tr>
              </tbody></NTable
            >
          </div>
          <div v-else class="empty-hint">
            {{
              pairBots.length
                ? '没有符合筛选条件的账户'
                : '账户池为空，可用上面的「创建账户」建立编制'
            }}
          </div>
          <p class="hint top-gap">
            MTM 为边际价估值，清算所得为整仓卖出报价；两项仅计钱包，总值各加一次现金。
          </p>
        </section>
      </template>
    </NSpin>
    <NModal
      :show="edit !== null"
      preset="card"
      :title="`账户管理：${edit?.username ?? ''}（${editPair}）`"
      class="manage-modal"
      style="width: 760px; max-width: calc(100vw - 32px)"
      @update:show="
        (v) => {
          if (!v) edit = null
        }
      "
    >
      <div v-if="edit && ov" class="modal-form">
        <NAlert v-if="error" type="error">{{ error }}</NAlert>
        <div class="section-head">
          <div class="row">
            <NTag size="small" :type="statusType(edit.status)">{{ statusLabel(edit.status) }}</NTag
            ><span class="hint">现金 {{ format(edit.cash) }}，外币 {{ format(edit.wallet) }}</span>
          </div>
          <NButton
            size="small"
            :disabled="busy || edit.status === 'retired'"
            @click="
              act(() =>
                api.patch(edit!.profile_id, {
                  status: edit!.status === 'active' ? 'paused' : 'active',
                }),
              )
            "
            >{{ edit.status === 'active' ? '暂停' : '启用' }}</NButton
          >
        </div>
        <section class="modal-section">
          <h3>策略参数</h3>
          <p class="hint">
            随机与库存参数可运行中修改；切换策略和修改短线参数须暂停且没有未结束轮次。
          </p>
          <div class="row">
            <span class="field-label">策略模板</span
            ><NSelect
              :value="strategy"
              :options="options"
              size="small"
              style="width: 210px"
              :disabled="busy || edit.status !== 'paused' || live"
              @update:value="change"
            />
          </div>
          <p class="hint persona-pick top-gap">
            {{ ov.templates.find((t) => t.template === strategy)?.description }}
          </p>
          <p class="hint">
            实际初始外币基准（只读）：{{ edit.inventory_baseline_foreign }}，注资不改变基准。
          </p>
          <div class="param-form">
            <div v-for="(s, k) in fields" :key="k" class="param-row">
              <div class="param-info">
                <span class="field-label">{{ labels[k] ?? s.title ?? k }}</span
                ><code class="param-doc"
                  >{{ k }}<template v-if="s.unit">（{{ s.unit }}）</template
                  ><template v-if="s.readOnly">，只读</template></code
                >
              </div>
              <NInputNumber
                size="small"
                class="param-input"
                :value="num(params[k])"
                :min="numeric(s).minimum"
                :max="numeric(s).maximum"
                :disabled="busy || s.readOnly || !canEdit"
                @update:value="params[k] = $event"
              />
            </div>
          </div>
          <div class="row right">
            <NButton
              size="small"
              :disabled="
                busy || !batchOK || !canEdit || targets.some((b) => b.template !== strategy)
              "
              @click="batch({ params: writable() })"
              >应用参数到所选 {{ targets.length }} 户</NButton
            ><NButton
              size="small"
              type="primary"
              :loading="busy"
              :disabled="busy || !canEdit"
              @click="
                act(() => api.patch(edit!.profile_id, { template: strategy, params: writable() }))
              "
              >保存个人参数</NButton
            >
          </div>
        </section>
        <section class="modal-section">
          <h3>现金注资</h3>
          <div class="row">
            <label class="inline-field"
              ><span class="field-label">金额</span
              ><NInput v-model:value="fund" size="small" style="width: 180px" /></label
            ><NButton
              size="small"
              :disabled="busy || edit.status === 'retired'"
              @click="act(() => api.fund(edit!.profile_id, fund))"
              >注资</NButton
            >
          </div>
        </section>
        <section class="modal-section">
          <h3>轮次与清仓</h3>
          <details class="technical-detail">
            <summary>查看轮次状态</summary>
            <pre>{{ JSON.stringify(edit.strategy_state, null, 2) }}</pre>
          </details>
          <p class="hint">
            每次仅执行一笔真实清仓；负向短线轮次买回，其他持仓卖出。受单笔上限限制，未清完须核对剩余仓位后再次手动提交。
          </p>
          <label class="minout-field"
            ><span class="field-label">本笔最小输出 min_out</span
            ><NInput v-model:value="minOut" size="small" style="width: 180px" /><span
              class="param-doc"
              >卖出为现金，买回为外币</span
            ></label
          >
          <div class="row">
            <NButton
              size="small"
              :disabled="busy || edit.status !== 'paused'"
              @click="
                act(async () => {
                  edit = (await api.liquidate(edit!.profile_id, minOut)).bot
                }, '本笔已成交，请核对剩余仓位；未清完可再次提交')
              "
              >执行一笔清仓</NButton
            ><NButton
              size="small"
              type="error"
              ghost
              :disabled="busy || edit.status !== 'paused' || Number(edit.wallet) > 0 || live"
              @click="
                act(() => api.retire(edit!.profile_id), '空白账户已删除或账户已退休并回收现金')
              "
              >退休 / 删除空白账户</NButton
            >
          </div>
          <p class="hint top-gap">
            剩余外币 {{ edit.wallet }}；未结束轮次{{
              live ? '有' : '无'
            }}。钱包与轮次清空后可退休，已有成交保留身份和流水。
          </p>
        </section>
        <section class="modal-section">
          <div class="section-head">
            <h3>决策日志</h3>
            <NButton size="small" :loading="logsLoading" @click="loadLogs">读取日志</NButton>
          </div>
          <p class="hint">内存日志重启后丢失。</p>
          <div v-if="logRows.length" class="table">
            <NTable size="small" :single-line="false"
              ><thead>
                <tr>
                  <th>时间</th>
                  <th>事件</th>
                  <th>内容</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, i) in logRows" :key="i">
                  <td class="ts">{{ row.time }}</td>
                  <td>
                    <NTag
                      size="small"
                      :type="
                        row.event === 'error'
                          ? 'error'
                          : row.event === 'trade'
                            ? 'success'
                            : 'default'
                      "
                      >{{ row.event }}</NTag
                    >
                  </td>
                  <td class="log-content">
                    {{ row.message }}
                    <details v-if="row.hasDetail">
                      <summary>详情</summary>
                      <pre>{{ row.detail }}</pre>
                    </details>
                  </td>
                </tr>
              </tbody></NTable
            >
          </div>
          <div v-else class="empty-hint">
            {{ logsLoaded ? '暂无决策记录' : '点击「读取日志」查看决策记录' }}
          </div>
        </section>
      </div>
    </NModal>
  </div>
</template>
<style scoped>
.pve-manage {
  padding: 16px;
}
.panel {
  margin-bottom: 16px;
  border: 2px solid #000;
  padding: 16px;
  background: #fff;
}
.stats-panel {
  background: #fafafa;
}
.stats-row {
  display: flex;
  align-items: center;
  gap: 24px;
  flex-wrap: wrap;
}
.stat-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.stat-label {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: #666;
}
.stat-value {
  font-size: 24px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
}
.danger {
  color: #cc0000;
}
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
  flex-wrap: wrap;
  gap: 8px;
}
h2 {
  margin: 0 0 8px 0;
}
.user-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.user-id {
  font-family: monospace;
  font-size: 10px;
  color: #888;
}
.action-cell {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}
.ts {
  font-size: 12px;
  color: #555;
  white-space: nowrap;
}
.mono {
  font-family: monospace;
  font-size: 12px;
}
.num {
  text-align: right;
}
.small {
  font-size: 11px;
  color: #888;
}
.empty-hint {
  padding: 24px 12px;
  text-align: center;
  color: #888;
  border: 1.5px dashed #ccc;
  font-size: 13px;
}
.row {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  margin-top: 8px;
}
.row.right {
  justify-content: flex-end;
}
.field-label {
  font-size: 12px;
  font-weight: 700;
}
.modal-form {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.hint {
  font-size: 12px;
  color: #666;
  margin: 0 0 6px 0;
}
.persona-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 10px;
  margin-top: 10px;
}
.persona-card {
  border: 2px solid #000;
  padding: 10px 12px;
  background: #fafafa;
}
.persona-card[open] {
  background: #fff;
}
.persona-head {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  list-style: none;
  user-select: none;
}
.persona-head::-webkit-details-marker {
  display: none;
}
.persona-title {
  font-weight: 800;
  font-size: 14px;
}
.persona-code {
  font-family: monospace;
  font-size: 11px;
  color: #888;
}
.persona-summary {
  margin: 8px 0 0 0;
  font-size: 12px;
  font-weight: 700;
}
.persona-desc {
  margin: 6px 0 0 0;
  font-size: 12px;
  color: #444;
  line-height: 1.7;
}
.persona-summary + .persona-desc {
  border-top: 1.5px dashed #ccc;
  padding-top: 6px;
}
.persona-pick {
  border-left: 3px solid #000;
  padding-left: 8px;
  color: #000;
}
.param-form {
  max-height: 46vh;
  overflow-y: auto;
  border: 1.5px solid #ddd;
}
.param-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 10px;
  border-bottom: 1px solid #eee;
}
.param-row:last-child {
  border-bottom: none;
}
.param-info,
.config-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.param-key {
  font-family: monospace;
  font-size: 12px;
  font-weight: 700;
}
.param-doc {
  font-size: 11px;
  color: #777;
  line-height: 1.5;
}
.param-input {
  flex-shrink: 0;
}
.spacer {
  flex: 1;
}

.error-alert {
  margin-bottom: 16px;
}
.table {
  display: block;
  width: 100%;
  overflow-x: auto;
}
.table :deep(table) {
  width: 100%;
}
.table :deep(th),
.table :deep(td.num) {
  white-space: nowrap;
}
.config-value {
  width: 200px;
}
.creation-fields label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: 150px;
}
.inline-field {
  display: flex;
  align-items: center;
  gap: 8px;
}
.top-gap {
  margin-top: 10px;
}
.technical-detail {
  font-size: 12px;
  color: #666;
  margin: 10px 0;
}
.technical-detail summary,
.log-content summary {
  cursor: pointer;
}
pre {
  padding: 10px;
  background: #f5f5f5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 12px;
  max-height: 240px;
  overflow-y: auto;
}
.modal-form {
  max-height: 76vh;
  overflow-y: auto;
  padding-right: 4px;
}
.modal-section {
  border-top: 1px solid #ddd;
  padding-top: 12px;
}
h3 {
  margin: 0 0 8px;
  font-size: 16px;
}
.param-input {
  width: 170px;
}
.minout-field {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.log-content {
  min-width: 180px;
  overflow-wrap: anywhere;
}
@media (max-width: 600px) {
  .pve-manage {
    padding: 8px;
  }
  .panel {
    padding: 12px;
  }
  .stats-row {
    gap: 16px;
  }
  .stat-value {
    font-size: 20px;
  }
  .persona-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .param-row {
    flex-wrap: wrap;
    gap: 6px;
  }
  .param-info {
    flex: 1 1 100%;
  }
  .param-input {
    width: 100%;
  }
  .config-value {
    width: 140px;
  }
  .inline-field {
    flex-wrap: wrap;
  }
  .modal-form {
    max-height: 78vh;
  }
}
</style>
