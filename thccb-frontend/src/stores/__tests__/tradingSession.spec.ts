import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { reactive } from 'vue'
import type { TradingStreamHandlers } from '@/api/tradingStream'
import type { AccountBasis, FxPricingGroup, TradingContext, TradingRules } from '@/types/tradingContext'
const mocks = vi.hoisted(() => ({ context: vi.fn(), versions: vi.fn(), prices: vi.fn(), handlers: null as TradingStreamHandlers | null, close: vi.fn(), auth: {} as { user: { id: number } | null; accessToken: string | null; refreshToken: string; sessionGeneration: number } }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => mocks.auth }))
vi.mock('@/api', () => ({ authIdentityGuard: () => { const generation = mocks.auth.sessionGeneration; return () => generation === mocks.auth.sessionGeneration } }))
vi.mock('@/api/tradingContext', () => ({ groupKey: (key: { product: string; id: number }) => `${key.product}:${key.id}`, tradingContextApi: { getContext: mocks.context, getVersions: mocks.versions, getPrices: mocks.prices } }))
vi.mock('@/api/tradingStream', () => ({ TradingStream: class { connect(_group: unknown, handlers: TradingStreamHandlers) { mocks.handlers = handlers } close() { mocks.close() } } }))
import { formatUnits, units } from '@/utils/fxDecimal'
import { quoteFx } from '@/utils/fxSimulation'
import { accrueTradingDebt } from '@/utils/tradingRisk'
import { useTradingSessionStore } from '../tradingSession'
import { useNotificationStore } from '../notification'
const time = '2026-01-01T00:00:00Z'
const rules: TradingRules = { rule_version: 'opaque-A', loan_daily_rate: '0', lmsr_sell_fee_rate: '0', fx_enabled: true, fx_short_enabled: true, loan_enabled: true, unified_credit_enabled: true, credit_new_risk_frozen: false, risk_engine_ready: true, leverage: '3', initial_ratio: '0.5', maintenance_ratio: '0.2' }
const basis: AccountBasis = { user_id: 1, economic_version: '1', cash: '100', debt: '0', debt_last_accrued_at: null, credit_frozen: false, is_active: true, is_bot: false, tos_accepted_at: time, last_liquidated_at: null, lmsr_positions: [], fx_wallets: [], fx_shorts: [] }
function fx(id = 1, revision = '1', epoch = 'E'): FxPricingGroup { return { product: 'fx', id, stamp: { epoch, revision, input_hash: `${epoch}-${revision}` }, as_of: time, gold_reserve: '1000', foreign_reserve: '2000', buy_fee_rate: '0', sell_fee_rate: '0', pool_version: revision, status: 'trading', archived: false, reduce_only: false } }
function context(version = '1', pricing = [fx()], account = basis): TradingContext { return { schema_version: 1, account: { ...account, economic_version: version }, rules, groups: pricing, minimum_group_stamps: pricing.map(group => ({ product: group.product, id: group.id, stamp: group.stamp })), missing_groups: [], as_of: time, server_time: time } }
function frame(event: string, data: unknown) { mocks.handlers!.frame({ event, data: JSON.stringify(data) }) }
async function boot(data = context()) {
  mocks.context.mockResolvedValueOnce(data)
  const store = useTradingSessionStore(); store.start({ product: 'fx', id: 1 })
  await vi.advanceTimersByTimeAsync(0)
  expect(store.ready).toBe(false)
  frame('sync', { economic_version: data.account.economic_version, rules: data.rules, groups: data.groups, missing_groups: [], server_time: time })
  expect(store.ready).toBe(true)
  return store
}
beforeEach(() => {
  vi.useFakeTimers(); vi.setSystemTime(new Date(time)); vi.spyOn(performance, 'now').mockImplementation(() => Date.now()); setActivePinia(createPinia())
  mocks.auth = reactive({ user: { id: 1 }, accessToken: 'A', refreshToken: 'R', sessionGeneration: 1 })
  mocks.context.mockReset().mockResolvedValue(context()); mocks.versions.mockReset().mockResolvedValue({ economic_version: '1', rule_version: rules.rule_version, group_stamps: [fx()], missing_groups: [], server_time: time })
  mocks.prices.mockReset().mockResolvedValue({ groups: [fx(1, '2')], missing_groups: [], server_time: time })
  vi.stubGlobal('document', { visibilityState: 'visible', addEventListener: vi.fn(), removeEventListener: vi.fn() })
})
afterEach(() => { useTradingSessionStore().stop(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
it('updates five groups locally without quote/context HTTP or rolling back newer pricing', async () => {
  const store = await boot(context('1', [1, 2, 3, 4, 5].map(id => fx(id))))
  frame('pricing', { groups: [1, 2, 3, 4, 5].map(id => ({ ...fx(id, '3'), gold_reserve: '2000' })), missing_groups: [], server_time: time })
  frame('pricing', { groups: [fx(1, '2')], missing_groups: [], server_time: time })
  expect(store.groups.get('fx:1')!.stamp.revision).toBe('3')
  expect(store.previewFx({ side: 'buy', amount: '1' })!.condition).toBe('satisfied')
  expect(mocks.context).toHaveBeenCalledTimes(1); expect(mocks.prices).not.toHaveBeenCalled()
})
it('coalesces account hints at 200ms and follows a larger target arriving during refresh', async () => {
  const store = await boot()
  let resolve!: (value: TradingContext) => void
  mocks.context.mockImplementationOnce(() => new Promise<TradingContext>(done => { resolve = done })).mockResolvedValueOnce(context('4'))
  frame('account_changed', { event_id: 'a', economic_version: '2', reason: 'interest', occurred_at: time })
  frame('account_changed', { event_id: 'b', economic_version: '3', reason: 'interest', occurred_at: time })
  expect(store.ready).toBe(false)
  await vi.advanceTimersByTimeAsync(199); expect(mocks.context).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(1); expect(mocks.context).toHaveBeenCalledTimes(2)
  frame('account_changed', { event_id: 'c', economic_version: '4', reason: 'interest', occurred_at: time })
  resolve(context('3')); await vi.advanceTimersByTimeAsync(0)
  expect(store.account!.economic_version).toBe('1')
  await vi.advanceTimersByTimeAsync(200)
  expect(store.account!.economic_version).toBe('4'); expect(store.ready).toBe(true)
  expect(mocks.context).toHaveBeenCalledTimes(3)
})
it('retains a newer stream price and rules while older HTTP baseline is in flight', async () => {
  const store = await boot()
  let resolve!: (value: TradingContext) => void
  mocks.context.mockImplementationOnce(() => new Promise<TradingContext>(done => { resolve = done }))
  store.requestRefresh('2'); await vi.advanceTimersByTimeAsync(200)
  frame('pricing', { groups: [fx(1, '5')], missing_groups: [], server_time: time })
  frame('rules', { ...rules, rule_version: 'latest-not-sortable', fx_enabled: false })
  resolve(context('2', [fx(1, '2')])); await vi.advanceTimersByTimeAsync(0)
  expect(store.groups.get('fx:1')!.stamp.revision).toBe('5')
  expect(store.rules!.fx_enabled).toBe(false); expect(store.ready).toBe(true)
})
it('recovers a missing last account event at 15s; price-only drift calls only getPrices', async () => {
  const store = await boot()
  mocks.versions.mockResolvedValueOnce({ economic_version: '1', rule_version: rules.rule_version, group_stamps: [fx(1, '2')], missing_groups: [], server_time: time })
  await vi.advanceTimersByTimeAsync(15000)
  expect(mocks.context).toHaveBeenCalledTimes(1); expect(mocks.prices).toHaveBeenCalledTimes(1)
  expect(store.groups.get('fx:1')!.stamp.revision).toBe('2')
  mocks.versions.mockResolvedValueOnce({ economic_version: '2', rule_version: rules.rule_version, group_stamps: [fx(1, '2')], missing_groups: [], server_time: time })
  mocks.context.mockResolvedValueOnce(context('2', [fx(1, '2')]))
  await vi.advanceTimersByTimeAsync(15200)
  expect(store.account!.economic_version).toBe('2'); expect(mocks.context).toHaveBeenCalledTimes(2)
})
it('stops background checks and discards delayed HTTP, stream and notifications after account switching', async () => {
  const store = await boot()
  Object.assign(document, { visibilityState: 'hidden' }); await vi.advanceTimersByTimeAsync(30000)
  expect(mocks.versions).not.toHaveBeenCalled()
  let resolve!: (value: TradingContext) => void
  mocks.context.mockImplementationOnce(() => new Promise<TradingContext>(done => { resolve = done }))
  store.requestRefresh('2'); await vi.advanceTimersByTimeAsync(200)
  frame('account_changed', { event_id: 'shown', economic_version: '1', reason: 'admin_account', occurred_at: time })
  expect(useNotificationStore().notifications).toHaveLength(1)
  const old = mocks.handlers!
  old.frame({ event: 'account_changed', data: JSON.stringify({ event_id: 'force', economic_version: '2', reason: 'liquidation', occurred_at: time }) })
  mocks.auth.user = { id: 2 }; mocks.auth.accessToken = 'B'
  mocks.context.mockResolvedValueOnce(context('1', [fx()], { ...basis, user_id: 2, cash: '7' }))
  mocks.auth.sessionGeneration++
  resolve(context('2')); await vi.advanceTimersByTimeAsync(200)
  old.frame({ event: 'account_changed', data: JSON.stringify({ event_id: 'admin', economic_version: '3', reason: 'admin_account', occurred_at: time }) })
  expect(store.account!.user_id).toBe(2); expect(store.account!.cash).toBe('7')
  expect(useNotificationStore().notifications).toHaveLength(0)
})
it('deduplicates already-synced admin hints, batches forced changes, and keeps interest silent', async () => {
  await boot()
  const event = { event_id: 'admin', economic_version: '1', reason: 'admin_account', occurred_at: time }
  frame('account_changed', event); frame('account_changed', event)
  frame('account_changed', { ...event, event_id: 'interest', reason: 'interest' })
  frame('account_changed', { ...event, event_id: 'force1', reason: 'credit_liquidation' })
  frame('account_changed', { ...event, event_id: 'force2', reason: 'credit_liquidation' })
  await vi.advanceTimersByTimeAsync(200)
  expect(useNotificationStore().notifications).toHaveLength(2)
  expect(mocks.context).toHaveBeenCalledTimes(1)
})
it('requires all account floors and a fresh stream sync after epoch replacement', async () => {
  const data = context(); data.minimum_group_stamps = [fx(1, '3')]
  mocks.context.mockResolvedValueOnce(data)
  const store = useTradingSessionStore(); store.start({ product: 'fx', id: 1 }); await vi.advanceTimersByTimeAsync(0)
  frame('sync', { economic_version: '1', rules, groups: [fx(1, '2')], missing_groups: [], server_time: time })
  expect(store.ready).toBe(false)
  frame('pricing', { groups: [fx(1, '3')], missing_groups: [], server_time: time }); expect(store.ready).toBe(true)
  mocks.handlers!.disconnected!(); expect(store.ready).toBe(false)
  frame('sync', { economic_version: '1', rules, groups: [fx(1, '1', 'NEW')], missing_groups: [], server_time: time })
  expect(store.ready).toBe(false)
  mocks.context.mockResolvedValueOnce(context('1', [fx(1, '1', 'NEW')]))
  await vi.advanceTimersByTimeAsync(200); expect(store.ready).toBe(true)
  frame('pricing', { groups: [fx(1, '99', 'E')], missing_groups: [], server_time: time })
  expect(store.groups.get('fx:1')!.stamp.epoch).toBe('NEW')
})
it('uses whole portfolio for growth admission without losing known order rejections', async () => {
  const a = { ...basis, cash: '2', debt: '2', fx_wallets: [{ pair_id: 2, foreign_amount: '20', cost_basis: '0' }] }
  const store = await boot(context('1', [fx(1), fx(2)], a))
  expect(store.previewFx({ side: 'buy', amount: '1' })!.condition).toBe('satisfied')
  expect(store.previewFx({ side: 'buy', amount: '1', allow_growth: false })!.condition).toBe('rejected')
})

it('keeps the context-to-subscribe gap provisional until the sync account target is covered', async () => {
  mocks.context.mockResolvedValueOnce(context()).mockResolvedValueOnce(context('9007199254740994'))
  const store = useTradingSessionStore(); store.start({ product: 'fx', id: 1 }); await vi.advanceTimersByTimeAsync(0)
  frame('sync', { economic_version: '9007199254740994', rules: { ...rules, rule_version: 'latest', loan_enabled: false }, groups: [fx()], missing_groups: [], server_time: time })
  expect(store.ready).toBe(false)
  await vi.advanceTimersByTimeAsync(200)
  expect(store.account!.economic_version).toBe('9007199254740994')
  expect(store.rules!.rule_version).toBe('latest')
  expect(store.rules!.loan_enabled).toBe(false)
  expect(store.ready).toBe(true)
})
it('preserves genuine full-cover interest uncertainty even if whole-account margin passes', async () => {
  const data = context('1', [fx()], { ...basis, debt: '1', debt_last_accrued_at: '2025-12-31T23:59:59Z', fx_shorts: [{ pair_id: 1, principal_foreign: '1', interest_foreign: '0', interest_last_accrued_at: '2025-12-31T23:59:59Z', restricted_gold: '2', proceeds_basis_gold: '2' }] })
  data.rules = { ...rules, loan_daily_rate: '0.1' }
  const store = await boot(data)
  const owed = accrueTradingDebt('1', '2025-12-31T23:59:59Z', '0.1', store.now).amount
  const amount = quoteFx('buyExactOut', owed, fx()).input_amount
  const preview = store.previewFx({ side: 'buy', amount })!
  expect(preview.growth_amount).toBe('0.000000')
  expect(preview.account_preview!.initial_condition).toBe('satisfied')
  expect(preview.condition).toBe('unknown')
  expect(preview.unchecked_reasons).toContain('interest_exact_cover_uncertainty')
})
it('accrues from one monotonic server clock locally and never rewinds on delayed server time', async () => {
  const data = context('1', [fx()], { ...basis, debt: '1', debt_last_accrued_at: time }); data.rules = { ...rules, loan_daily_rate: '0.1' }
  const store = await boot(data)
  await vi.advanceTimersByTimeAsync(1000)
  expect(Date.parse(store.now)).toBe(Date.parse(time) + 1000)
  expect(Number(store.accountPreview!.gold_debt)).toBeGreaterThan(1)
  frame('heartbeat', { server_time: '2025-12-31T23:59:00Z' })
  expect(Date.parse(store.now)).toBe(Date.parse(time) + 1000)
  expect(mocks.context).toHaveBeenCalledTimes(1)
})

it('a departed route cannot tear down the newer route owner', async () => {
  const store = await boot()
  const previousOwner = store.start({ product: 'fx', id: 1 })
  const currentOwner = store.start({ product: 'fx', id: 1 })
  store.stop(previousOwner)
  expect(store.ready).toBe(true)
  store.stop(currentOwner)
  expect(store.account).toBeNull()
  expect(store.groups.size).toBe(0)
})

it('resynchronizes opaque rule changes without fetching account data', async () => {
  const store = await boot()
  mocks.versions.mockResolvedValueOnce({ economic_version: '1', rule_version: 'opaque-Z', group_stamps: [fx()], missing_groups: [], server_time: time })
  await store.checkVersions()
  expect(store.ready).toBe(false)
  expect(mocks.context).toHaveBeenCalledTimes(1)
  frame('sync', { economic_version: '1', rules: { ...rules, rule_version: 'opaque-Z', credit_new_risk_frozen: true }, groups: [fx()], missing_groups: [], server_time: time })
  expect(store.ready).toBe(true)
  expect(store.rules!.credit_new_risk_frozen).toBe(true)
  expect(mocks.context).toHaveBeenCalledTimes(1)
})
it('foreground pricing cannot reopen admission before delayed authority and coverage recovery', async () => {
  const store = await boot()
  let finish!: (value: unknown) => void
  mocks.versions.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  const callback = vi.mocked(document.addEventListener).mock.calls.find(([event]) => event === 'visibilitychange')![1] as () => void
  Object.assign(document, { visibilityState: 'hidden' }); Object.assign(document, { visibilityState: 'visible' }); callback()
  frame('pricing', { groups: [fx(1, '2')], missing_groups: [], server_time: time })
  await vi.advanceTimersByTimeAsync(1000)
  expect(store.ready).toBe(false)
  expect(store.previewFx({ side: 'buy', amount: '1' })!.condition).toBe('unknown')
  mocks.context.mockResolvedValueOnce(context('2', [fx(1, '2')]))
  mocks.versions.mockResolvedValue({ economic_version: '2', rule_version: rules.rule_version, group_stamps: [fx(1, '2')], missing_groups: [], server_time: time })
  finish({ economic_version: '2', rule_version: rules.rule_version, group_stamps: [fx(1, '2')], missing_groups: [], server_time: time })
  await vi.advanceTimersByTimeAsync(200)
  expect(store.account!.economic_version).toBe('2')
  expect(store.ready).toBe(true)
})
it('a forced versionless refresh survives coverage arriving for an older pending candidate', async () => {
  const data = context(); data.minimum_group_stamps = [fx(1, '2')]
  mocks.context.mockResolvedValueOnce(data)
  const store = useTradingSessionStore(); store.start({ product: 'fx', id: 1 }); await vi.advanceTimersByTimeAsync(0)
  frame('sync', { economic_version: '1', rules, groups: [fx()], missing_groups: [], server_time: time })
  mocks.context.mockResolvedValueOnce(context('2', [fx(1, '2')]))
  store.requestRefresh(undefined, 'confirmed_response_without_version', 'known-write')
  frame('pricing', { groups: [fx(1, '2')], missing_groups: [], server_time: time })
  expect(store.ready).toBe(false)
  expect(store.account).toBeNull()
  await vi.advanceTimersByTimeAsync(199); expect(mocks.context).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(1)
  expect(mocks.context).toHaveBeenCalledTimes(2)
  expect(store.account!.economic_version).toBe('2')
  expect(store.ready).toBe(true)
})

it('allows a funded short-to-long reversal despite fractional-day interest rounding', async () => {
  const data = context('1', [fx()], { ...basis, fx_shorts: [{ pair_id: 1, principal_foreign: '1', interest_foreign: '0.1', interest_last_accrued_at: time, restricted_gold: '5', proceeds_basis_gold: '5' }] })
  data.rules = { ...rules, loan_daily_rate: '0.001' }
  const store = await boot(data)
  await vi.advanceTimersByTimeAsync(60000)
  const preview = store.previewFx({ side: 'buy', amount: '150', borrow_amount: '50', allow_growth: true })!
  expect(preview.account_preview?.initial_condition).toBe('satisfied')
  expect(preview.condition).toBe('satisfied')
  expect(preview.legs.map(leg => leg.purpose)).toEqual(['short_cover', 'borrow_buy'])
  expect(preview.foreign_debt_after).toBe('0.000000')
  expect(preview.account_after.debt).toBe('50.000000')
  expect(store.account?.fx_shorts[0]?.principal_foreign).toBe('1')
  await vi.advanceTimersByTimeAsync(60000)
  const cover = store.previewFx({ side: 'buy', amount: '150', borrow_amount: '50' })!.legs[0]!
  const atBoundary = store.previewFx({ side: 'buy', amount: formatUnits(units(cover.input_amount) + 1n) })!
  expect(Number(atBoundary.growth_amount)).toBeGreaterThan(0)
  expect(atBoundary.account_preview?.initial_condition).toBe('satisfied')
  expect(atBoundary.condition).toBe('unknown')
  const tooLeveraged = store.previewFx({ side: 'buy', amount: '1000', borrow_amount: '900', allow_growth: true })!
  expect(tooLeveraged.condition).toBe('rejected')
  expect(tooLeveraged.rejected_reasons).toContain('insufficient_initial_margin')
})
