import { computed, readonly, ref, shallowRef, watch } from 'vue'
import { defineStore } from 'pinia'
import { useAuthStore } from './auth'
import { useNotificationStore } from './notification'
import { authIdentityGuard } from '@/api'
import { groupKey, tradingContextApi } from '@/api/tradingContext'
import { TradingStream, type StreamFrame } from '@/api/tradingStream'
import type { AccountBasis, AccountChanged, GroupCoverage, GroupKey, PricingGroup, TradingContext, TradingRules, TradingSessionStatus } from '@/types/tradingContext'
import { simulateFxOrder, valueFxPostTrade, type FxOrder } from '@/utils/fxSimulation'
import { accrueTradingDebt, valueTradingAccount, type AccountPreview, type TradingValuationCache } from '@/utils/tradingRisk'
import type { FxOrderResult } from '@/types/fx'
import { describeFxOrderFailure, formatFxAmount } from '@/api/fx'
import { formatUnits, units } from '@/utils/fxDecimal'
export function compareVersion(a: string, b: string) { const x = BigInt(a), y = BigInt(b); return x < y ? -1 : x > y ? 1 : 0 }
function covers(group: PricingGroup | undefined, minimum: GroupCoverage) {
  // Pricing floors and account economic_version advance independently.
  return !!group && group.stamp.epoch === minimum.stamp.epoch && (compareVersion(group.stamp.revision, minimum.stamp.revision) > 0 || group.stamp.revision === minimum.stamp.revision && group.stamp.input_hash === minimum.stamp.input_hash)
}
/** Route owner: start is idempotent for the same group; stop releases all resources. */
export const useTradingSessionStore = defineStore('tradingSession', () => {
  const auth = useAuthStore(), notifications = useNotificationStore()
  const account = shallowRef<AccountBasis | null>(null), rules = shallowRef<TradingRules | null>(null)
  const groups = shallowRef(new Map<string, PricingGroup>()), currentGroup = shallowRef<GroupKey | null>(null)
  const status = ref<TradingSessionStatus>('syncing'), error = shallowRef<unknown>(null)
  const accountPreview = shallowRef<AccountPreview | null>(null), now = ref(new Date().toISOString())
  const lastOrderResult = shallowRef<FxOrderResult | null>(null)
  const ready = computed(() => status.value === 'ready')
  let routeOwner = 0, nextOwner = 0
  let foregroundRequired = false, foregroundSequence = 0
  let active = false, generation = 0, synced = false, dirty = true, target = '0', forceRefresh = false
  let candidate: TradingContext | null = null, minimum: GroupCoverage[] = [], missing: GroupKey[] = [], latestCoverage: GroupCoverage[] = []
  let cache: TradingValuationCache | undefined, inFlight = false, checking = false, failures = 0
  let ruleReceipt = 0, priceReceipt = 0, serverMs = Date.now(), clockAt = performance.now(), hasServerClock = false
  let refreshTimer: ReturnType<typeof setTimeout> | null = null, clockTimer: ReturnType<typeof setInterval> | null = null, checkTimer: ReturnType<typeof setInterval> | null = null
  let notifyTimer: ReturnType<typeof setTimeout> | null = null, forcedCount = 0
  const notificationIds = new Set<number>()
  function rememberNotification(id: number) {
    notificationIds.add(id)
    if (notificationIds.size > 128) { const oldest = notificationIds.values().next().value!; notifications.removeNotification(oldest); notificationIds.delete(oldest) }
  }
  const stream = new TradingStream(), eventIds = new Set<string>(), operationRefs = new Set<string>()
  const receipts = new Map<string, number>(), retiredEpochs = new Map<string, Set<string>>()
  let http = new AbortController()
  const valid = (g: number, guard: () => boolean) => active && generation === g && guard()
  function calibrate(time: string) {
    const value = Date.parse(time), elapsed = Math.max(0, performance.now() - clockAt)
    if (Number.isFinite(value)) { serverMs = hasServerClock ? Math.max(value, serverMs + elapsed) : value; hasServerClock = true; clockAt = performance.now(); tick() }
  }
  function tick() {
    now.value = new Date(serverMs + Math.max(0, performance.now() - clockAt)).toISOString()
    derive()
  }
  function derive() {
    if (!account.value || !rules.value) { accountPreview.value = null; return }
    const result = valueTradingAccount(account.value, [...groups.value.values()], rules.value, now.value, cache)
    cache = result.valuation_cache; accountPreview.value = result
  }
  function complete() {
    return !missing.length && [...minimum, ...latestCoverage].every(stamp => covers(groups.value.get(groupKey(stamp)), stamp)) && (!currentGroup.value || groups.value.has(groupKey(currentGroup.value)))
  }
  function install() {
    if (candidate && !forceRefresh && compareVersion(candidate.account.economic_version, target) >= 0 && complete()) {
      account.value = candidate.account; candidate = null; dirty = false; forceRefresh = false; failures = 0
      if (refreshTimer) clearTimeout(refreshTimer)
      refreshTimer = null; scheduleAfter = null
    }
    status.value = synced && !foregroundRequired && !forceRefresh && !dirty && account.value && complete() ? 'ready' : account.value ? 'stale' : 'syncing'
    derive()
  }
  function merge(pricing: PricingGroup[], absent: GroupKey[] = [], httpReceipt?: number) {
    const next = new Map(groups.value)
    for (const group of pricing) {
      const key = groupKey(group), old = next.get(key)
      if (retiredEpochs.get(key)?.has(group.stamp.epoch)) continue
      if (old) {
        if (old.stamp.epoch === group.stamp.epoch) {
          if (compareVersion(group.stamp.revision, old.stamp.revision) < 0) continue
          if (group.stamp.revision === old.stamp.revision && group.stamp.input_hash !== old.stamp.input_hash) { dirty = true; continue }
        } else {
          if (httpReceipt != null && (receipts.get(key) ?? 0) > httpReceipt) continue
          const retired = retiredEpochs.get(key) ?? new Set<string>(); retired.add(old.stamp.epoch); retiredEpochs.set(key, retired)
          // A pricing floor from a former server epoch cannot certify the new one.
          if (minimum.some(stamp => groupKey(stamp) === key && stamp.stamp.epoch !== group.stamp.epoch)) dirty = true
        }
      }
      next.set(key, group); receipts.set(key, ++priceReceipt)
    }
    for (const key of absent) if (httpReceipt == null || (receipts.get(groupKey(key)) ?? 0) <= httpReceipt) next.delete(groupKey(key))
    groups.value = next
    install()
  }
  function schedule(delay = 200) {
    if (!active || inFlight || refreshTimer) return
    refreshTimer = setTimeout(() => { refreshTimer = null; void refresh() }, delay)
  }
  function requestRefresh(version?: string, _reason?: string, operationRef?: string | null) {
    if (!active) return
    if (version && compareVersion(version, target) > 0) target = version
    if (operationRef) {
      if (operationRefs.has(operationRef) && account.value && version && compareVersion(account.value.economic_version, version) >= 0) return
      operationRefs.add(operationRef); if (operationRefs.size > 512) operationRefs.delete(operationRefs.values().next().value!)
    }
    if (version && account.value && compareVersion(account.value.economic_version, target) >= 0 && !dirty) return
    dirty = true; if (!version) { forceRefresh = true; candidate = null; if (refreshTimer) clearTimeout(refreshTimer); refreshTimer = null }
    status.value = account.value ? 'stale' : 'syncing'
    schedule()
  }
  async function refresh() {
    if (!active || inFlight) return
    inFlight = true
    const g = generation, guard = authIdentityGuard(), r = ruleReceipt, p = priceReceipt
    forceRefresh = false
    try {
      const context = await tradingContextApi.getContext(currentGroup.value, http.signal)
      if (!valid(g, guard)) return
      if (context.account.user_id !== auth.user?.id) throw new Error('Trading context identity mismatch')
      calibrate(context.server_time)
      if (!streamStarted && ruleReceipt === r) rules.value = context.rules
      // A newer target arriving during the request never installs this older account.
      if (compareVersion(context.account.economic_version, target) < 0 || forceRefresh) { scheduleAfter = Math.min(15000, 200 * 2 ** Math.min(failures++, 6)); return }
      const dependencyChanged = account.value != null && JSON.stringify(minimum.map(groupKey).sort()) !== JSON.stringify(context.minimum_group_stamps.map(groupKey).sort())
      candidate = context; minimum = context.minimum_group_stamps; missing = context.missing_groups
      latestCoverage = latestCoverage.filter(stamp => !context.minimum_group_stamps.some(floor => groupKey(floor) === groupKey(stamp) && floor.stamp.epoch !== stamp.stamp.epoch))
      merge(context.groups, context.missing_groups, p)
      if (!streamStarted || dependencyChanged) { connect(); install() }
      if (!complete()) { status.value = 'stale'; scheduleAfter = Math.min(15000, 1000 * 2 ** Math.min(failures++, 4)); void checkVersions() }
      else if (dirty) scheduleAfter = 200
      if (foregroundRequired && !dirty && complete()) void checkVersions()
    } catch (cause) {
      if (!valid(g, guard)) return
      error.value = cause; status.value = account.value ? 'stale' : 'error'
      scheduleAfter = Math.min(15000, 1000 * 2 ** Math.min(failures++, 4))
    } finally {
      if (valid(g, guard)) { inFlight = false; if (scheduleAfter != null) { const delay = scheduleAfter; scheduleAfter = null; schedule(delay) } }
    }
  }
  let scheduleAfter: number | null = null, streamStarted = false
  function notify(event: AccountChanged) {
    if (eventIds.has(event.event_id)) return
    eventIds.add(event.event_id); if (eventIds.size > 512) eventIds.delete(eventIds.values().next().value!)
    if (event.reason === 'fx_orders' || /interest|accrual|rate_maintenance/.test(event.reason)) return
    if (/liquidat|credit_execution/.test(event.reason)) {
      forcedCount++
      if (!notifyTimer) notifyTimer = setTimeout(() => { notifyTimer = null; rememberNotification(notifications.warning('账户发生强制处置', forcedCount > 1 ? `账户已完成 ${forcedCount} 次处置，请核对仓位和债务。` : '账户已完成一阶段处置，请核对仓位和债务。')); forcedCount = 0 }, 200)
    } else if (/admin/.test(event.reason)) rememberNotification(notifications.info('账户已由管理员调整', '余额、债务或账户权限发生变化，请核对最新状态。'))
    else if (!event.operation_ref || !operationRefs.has(event.operation_ref)) rememberNotification(notifications.info('账户状态已更新', '余额、仓位或债务发生变化，正在同步最新状态。'))
  }
  function orderResult(result: FxOrderResult) {
    const identity = `fx_order:${result.order_id}`
    if (eventIds.has(identity)) return
    eventIds.add(identity); if (eventIds.size > 512) eventIds.delete(eventIds.values().next().value!)
    lastOrderResult.value = result
    if (result.status === 'succeeded') {
      const buy = result.action === 'buy' || result.action === 'close_short'
      const amounts = result.input_amount != null && result.output_amount != null
        ? `投入 ${formatFxAmount(result.input_amount)} ${buy ? '金圆券' : result.currency_code}，获得 ${formatFxAmount(result.output_amount)} ${buy ? result.currency_code : '金圆券'}。`
        : '请查看挂单记录中的成交结果。'
      const financing = result.borrow_amount != null && units(result.borrow_amount) > 0n ? `自动借入 ${formatFxAmount(result.borrow_amount)} 金圆券。` : ''
      rememberNotification(notifications.success(`${result.currency_code} 挂单 #${result.order_id} 已成交`, amounts + financing))
      requestRefresh(undefined, 'fx_orders')
    } else rememberNotification(notifications.error(`${result.currency_code} 挂单 #${result.order_id} 未成交`, describeFxOrderFailure(result.failure_reason)))
  }
  function frame(frame: StreamFrame) {
    const payload = JSON.parse(frame.data)
    if (frame.event === 'sync') {
      synced = true; rules.value = payload.rules; ruleReceipt++
      calibrate(payload.server_time)
      latestCoverage = payload.groups.map((group: PricingGroup) => ({ product: group.product, id: group.id, stamp: group.stamp }))
      merge(payload.groups, payload.missing_groups)
      requestRefresh(payload.economic_version, 'sync')
      install()
      if (foregroundRequired && !dirty && complete()) void checkVersions()
    } else if (frame.event === 'pricing') {
      calibrate(payload.server_time); merge(payload.groups, payload.missing_groups)
      if (dirty && account.value && !candidate) requestRefresh(account.value.economic_version, 'epoch')
    } else if (frame.event === 'rules') { rules.value = payload; ruleReceipt++; derive() }
    else if (frame.event === 'heartbeat') calibrate(payload.server_time)
    else if (frame.event === 'fx_order_result') orderResult(payload)
    else if (frame.event === 'account_changed') { notify(payload); requestRefresh(payload.economic_version, payload.reason, payload.operation_ref) }
  }
  function connect() {
    streamStarted = true; synced = false
    const g = generation, guard = authIdentityGuard()
    stream.connect(currentGroup.value, { frame: value => { if (valid(g, guard)) frame(value) }, disconnected: () => { if (valid(g, guard)) { synced = false; install() } }, error: cause => { if (valid(g, guard)) error.value = cause } })
  }
  async function checkVersions() {
    if (!active || checking || typeof document !== 'undefined' && document.visibilityState === 'hidden') return
    checking = true
    const g = generation, guard = authIdentityGuard(), r = ruleReceipt, checkReceipt = priceReceipt, recovery = foregroundSequence
    try {
      const version = await tradingContextApi.getVersions(currentGroup.value, http.signal)
      if (!valid(g, guard)) return
      calibrate(version.server_time)
      if (!account.value || compareVersion(version.economic_version, account.value.economic_version) > 0) requestRefresh(version.economic_version, 'version_check')
      if (ruleReceipt === r && rules.value?.rule_version !== version.rule_version) { connect(); install() }
      latestCoverage = version.group_stamps.filter(stamp => {
        const group = groups.value.get(groupKey(stamp))
        return !group || group.stamp.epoch === stamp.stamp.epoch || (receipts.get(groupKey(stamp)) ?? 0) <= checkReceipt
      })
      const required = version.group_stamps.filter(stamp => !covers(groups.value.get(groupKey(stamp)), stamp))
      if (version.missing_groups.length) missing = version.missing_groups
      if (required.length || version.missing_groups.length) {
        status.value = 'stale'
        const p = priceReceipt
        const prices = await tradingContextApi.getPrices(required, currentGroup.value, http.signal)
        if (!valid(g, guard)) return
        missing = missing.filter(key => !prices.groups.some(group => groupKey(key) === groupKey(group)))
        calibrate(prices.server_time); merge(prices.groups, [...prices.missing_groups, ...version.missing_groups], p)
        if (account.value && !candidate && dirty) requestRefresh(account.value.economic_version, 'epoch_check')
      }
      if (recovery === foregroundSequence && synced && !dirty && !forceRefresh && account.value && compareVersion(account.value.economic_version, version.economic_version) >= 0 && rules.value?.rule_version === version.rule_version && complete()) foregroundRequired = false
      install()
    } catch (cause) { if (valid(g, guard)) { error.value = cause; status.value = account.value ? 'stale' : 'error' } }
    finally { if (valid(g, guard)) { checking = false; if (foregroundRequired && recovery !== foregroundSequence) void checkVersions() } }
  }
  function visibility() { if (document.visibilityState !== 'hidden') { foregroundRequired = true; foregroundSequence++; status.value = 'stale'; void checkVersions() } }
  function stop(owner?: number) {
    if (owner != null && owner !== routeOwner) return
    routeOwner = 0
    active = false; generation++; http.abort(); stream.close(); streamStarted = false
    if (refreshTimer) clearTimeout(refreshTimer)
    if (clockTimer) clearInterval(clockTimer)
    if (checkTimer) clearInterval(checkTimer)
    if (notifyTimer) clearTimeout(notifyTimer)
    refreshTimer = clockTimer = checkTimer = notifyTimer = null
    if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', visibility)
    lastOrderResult.value = null
    account.value = null; rules.value = null; groups.value = new Map(); accountPreview.value = null
    currentGroup.value = null; cache = undefined; candidate = null; minimum = []; missing = []; latestCoverage = []
    notificationIds.forEach(id => notifications.removeNotification(id)); notificationIds.clear()
    eventIds.clear(); operationRefs.clear(); receipts.clear(); retiredEpochs.clear()
    foregroundRequired = false; foregroundSequence = 0
    synced = false; dirty = true; target = '0'; inFlight = checking = forceRefresh = false
    ruleReceipt = priceReceipt = failures = forcedCount = 0; scheduleAfter = null; status.value = 'syncing'; error.value = null
  }
  function start(group: GroupKey | null = null) {
    if (active && ((!group && !currentGroup.value) || group && currentGroup.value && groupKey(group) === groupKey(currentGroup.value))) { routeOwner = ++nextOwner; return routeOwner }
    stop(); routeOwner = ++nextOwner
    if (!auth.user || !auth.accessToken) return routeOwner
    active = true; currentGroup.value = group; http = new AbortController()
    serverMs = Date.now(); clockAt = performance.now(); hasServerClock = false
    clockTimer = setInterval(tick, 1000)
    checkTimer = setInterval(() => { void checkVersions() }, 15000)
    if (typeof document !== 'undefined') document.addEventListener('visibilitychange', visibility)
    void refresh()
    return routeOwner
  }
  watch(() => auth.sessionGeneration, () => { if (active) { const group = currentGroup.value, owner = routeOwner; stop(); if (auth.user && auth.accessToken) { start(group); routeOwner = owner } } }, { flush: 'sync' })
  function previewFx(order: FxOrder, pairId = currentGroup.value?.product === 'fx' ? currentGroup.value.id : null) {
    const pricing = pairId == null ? undefined : groups.value.get(`fx:${pairId}`)
    if (!account.value || !rules.value || pricing?.product !== 'fx') return null
    const preview = simulateFxOrder(account.value, pricing, rules.value, order, now.value)
    const whole = valueFxPostTrade(account.value, preview, [...groups.value.values()], rules.value, now.value, cache)
    // Current-only missing dependencies are replaced by complete portfolio valuation.
    const trueRejections = preview.rejected_reasons.filter(reason => reason !== 'insufficient_initial_margin')
    let approximateCover = preview.legs.some(leg => leg.purpose === 'short_cover')
      && units(preview.foreign_debt_after) === 0n
      && units(preview.interest_uncertainty.shorts[pairId!] ?? '0') > 0n
    const growth = units(preview.growth_amount) > 0n
    if (approximateCover && growth && order.side === 'buy') {
      // A full cover is ambiguous only if rounding can change admission. Check
      // both accrued-debt bounds with the real sequential order and whole portfolio.
      const target = account.value.fx_shorts.find(row => row.pair_id === pairId)
      if (target) {
        try {
          const accrued = accrueTradingDebt(formatUnits(units(target.principal_foreign) + units(target.interest_foreign)), target.interest_last_accrued_at, rules.value.loan_daily_rate, now.value)
          const center = units(accrued.amount), error = units(accrued.uncertainty)
          const bounds = [center > error ? center - error : 0n, center + error]
          approximateCover = !bounds.every(total => {
            const principal = units(target.principal_foreign) < total ? units(target.principal_foreign) : total
            const boundedAccount = { ...account.value!, fx_shorts: account.value!.fx_shorts.map(row => row === target
              ? { ...row, principal_foreign: formatUnits(principal), interest_foreign: formatUnits(total - principal), interest_last_accrued_at: now.value }
              : row) }
            const bounded = simulateFxOrder(boundedAccount, pricing, rules.value!, order, now.value)
            const risk = valueFxPostTrade(boundedAccount, bounded, [...groups.value.values()], rules.value!, now.value)
            return bounded.rejected_reasons.every(reason => reason === 'insufficient_initial_margin')
              && bounded.final_direction === 'long' && units(bounded.growth_amount) > 0n
              && units(bounded.foreign_debt_after) === 0n && risk.initial_condition === 'satisfied'
          })
        } catch { /* Keep admission unknown if either bound cannot be valued. */ }
      }
    }
    if (growth && whole.has_debt !== false && whole.initial_condition === 'rejected') trueRejections.push('insufficient_initial_margin')
    preview.rejected_reasons = trueRejections; preview.account_preview = whole
    preview.unchecked_reasons = preview.unchecked_reasons.filter(reason => reason !== 'interest_exact_cover_uncertainty')
    if (approximateCover) preview.unchecked_reasons.unshift('interest_exact_cover_uncertainty')
    if (!ready.value) preview.unchecked_reasons.push('trading_session_not_ready')
    if (growth && whole.initial_condition === 'unknown') preview.unchecked_reasons.push(...whole.unknown_reasons)
    preview.condition = trueRejections.length ? 'rejected' : !ready.value || approximateCover || growth && whole.has_debt !== false && whole.initial_condition === 'unknown' ? 'unknown' : 'satisfied'
    return preview
  }
  return { lastOrderResult: readonly(lastOrderResult), account, rules, groups, currentGroup, status, error, ready, now, accountPreview, start, stop, requestRefresh, checkVersions, previewFx }
})
