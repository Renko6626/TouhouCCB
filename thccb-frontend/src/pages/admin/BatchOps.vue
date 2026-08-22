<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import BatchAdjustCashPanel from './batch/BatchAdjustCashPanel.vue'
import AmnestyPanel from './batch/AmnestyPanel.vue'

type Tab = 'cash' | 'amnesty'
const TABS: { key: Tab; label: string; hint: string }[] = [
  { key: 'cash', label: '批量发钱', hint: '按筛选加减现金' },
  { key: 'amnesty', label: '大赦天下', hint: '清债 + 现金还原' },
]

const route = useRoute()
const router = useRouter()
const tab = computed<Tab>(() => (route.query.tab === 'amnesty' ? 'amnesty' : 'cash'))
function go(t: Tab) {
  router.replace({ query: t === 'cash' ? {} : { tab: t } })
}
</script>

<template>
  <div class="page">
    <header class="page-header">
      <h1 class="page-title">批量操作</h1>
      <p class="page-sub">对一批用户的资金/债务做整体调整。所有操作先 dry-run 预览，再二次确认执行，逐人写审计流水。</p>
    </header>

    <nav class="tabs" role="tablist">
      <button
        v-for="t in TABS" :key="t.key"
        role="tab" :aria-selected="tab === t.key"
        class="tab" :class="{ 'tab--active': tab === t.key, 'tab--danger': t.key === 'amnesty' }"
        @click="go(t.key)"
      >
        <span class="tab-label">{{ t.label }}</span>
        <span class="tab-hint">{{ t.hint }}</span>
      </button>
    </nav>

    <BatchAdjustCashPanel v-if="tab === 'cash'" />
    <AmnestyPanel v-else />
  </div>
</template>

<style scoped>
.page {
  max-width: 1080px;
  margin: 0 auto;
  padding: 16px 0;
}
.page-header {
  margin-bottom: 16px;
}
.page-title {
  font-size: 22px;
  font-weight: 800;
  letter-spacing: 0.02em;
}
.page-sub {
  margin-top: 4px;
  font-size: 13px;
  color: #666;
}
.tabs {
  display: flex;
  gap: 0;
  margin-bottom: 20px;
  border: 2px solid #000;
  background: #fff;
}
.tab {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  padding: 10px 16px;
  background: #fff;
  border: none;
  border-right: 2px solid #000;
  cursor: pointer;
  font-family: inherit;
  text-align: left;
}
.tab:last-child {
  border-right: none;
}
.tab-label {
  font-size: 13px;
  font-weight: 800;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.tab-hint {
  font-size: 11px;
  color: #777;
}
.tab--active {
  background: #000;
  color: #fff;
}
.tab--active .tab-hint {
  color: #bbb;
}
.tab--danger.tab--active {
  background: #b91c1c;
}
</style>
