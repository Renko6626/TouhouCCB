<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { fxApi, formatFxPrice } from '@/api/fx'
import type { FxPairPublic, FxSnapshot } from '@/types/fx'

const pairs = ref<FxPairPublic[]>([])
const snapshots = ref<Record<number, FxSnapshot>>({})
const loading = ref(false)
const error = ref(false)
const updatedAt = ref('')
const statusNames: Record<string, string> = { trading: '交易中', paused: '暂停', closed: '已闭市' }

async function refresh() {
  if (loading.value) return
  loading.value = true
  error.value = false
  try {
    const list = await fxApi.listPairs()
    // 首页只展示最近六个公开品种，完整列表在交易页。
    pairs.value = [...list].sort((a, b) => Number(b.status === 'trading') - Number(a.status === 'trading') || b.id - a.id).slice(0, 6)
    const results = await Promise.allSettled(pairs.value.map(p => fxApi.getSnapshot(p.id)))
    const next: Record<number, FxSnapshot> = {}
    results.forEach(result => {
      if (result.status === 'fulfilled') next[result.value.pair.id] = result.value
    })
    snapshots.value = next
    updatedAt.value = new Date().toLocaleTimeString()
  } catch {
    error.value = true
    pairs.value = []
    snapshots.value = {}
  } finally {
    loading.value = false
  }
}

onMounted(refresh)
</script>

<template>
  <section class="fx-overview" aria-labelledby="fx-overview-title">
    <header>
      <div><h2 id="fx-overview-title">外汇行情</h2><p>金圆券 / 1 外币 · {{ updatedAt ? `快照更新于 ${updatedAt}` : '公开行情快照' }}</p></div>
      <button :disabled="loading" @click="refresh">{{ loading ? '刷新中…' : '刷新行情' }}</button>
    </header>
    <p v-if="error" class="state" role="alert">行情暂时不可用，请点击刷新重试。</p>
    <p v-else-if="loading && !pairs.length" class="state">正在读取外汇行情…</p>
    <p v-else-if="!pairs.length" class="state">暂无公开外汇品种，开市后会在这里展示。</p>
    <div v-else class="fx-pairs">
      <router-link v-for="pair in pairs" :key="pair.id" :to="{ path: '/fx', query: { pair: pair.id } }" class="fx-pair">
        <div class="pair-heading"><h3>{{ pair.currency_name }} <small>{{ pair.currency_code }}</small></h3><span>{{ statusNames[pair.status] ?? pair.status }}</span></div>
        <template v-if="snapshots[pair.id]">
          <div class="pair-price">{{ formatFxPrice(snapshots[pair.id]!.price) }}</div>
          <dl><div><dt>买入价</dt><dd>{{ formatFxPrice(snapshots[pair.id]!.buy_price) }}</dd></div><div><dt>卖出价</dt><dd>{{ formatFxPrice(snapshots[pair.id]!.sell_price) }}</dd></div></dl>
        </template>
        <p v-else class="state">该币种报价暂不可用</p>
        <span class="pair-link">查看 K 线与交易 →</span>
      </router-link>
    </div>
  </section>
</template>

<style scoped>
header, .pair-heading { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }
header { border-bottom: 2px solid #000; padding-bottom: 12px; margin-bottom: 16px; }
h2 { font-size: 20px; font-weight: 800; margin: 0; }
header p { margin: 4px 0 0; color: #666; font-size: 12px; }
button { border: 2px solid #000; background: #fff; padding: 6px 12px; cursor: pointer; }
button:disabled { opacity: .5; cursor: wait; }
.fx-pairs { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 280px), 1fr)); gap: 16px; }
.fx-pair { display: block; border: 2px solid #000; padding: 20px; color: #000; text-decoration: none; background: #fff; }
.fx-pair:hover { background: #f5f5f5; }
.fx-pair:focus-visible, button:focus-visible { outline: 3px solid #555; outline-offset: 3px; }
h3 { margin: 0; font-size: 18px; font-weight: 800; overflow-wrap: anywhere; }
small, .pair-heading > span { font-size: 12px; font-weight: 400; color: #555; }
.pair-price { font-size: clamp(26px, 4vw, 36px); font-weight: 800; font-variant-numeric: tabular-nums; margin: 16px 0; overflow-wrap: anywhere; }
dl { display: flex; flex-wrap: wrap; gap: 12px 32px; margin-bottom: 16px; font-size: 12px; }
dt { color: #666; } dd { margin: 4px 0 0; font-weight: 600; }
.pair-link { font-size: 12px; text-decoration: underline; text-underline-offset: 3px; }
.state { padding: 20px 0; color: #666; font-size: 14px; }
</style>
