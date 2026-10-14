<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref, watch, type ComputedRef } from 'vue'
import { useDialog } from 'naive-ui'
import { compareFxAmounts, formatFxAmount as amount, formatFxPrice as price, fxAdminApi, mapFxError } from '@/api/fx'
import type { FxAdminOverview, FxLiquidityAction, FxLiquidityHistoryItem, FxLiquidityPreview, FxPairAdmin, FxPairAdminDetail } from '@/types/fx'
const dialog = useDialog(), gate = inject<ComputedRef<boolean | null>>('fxAdminGate')
const pairs = ref<FxPairAdminDetail[]>([]), pairId = ref<number | null>(null), listError = ref(''), listLoading = ref(true)
const overview = ref<FxAdminOverview | null>(null), stateLoading = ref(false), stateError = ref('')
const history = ref<FxLiquidityHistoryItem[]>([]), cursor = ref<number | null>(null), historyLoading = ref(false), historyError = ref('')
const action = ref<FxLiquidityAction>('fund'), gold = ref('0'), foreign = ref('0')
const preview = ref<FxLiquidityPreview | null>(null), previewLoading = ref(false), previewError = ref('')
const confirming = ref(false), submitting = ref(false), unknown = ref(false), writeError = ref('')
const result = ref<{pair:FxPairAdmin;action:FxLiquidityAction;gold:string;foreign:string}|null>(null)
let selection = 0, generation = 0, timer:ReturnType<typeof setTimeout>|undefined
const locked = computed(() => confirming.value || submitting.value)
const selected = computed(() => pairs.value.find(p => p.id === pairId.value))
const labels:Record<string,string> = {draft:'草稿',trading:'交易中',paused:'暂停',closed:'关闭'}
const playerStatus = computed(() => {
  const p = overview.value?.pair
  if (!p || gate?.value == null) return '玩家交易状态未知'
  if (p.archived) return '已归档，玩家不可交易'
  if (!gate.value) return '总闸关闭，玩家不可交易'
  if (p.status === 'paused' && p.reduce_only) return '市场暂停，玩家只允许卖出减仓或回补空头'
  if (p.status !== 'trading') return `${labels[p.status] ?? p.status}，玩家不可交易`
  return p.reduce_only ? '玩家当前只允许减仓' : '玩家当前可交易，仍受账户风控限制'
})
const inputError = computed(() => {
  const valid = (v:string) => /^\d+(?:\.\d{1,6})?$/.test(v) && compareFxAmounts(v,'9999999999.999999') !== 1
  if (!valid(gold.value) || !valid(foreign.value)) return '请填写非负十进制金额，最多 6 位小数，不超过 9999999999.999999'
  return compareFxAmounts(gold.value,'0') !== 1 && compareFxAmounts(foreign.value,'0') !== 1 ? '至少填写一项正数' : ''
})
async function loadPairs() {
  listLoading.value=true; listError.value=''
  try { pairs.value=await fxAdminApi.listPairs(); pairId.value=pairs.value[0]?.id ?? null }
  catch(e) { listError.value=mapFxError(e,'市场列表读取失败') }
  finally { listLoading.value=false }
}
async function loadState(id:number, epoch:number) {
  stateLoading.value=true; stateError.value=''; overview.value=null
  try { const data=await fxAdminApi.getOverview(id); if(epoch===selection) overview.value=data }
  catch(e) { if(epoch===selection) stateError.value=mapFxError(e,'当前状态读取失败') }
  finally { if(epoch===selection) stateLoading.value=false }
}
async function loadHistory(more=false) {
  if(!pairId.value || historyLoading.value || (more && cursor.value===null)) return
  const id=pairId.value, epoch=selection, before=more ? cursor.value! : undefined
  historyLoading.value=true; historyError.value=''
  try { const data=await fxAdminApi.getLiquidityHistory(id,before); if(epoch===selection) { history.value=more ? [...history.value,...data.items] : data.items; cursor.value=data.next_before_id } }
  catch(e) { if(epoch===selection) historyError.value=mapFxError(e,'操作记录读取失败') }
  finally { if(epoch===selection) historyLoading.value=false }
}
async function refresh() { if(pairId.value && !locked.value) await Promise.all([loadState(pairId.value,selection),loadHistory()]) }
function schedule() {
  clearTimeout(timer); const epoch=++generation
  preview.value=null; previewError.value=''; previewLoading.value=false
  if(!pairId.value || inputError.value || locked.value) return
  const id=pairId.value, body={action:action.value,gold_amount:gold.value,foreign_amount:foreign.value}
  previewLoading.value=true
  timer=setTimeout(async()=>{
    try { const data=await fxAdminApi.previewLiquidity(id,body); if(epoch===generation) preview.value=data }
    catch(e) { if(epoch===generation) previewError.value=mapFxError(e,'试算读取失败') }
    finally { if(epoch===generation) previewLoading.value=false }
  },350)
}
watch([pairId,action,gold,foreign],schedule)
watch(pairId,id=>{
  const epoch=++selection; overview.value=null; stateError.value=''; history.value=[]; cursor.value=null; historyError.value=''; historyLoading.value=false
  if(id) { void loadState(id,epoch); void loadHistory() }
})
const verb=(a:string)=>a==='fund'?'注资':a==='withdraw'?'撤资':'初始发行'
async function confirmOperation() {
  if(!pairId.value || locked.value || inputError.value || unknown.value || !overview.value || stateError.value) return
  const id=pairId.value, body=Object.freeze({action:action.value,gold_amount:gold.value,foreign_amount:foreign.value})
  const name=selected.value ? `${selected.value.currency_name}（${selected.value.currency_code}）` : `市场 #${id}`
  const epoch=++generation; clearTimeout(timer); confirming.value=true; previewLoading.value=true; preview.value=null; previewError.value=''; writeError.value=''
  try {
    const fresh=await fxAdminApi.previewLiquidity(id,body)
    if(epoch!==generation || id!==pairId.value) { confirming.value=false; return }
    preview.value=fresh
    if(!fresh.executable) { confirming.value=false; return }
    dialog.warning({title:`确认${verb(body.action)} ${name}`,
      content:`固定数量：${amount(body.gold_amount)} 金圆券，${amount(body.foreign_amount)} 外币。最新试算汇率 ${price(fresh.price_before)} → ${price(fresh.price_after)}，变化 ${amount(fresh.price_change_pct,4)}%。池子将为 ${amount(fresh.pool_after.gold)} 金圆券 / ${amount(fresh.pool_after.foreign)} 外币；金库将为 ${amount(fresh.treasury_after.gold)} 金圆券 / ${amount(fresh.treasury_after.foreign)} 外币。试算不锁价，执行按最新储备检查。`,
      positiveText:`确认${verb(body.action)}`,negativeText:'取消',maskClosable:false,closeOnEsc:false,
      onNegativeClick:()=>{confirming.value=false},onClose:()=>{confirming.value=false},
      onPositiveClick:async()=>{
        if(submitting.value) return false
        submitting.value=true
        try {
          const actual=body.action==='fund' ? await fxAdminApi.fundPair(id,body) : await fxAdminApi.withdrawPair(id,body)
          result.value={pair:actual,action:body.action,gold:body.gold_amount,foreign:body.foreign_amount}
          await Promise.all([loadState(id,selection),loadHistory()]); preview.value=null
        } catch(e) {
          const status=(e as {status?:number})?.status
          if(!status || status>=500) { unknown.value=true; writeError.value='执行结果暂不明确。请先刷新状态、核对操作记录，再决定后续处理；不要直接重复提交。' }
          else writeError.value=mapFxError(e,`${verb(body.action)}未完成`)
        } finally { submitting.value=false; confirming.value=false }
      },
    })
  } catch(e) { previewError.value=mapFxError(e,'最新试算读取失败，未提交操作'); confirming.value=false }
  finally { previewLoading.value=false }
}
onMounted(loadPairs)
onBeforeUnmount(()=>{clearTimeout(timer); generation++; selection++})
</script>
<template>
<div class="fx-admin fx-operations">
  <div class="fx-panel-head"><h2 class="page-title">央行操作</h2><button class="btn-secondary" :disabled="locked || stateLoading || historyLoading" @click="refresh">刷新状态与记录</button></div>
  <p v-if="listLoading">市场列表读取中…</p><p v-else-if="listError" class="fx-error" role="alert">{{ listError }} <button class="btn-secondary" @click="loadPairs">重新读取</button></p>
  <label v-else class="fx-selection">管理币种<select v-model="pairId" class="fx-input" :disabled="locked"><option v-for="p in pairs" :key="p.id" :value="p.id">{{ p.currency_name }}（{{ p.currency_code }}）{{ p.archived ? ' 已归档' : '' }}</option></select></label>
  <p v-if="!listLoading && !listError && !pairs.length">暂无市场，请先登记市场。</p>
  <template v-if="pairId">
    <section class="fx-panel"><h2>当前状态</h2><p>{{ playerStatus }}</p><p v-if="stateLoading">当前状态读取中…</p><p v-else-if="stateError" class="fx-error" role="alert">{{ stateError }}</p>
      <template v-else-if="overview"><div class="fx-balances"><div><h3>交易池</h3><p>{{ amount(overview.pair.gold_reserve) }} 金圆券</p><p>{{ amount(overview.pair.foreign_reserve) }} {{ overview.pair.currency_code }}</p></div><div><h3>系统金库</h3><p>手续费账户 {{ amount(overview.pair.gold_balance) }} 金圆券</p><p>借币库存 {{ amount(overview.pair.foreign_balance) }} {{ overview.pair.currency_code }}</p></div></div><p>参考汇率：1 外币 = {{ price(overview.reference_price) }} 金圆券</p><p class="fx-hint">数据时间 {{ overview.as_of }}，池子版本 {{ overview.pair.pool_version }}</p></template><p v-else>当前余额未知</p>
    </section>
    <section class="fx-panel"><h2>固定金额操作</h2><p class="fx-hint">交易池与金库分别调整同额余额。草稿、暂停或关闭的未归档市场仍可注资。试算不锁定汇率或涨跌幅。</p>
      <div class="fx-setup-fields"><label>操作<select v-model="action" class="fx-input" :disabled="locked"><option value="fund">注资</option><option value="withdraw">撤资</option></select></label><label>金圆券数量<input v-model="gold" class="fx-input" inputmode="decimal" :disabled="locked" /></label><label>{{ selected?.currency_code ?? '外币' }} 数量<input v-model="foreign" class="fx-input" inputmode="decimal" :disabled="locked" /></label></div>
      <p v-if="inputError" class="fx-hint">{{ inputError }}</p><p v-else-if="previewLoading" aria-live="polite">试算读取中…</p><p v-else-if="previewError" class="fx-error" role="alert">{{ previewError }}</p>
      <div v-else-if="preview" class="fx-setup-preview"><h3>预计变化</h3><div class="table-wrap"><table class="fx-table"><thead><tr><th>项目</th><th>操作前</th><th>操作后</th></tr></thead><tbody>
        <tr><td>参考汇率（金圆券 / 外币）</td><td>{{ price(preview.price_before) }}</td><td>{{ price(preview.price_after) }}</td></tr>
        <tr><td>交易池金圆券</td><td>{{ amount(preview.pool_before.gold) }}</td><td>{{ amount(preview.pool_after.gold) }}</td></tr><tr><td>交易池外币</td><td>{{ amount(preview.pool_before.foreign) }}</td><td>{{ amount(preview.pool_after.foreign) }}</td></tr>
        <tr><td>金库金圆券</td><td>{{ amount(preview.treasury_before.gold) }}</td><td>{{ amount(preview.treasury_after.gold) }}</td></tr><tr><td>金库外币</td><td>{{ amount(preview.treasury_before.foreign) }}</td><td>{{ amount(preview.treasury_after.foreign) }}</td></tr>
      </tbody></table></div><p>汇率预计变化 {{ amount(preview.price_change_pct,4) }}%</p><p class="fx-hint">试算时间 {{ preview.as_of }}。— 表示无法计算或未知。</p><p v-if="!preview.executable" class="fx-error" role="alert">{{ preview.blocked_reason ?? '当前操作无法执行' }}</p></div>
      <button :class="action === 'fund' ? 'btn-primary' : 'btn-secondary'" :disabled="locked || stateLoading || historyLoading || unknown || !!inputError || !overview || !!stateError || !preview?.executable" @click="confirmOperation">{{ submitting ? '执行中…' : confirming ? '等待确认…' : `确认${verb(action)}` }}</button><p v-if="writeError" class="fx-error" role="alert">{{ writeError }}</p><p v-if="unknown" class="fx-hint">本页资金提交已暂停，请核对服务端记录。页面不会自动重试。</p>
    </section>
    <section v-if="result" class="fx-panel" aria-live="polite"><h2>{{ verb(result.action) }}已完成</h2><p>{{ result.pair.currency_name }}（{{ result.pair.currency_code }}），实际提交 {{ amount(result.gold) }} 金圆券与 {{ amount(result.foreign) }} 外币。</p><p>回执交易池 {{ amount(result.pair.gold_reserve) }} 金圆券 / {{ amount(result.pair.foreign_reserve) }} 外币</p><p class="fx-hint">以上为本次执行回执，后续状态或记录刷新失败可独立刷新。</p></section>
    <section class="fx-panel"><h2>玩家持仓汇总</h2><p v-if="stateLoading">持仓读取中…</p><p v-else-if="!overview">持仓未知，请刷新当前状态。</p><template v-else><div class="fx-balances"><div><h3>多头</h3><p>{{ overview.holdings.long_users }} 人，{{ amount(overview.holdings.long_foreign) }} 外币</p><p>账面成本 {{ amount(overview.holdings.long_cost_gold) }} 金圆券</p></div><div><h3>空头</h3><p>{{ overview.holdings.short_users }} 人，本金 {{ amount(overview.holdings.short_principal_foreign) }} 外币</p><p>已入账利息 {{ amount(overview.holdings.short_recorded_interest_foreign) }} 外币</p></div></div><p class="fx-hint">涵盖全部钱包与空头记录；利息未实时计提。持仓仅在进页、切币、刷新或操作成功后读取。</p></template></section>
    <section class="fx-panel"><div class="fx-panel-head"><h2>最近操作记录</h2><button class="btn-secondary" :disabled="historyLoading || locked" @click="loadHistory()">刷新记录</button></div><p v-if="historyError" class="fx-error" role="alert">{{ historyError }}</p><p v-if="historyLoading">记录读取中…</p><p v-else-if="!history.length && !historyError">暂无操作记录</p>
      <div v-if="history.length" class="table-wrap"><table class="fx-table"><thead><tr><th>审计 ID / 时间</th><th>操作 / 操作者</th><th>固定数量</th><th>池子前后余额</th><th>金库前后余额</th><th>汇率变化</th></tr></thead><tbody><tr v-for="item in history" :key="item.id"><td>#{{ item.id }}<br />{{ item.ts }}</td><td>{{ verb(item.action) }}<br />{{ item.operator_user_id === null ? '未知' : `用户 #${item.operator_user_id}` }}</td><td>{{ amount(item.gold_amount) }} 金圆券<br />{{ amount(item.foreign_amount) }} 外币</td><td>{{ amount(item.pool_before?.gold) }} → {{ amount(item.pool_after?.gold) }} 金圆券<br />{{ amount(item.pool_before?.foreign) }} → {{ amount(item.pool_after?.foreign) }} 外币</td><td>{{ amount(item.treasury_before?.gold) }} → {{ amount(item.treasury_after?.gold) }} 金圆券<br />{{ amount(item.treasury_before?.foreign) }} → {{ amount(item.treasury_after?.foreign) }} 外币</td><td>{{ price(item.price_before) }} → {{ price(item.price_after) }}<br />{{ amount(item.price_change_pct,4) }}%</td></tr></tbody></table></div><p class="fx-hint">记录来自服务端审计，按 ID 倒序。旧记录缺失字段显示 —。</p><button v-if="cursor !== null" class="btn-secondary" :disabled="historyLoading || locked" @click="loadHistory(true)">加载更早记录</button>
    </section>
  </template>
</div>
</template>
<style scoped>
.fx-selection { display:block; max-width:480px; margin-bottom:16px; font-weight:700; }
.fx-balances { display:grid; grid-template-columns:1fr 1fr; border:2px solid #000; margin:12px 0; }
.fx-balances > div { padding:14px; overflow-wrap:anywhere; }
.fx-balances > div + div { border-left:2px solid #000; }
.fx-balances h3 { margin:0 0 10px; font-size:14px; }
.fx-balances p { font-variant-numeric:tabular-nums; margin:6px 0; }
.fx-setup-fields label { font-size:13px; display:flex; flex-direction:column; gap:6px; margin:12px 0; }
@media(max-width:650px) { .fx-balances { grid-template-columns:1fr; } .fx-balances > div + div { border-left:0; border-top:2px solid #000; } }
</style>
