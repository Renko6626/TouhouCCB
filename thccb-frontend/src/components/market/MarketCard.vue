<script setup lang="ts">
import { computed } from 'vue'
import type { MarketListItem } from '@/types/api'

const props = defineProps<{
  market: MarketListItem
}>()

const emit = defineEmits<{
  view: [id: number]
  trade: [id: number]
}>()

const statusText: Record<string, string> = {
  trading: '交易中',
  halt: '已暂停',
  settled: '已结算',
}

const statusClass: Record<string, string> = {
  trading: 'status-trading',
  halt: 'status-halt',
  settled: 'status-settled',
}

// 最多展示前4个选项
const displayOutcomes = props.market.outcomes?.slice(0, 4) ?? []
const hiddenCount = (props.market.outcomes?.length ?? 0) - displayOutcomes.length

// 截止时间倒计时
const closesAtLabel = computed(() => {
  if (!props.market.closes_at) return null
  const closes = new Date(props.market.closes_at)
  const now = new Date()
  const diff = closes.getTime() - now.getTime()
  if (diff <= 0) return '已截止'
  const hours = Math.floor(diff / 3600000)
  const days = Math.floor(hours / 24)
  if (days > 0) return `${days}天${hours % 24}时后截止`
  if (hours > 0) return `${hours}时${Math.floor((diff % 3600000) / 60000)}分后截止`
  return `${Math.floor(diff / 60000)}分后截止`
})
</script>

<template>
  <div class="market-card">
    <!-- 卡片头部 -->
    <div class="card-header">
      <h3 class="card-title" :title="props.market.title">{{ props.market.title }}</h3>
      <span :class="['status-tag', statusClass[props.market.status] ?? 'status-settled']">
        {{ statusText[props.market.status] ?? props.market.status }}
      </span>
    </div>

    <!-- 标签 + 截止时间 -->
    <div class="card-info-bar" v-if="(props.market.tags && props.market.tags.length) || closesAtLabel">
      <div class="tags-row" v-if="props.market.tags && props.market.tags.length">
        <span v-for="tag in props.market.tags" :key="tag" class="market-tag">{{ tag }}</span>
      </div>
      <span v-if="closesAtLabel" class="closes-at-label">{{ closesAtLabel }}</span>
    </div>

    <!-- 描述 -->
    <p class="card-desc">{{ props.market.description || '暂无描述' }}</p>

    <!-- 概率条 -->
    <div class="outcomes-section" v-if="displayOutcomes.length">
      <div v-for="outcome in displayOutcomes" :key="outcome.id" class="outcome-row">
        <div class="outcome-meta">
          <span class="outcome-label">{{ outcome.label }}</span>
          <span class="outcome-price-row">
            <span class="outcome-pct">{{ (outcome.current_price * 100).toFixed(1) }}%</span>
            <span
              v-if="outcome.price_change_pct_24h != null"
              :class="['price-change', outcome.price_change_pct_24h > 0 ? 'up' : outcome.price_change_pct_24h < 0 ? 'down' : '']"
            >
              {{ outcome.price_change_pct_24h > 0 ? '+' : '' }}{{ outcome.price_change_pct_24h.toFixed(1) }}%
            </span>
          </span>
        </div>
        <div class="prob-track">
          <div class="prob-fill" :style="{ width: `${outcome.current_price * 100}%` }"></div>
        </div>
      </div>
      <div v-if="hiddenCount > 0" class="outcomes-more">+{{ hiddenCount }} 个选项</div>
    </div>

    <!-- 底部信息 + 操作 -->
    <div class="card-footer">
      <div class="card-meta">
        <span class="meta-item">
          <span class="meta-label">流动性</span>
          <span class="meta-value">¥{{ props.market.liquidity_b.toLocaleString() }}</span>
        </span>
        <span class="meta-item">
          <span class="meta-label">选项</span>
          <span class="meta-value">{{ props.market.outcomes?.length ?? 0 }}</span>
        </span>
      </div>
      <div class="card-actions">
        <button class="btn-detail" @click="emit('view', props.market.id)">详情</button>
        <button
          v-if="props.market.status === 'trading'"
          class="btn-trade"
          @click="emit('trade', props.market.id)"
        >
          交易
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.market-card {
  border: 2px solid #000000;
  background: #ffffff;
  box-shadow: 4px 4px 0 #000000;
  display: flex;
  flex-direction: column;
  transition: box-shadow 0.1s ease, transform 0.1s ease;
}

