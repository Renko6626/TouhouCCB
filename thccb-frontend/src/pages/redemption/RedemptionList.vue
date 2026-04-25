<script setup lang="ts">
import { onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useRedemptionStore } from '@/stores/redemption'
import type { BatchListItem, PartnerPublic } from '@/types/redemption'

const store = useRedemptionStore()
const router = useRouter()

onMounted(() => store.loadBatches())

interface PartnerGroup {
  partner: PartnerPublic
  items: BatchListItem[]
}

const grouped = computed<PartnerGroup[]>(() => {
  const map = new Map<number, PartnerGroup>()
  for (const b of store.batches) {
    if (!map.has(b.partner.id)) {
      map.set(b.partner.id, { partner: b.partner, items: [] })
    }
    map.get(b.partner.id)!.items.push(b)
  }
  return Array.from(map.values())
})

const goDetail = (id: number) => router.push(`/redemption/batches/${id}`)
</script>

<template>
  <div class="page">
    <h1 class="page-title">兑换中心</h1>
    <p class="page-hint">用 TouhouCCB 资金兑换合作方网站的码。码可在「我的兑换」中永久查看。</p>

    <div v-if="store.loading" class="loading">加载中…</div>
    <div v-else-if="grouped.length === 0" class="empty">暂无可兑换批次</div>

    <section v-for="g in grouped" :key="g.partner.id" class="partner-section">
      <header class="partner-header">
        <img v-if="g.partner.logo_url" :src="g.partner.logo_url" class="partner-logo" :alt="g.partner.name" />
        <div class="partner-meta">
          <h2>{{ g.partner.name }}</h2>
          <p v-if="g.partner.description">{{ g.partner.description }}</p>
          <a v-if="g.partner.website_url" :href="g.partner.website_url" target="_blank" rel="noopener">
            访问站点 →
          </a>
        </div>
      </header>
      <div class="batch-grid">
        <button
          v-for="b in g.items"
          :key="b.id"
          class="batch-card"
          @click="goDetail(b.id)"
        >
          <div class="batch-name">{{ b.name }}</div>
          <div class="batch-price">{{ b.unit_price }}</div>
          <div class="batch-stock">剩余 {{ b.available_count }}</div>
        </button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.page { padding: 16px; }
.page-title { font-size: 22px; font-weight: 700; margin-bottom: 4px; }
.page-hint { color: #666; font-size: 13px; margin-bottom: 24px; }
.loading, .empty { color: #999; padding: 32px; text-align: center; }
.partner-section {
  border: 2px solid #000; padding: 16px; margin-bottom: 24px; background: #fff;
  box-shadow: 6px 6px 0 #000;
}
.partner-header { display: flex; gap: 12px; align-items: flex-start; margin-bottom: 12px; }
.partner-logo {
  width: 48px; height: 48px; object-fit: cover; border: 2px solid #000; flex-shrink: 0;
}
.partner-meta h2 { font-size: 18px; font-weight: 700; }
.partner-meta p { font-size: 13px; color: #555; margin: 4px 0; }
.partner-meta a { font-size: 12px; color: #000; text-decoration: underline; }
.batch-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 8px;
}
.batch-card {
  border: 2px solid #000; background: #fff; padding: 12px;
  text-align: left; cursor: pointer; font-family: inherit;
  box-shadow: 2px 2px 0 #000;
  transition: background 0.1s, color 0.1s, transform 0.1s, box-shadow 0.1s;
}
.batch-card:hover {
  background: #000; color: #fff;
  transform: translate(-1px, -1px);
  box-shadow: 4px 4px 0 #000;
}
.batch-name { font-weight: 600; margin-bottom: 6px; }
.batch-price { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; }
.batch-stock { font-size: 12px; opacity: 0.7; margin-top: 4px; font-variant-numeric: tabular-nums; }
</style>
