<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  NAlert,
  NButton,
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
  logs = ref<unknown[]>([])
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
function toggle(id: number, e: Event) {
  selected.value = (e.target as HTMLInputElement).checked
    ? [...selected.value, id]
    : selected.value.filter((x) => x !== id)
}
onMounted(load)
</script>
<template>
  <div class="fx-bots">
    <div class="row">
      <h2>FX 机器人</h2>
      <NButton :loading="loading" :disabled="busy" @click="load">刷新</NButton>
    </div>
    <NAlert v-if="error" type="error">{{ error }}</NAlert>
    <p v-if="loading">正在读取…</p>
    <template v-if="ov">
      <section>
        <h3>运行设置</h3>
        <p>固定每5秒一个tick，保存后下一tick生效。普通FX交易总闸与交易对状态仍约束成交。</p>
        <label>FX机器人总开关<NSwitch v-model:value="cfg.enabled" :disabled="busy" /></label
        ><NSelect
          v-model:value="pairId"
          :options="ov.pairs.map((p) => ({ label: p.code, value: p.id }))"
          placeholder="选择交易对"
        />
        <div v-if="draft" class="grid">
          <label v-for="(s, k) in ov.pair_config_schema.properties" :key="k"
            >{{ labels[k] ?? s.title ?? k
            }}<NSwitch
              v-if="s.type === 'boolean'"
              :value="Boolean(draft[k])"
              @update:value="draft[k] = $event" /><NInputNumber
              v-else
              :value="num(draft[k])"
              :min="numeric(s).minimum"
              :max="numeric(s).maximum"
              @update:value="draft[k] = $event"
          /></label>
        </div>
        <NButton :disabled="busy" @click="act(() => api.saveConfig(cfg))">保存运行设置</NButton>
        <details>
          <summary>热度与运行状态</summary>
          <pre>{{ JSON.stringify(ov.engine, null, 2) }}</pre>
        </details>
        <div class="row">
          <label v-for="k in ['leaderboard_include_bots', 'wealth_stats_include_bots']" :key="k"
            >{{ k === 'leaderboard_include_bots' ? '排行榜包含机器人' : '财富统计包含机器人'
            }}<NSwitch
              :value="shared[k]"
              :disabled="busy"
              @update:value="act(() => pveApi.putConfig({ [k]: String($event) }))"
          /></label>
        </div>
        <p>两个统计开关影响全部机器人；排行榜缓存到期后更新。</p>
      </section>
      <section>
        <div class="row">
          <h3>创建账户</h3>
          <NButton @click="preset">填入冻结研究预设</NButton>
        </div>
        <p>预设可修改，新账户保持暂停。库存基准采用实际初始外币量。</p>
        <div class="grid">
          <label>每户现金<NInput v-model:value="cash" /></label
          ><label>每户外币<NInput v-model:value="foreign" /></label
          ><label v-for="s in ov.templates" :key="s.template"
            >{{ labels[s.template] }}人数<NInputNumber
              :value="counts[s.template]"
              :min="0"
              :max="50"
              :precision="0"
              @update:value="counts[s.template] = $event ?? 0" /></label
          ><label
            >命名风格<NSelect
              v-model:value="naming"
              :options="[
                { label: '低调', value: 'lowkey' },
                { label: 'NPC', value: 'npc' },
                { label: '短语', value: 'phrase' },
              ]"
          /></label>
        </div>
        <p>
          共{{ total }}户，现金发放{{ Number(cash) * total }}，外币发放{{
            Number(foreign) * total
          }}，按当前边际价估算总初始市值{{ estimated.toFixed(6) }}。
        </p>
        <NButton
          :disabled="busy || !total || !pairId || pair?.archived || pair?.status !== 'trading'"
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
      <section>
        <h3>账户池</h3>
        <div class="row">
          <NSelect
            v-model:value="filter"
            :options="options"
            clearable
            placeholder="全部策略"
          /><NSelect
            v-model:value="status"
            :options="['active', 'paused', 'dead', 'retired'].map((s) => ({ label: s, value: s }))"
            clearable
            placeholder="全部状态"
          />
        </div>
        <p>已选{{ targets.length }}户，批量操作须为同一交易对、同一策略。</p>
        <div class="row">
          <NButton :disabled="busy || !batchOK" @click="batch({ status: 'active' })"
            >启用所选{{ targets.length }}户</NButton
          ><NButton :disabled="busy || !batchOK" @click="batch({ status: 'paused' })"
            >暂停所选{{ targets.length }}户</NButton
          >
        </div>
        <div class="table">
          <NTable size="small"
            ><thead>
              <tr>
                <th>选择</th>
                <th>账户 / 策略</th>
                <th>状态</th>
                <th>现金</th>
                <th>外币</th>
                <th>钱包MTM</th>
                <th>钱包清算所得</th>
                <th>现金＋MTM / 现金＋清算</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="b in visible" :key="b.profile_id">
                <td>
                  <input
                    type="checkbox"
                    :checked="selected.includes(b.profile_id)"
                    :disabled="b.status === 'retired'"
                    @change="toggle(b.profile_id, $event)"
                  />
                </td>
                <td>{{ b.username }} / {{ labels[b.template] }}</td>
                <td>{{ b.status }}</td>
                <td>{{ b.cash }}</td>
                <td>{{ b.wallet }}</td>
                <td>{{ b.mtm }}</td>
                <td>{{ b.liquidation_cash }}</td>
                <td>
                  {{ (Number(b.cash) + Number(b.mtm)).toFixed(6) }} /
                  {{ (Number(b.cash) + Number(b.liquidation_cash)).toFixed(6) }}
                </td>
                <td><NButton size="small" @click="open(b)">管理</NButton></td>
              </tr>
              <tr v-if="!visible.length">
                <td colspan="9">暂无账户</td>
              </tr>
            </tbody></NTable
          >
        </div>
        <p>MTM为边际价估值，清算所得为整仓卖出报价；两项仅计钱包，总值各加一次现金。</p>
      </section>
      <section v-if="edit">
        <div class="row">
          <h3>{{ edit.username }}</h3>
          <NButton @click="edit = null">收起</NButton>
        </div>
        <p>
          状态{{ edit.status }}；实际初始外币基准{{
            edit.inventory_baseline_foreign
          }}，注资不改变基准。
        </p>
        <NButton
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
        <h4>策略参数</h4>
        <p>随机与库存参数可运行中修改；切换策略和修改短线参数须暂停且没有未结束轮次。</p>
        <NSelect
          :value="strategy"
          :options="options"
          :disabled="edit.status !== 'paused' || live"
          @update:value="change"
        />
        <p>{{ ov.templates.find((t) => t.template === strategy)?.description }}</p>
        <div class="grid">
          <label v-for="(s, k) in fields" :key="k"
            >{{ labels[k] ?? s.title ?? k
            }}<NInputNumber
              :value="num(params[k])"
              :min="numeric(s).minimum"
              :max="numeric(s).maximum"
              :disabled="s.readOnly || !canEdit"
              @update:value="params[k] = $event"
            /><small>{{ k }} {{ s.unit ?? '' }}</small></label
          >
        </div>
        <div class="row">
          <NButton
            :disabled="busy || !canEdit"
            @click="
              act(() => api.patch(edit!.profile_id, { template: strategy, params: writable() }))
            "
            >保存个人参数</NButton
          ><NButton
            :disabled="busy || !batchOK || !canEdit || targets.some((b) => b.template !== strategy)"
            @click="batch({ params: writable() })"
            >应用参数到所选{{ targets.length }}户</NButton
          >
        </div>
        <h4>现金注资</h4>
        <div class="row">
          <NInput v-model:value="fund" /><NButton
            :disabled="busy || edit.status === 'retired'"
            @click="act(() => api.fund(edit!.profile_id, fund))"
            >注资</NButton
          >
        </div>
        <h4>轮次与清仓</h4>
        <pre>{{ JSON.stringify(edit.strategy_state, null, 2) }}</pre>
        <p>
          每次仅执行一笔真实清仓；负向短线轮次买回，其他持仓卖出。受单笔上限限制，未清完须核对剩余仓位后再次手动提交。
        </p>
        <label
          >本笔最小输出 min_out（卖出为现金，买回为外币）<NInput v-model:value="minOut"
        /></label>
        <div class="row">
          <NButton
            :disabled="busy || edit.status !== 'paused'"
            @click="
              act(async () => {
                edit = (await api.liquidate(edit!.profile_id, minOut)).bot
              }, '本笔已成交，请核对剩余仓位；未清完可再次提交')
            "
            >执行一笔清仓</NButton
          ><NButton
            :disabled="busy || edit.status !== 'paused' || Number(edit.wallet) > 0 || live"
            @click="act(() => api.retire(edit!.profile_id), '空白账户已删除或账户已退休并回收现金')"
            >退休 / 删除空白账户</NButton
          >
        </div>
        <p>
          剩余外币{{ edit.wallet }}；未结束轮次{{
            live ? '有' : '无'
          }}。钱包与轮次清空后可退休，已有成交保留身份和流水。
        </p>
        <h4>决策日志</h4>
        <NButton
          :disabled="busy"
          @click="
            act(async () => {
              logs = (await api.log(edit!.profile_id)).log
            }, '已读取日志')
          "
          >读取日志</NButton
        >
        <p>内存日志重启后丢失。</p>
        <pre>{{ JSON.stringify(logs, null, 2) }}</pre>
      </section>
    </template>
  </div>
</template>
<style scoped>
section {
  border: 2px solid #000;
  background: #fff;
  padding: 16px;
  margin: 16px 0;
}
.row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  margin: 12px 0;
}
.row > .n-select,
.row > .n-input {
  max-width: 260px;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
  margin: 12px 0;
}
label {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
p,
small {
  color: #555;
  font-size: 13px;
  line-height: 1.6;
}
.table {
  overflow: auto;
}
pre {
  background: #f5f5f5;
  padding: 12px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 12px;
}
</style>