.market-card:hover {
  box-shadow: 6px 6px 0 #000000;
  transform: translate(-1px, -1px);
}

/* 头部 */
.card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  padding: 14px 16px 10px;
  border-bottom: 1px solid #e0e0e0;
}

.card-title {
  font-size: 14px;
  font-weight: 700;
  color: #000000;
  line-height: 1.4;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  flex: 1;
}

/* 状态标签 */
.status-tag {
  flex-shrink: 0;
  font-size: 11px;
  font-weight: 600;
  padding: 2px 7px;
  border: 1.5px solid #000000;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  white-space: nowrap;
}

.status-trading {
  background: #000000;
  color: #ffffff;
}

.status-halt {
  background: #f0f0f0;
  color: #444444;
}

.status-settled {
  background: #ffffff;
  color: #888888;
  border-color: #888888;
}

/* 标签 + 截止 */
.card-info-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 16px 0;
  flex-wrap: wrap;
}

.tags-row {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.market-tag {
  display: inline-block;
  padding: 0 6px;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  border: 1px solid #000;
  background: #f5f5f5;
  color: #333;
}

.closes-at-label {
  font-size: 10px;
  font-weight: 600;
  color: #555;
  letter-spacing: 0.02em;
  white-space: nowrap;
}

/* 描述 */
.card-desc {
  padding: 10px 16px;
  font-size: 12px;
  color: #555555;
  line-height: 1.5;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  flex: 1;
}

/* 概率条 */
.outcomes-section {
  padding: 0 16px 10px;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.outcome-row {}

.outcome-meta {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 3px;
}

.outcome-label {
  font-size: 12px;
  color: #333333;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 70%;
}

.outcome-price-row {
  display: flex;
  align-items: baseline;
  gap: 5px;
}

.outcome-pct {
  font-size: 12px;
  font-weight: 700;
  color: #000000;
  font-variant-numeric: tabular-nums;
}

.price-change {
  font-size: 10px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: #888;
}

.price-change.up {
  color: var(--color-up);
}

.price-change.down {
  color: var(--color-down);
}

.prob-track {
  height: 6px;
  background: #f0f0f0;
  border: 1px solid #cccccc;
}

.prob-fill {
  height: 100%;
  background: #000000;
  transition: width 0.3s ease;
  min-width: 2px;
}

.outcomes-more {
  font-size: 11px;
  color: #999999;
  margin-top: 2px;
}

/* 底部 */
.card-footer {
  border-top: 1px solid #e0e0e0;
  padding: 10px 16px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: auto;
}

.card-meta {
  display: flex;
  gap: 16px;
}

.meta-item {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.meta-label {
  font-size: 10px;
  color: #888888;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.meta-value {
  font-size: 13px;
  font-weight: 700;
  color: #000000;
  font-variant-numeric: tabular-nums;
}

/* 按钮 */
.card-actions {
  display: flex;
  gap: 6px;
}

.btn-detail,
.btn-trade {
  padding: 5px 12px;
  font-size: 12px;
  font-weight: 600;
  border: 1.5px solid #000000;
  cursor: pointer;
  transition: background 0.1s, color 0.1s;
  letter-spacing: 0.02em;
}

.btn-detail {
  background: #ffffff;
  color: #000000;
  box-shadow: 2px 2px 0 #000000;
}

.btn-detail:hover {
  background: #f0f0f0;
}

.btn-trade {
  background: #000000;
  color: #ffffff;
  box-shadow: 2px 2px 0 #444444;
}

.btn-trade:hover {
  background: #222222;
}
</style>
