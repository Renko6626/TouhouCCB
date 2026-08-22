<script setup lang="ts">
/**
 * 批量操作共用的用户筛选表单。v-model 一个 UserFilterState（全部字符串/枚举，
 * 由 toApiFilter() 转成后端 BatchAdjustFilter，空串 → 不过滤）。
 */

import type { UserFilterState } from './userFilter'

const model = defineModel<UserFilterState>({ required: true })
</script>

<template>
  <div class="filter-grid">
    <label class="field">
      <span class="field-label">用户 ID 起（含）</span>
      <input v-model="model.userIdMin" type="text" inputmode="numeric" class="input tabular-nums" placeholder="不限" />
    </label>
    <label class="field">
      <span class="field-label">用户 ID 止（含）</span>
      <input v-model="model.userIdMax" type="text" inputmode="numeric" class="input tabular-nums" placeholder="不限" />
    </label>
    <label class="field">
      <span class="field-label">现金 ≥</span>
      <input v-model="model.cashMin" type="text" inputmode="decimal" class="input tabular-nums" placeholder="不限" />
    </label>
    <label class="field">
      <span class="field-label">现金 ≤</span>
      <input v-model="model.cashMax" type="text" inputmode="decimal" class="input tabular-nums" placeholder="不限" />
    </label>
    <label class="field">
      <span class="field-label">债务 ≥</span>
      <input v-model="model.debtMin" type="text" inputmode="decimal" class="input tabular-nums" placeholder="不限" />
    </label>
    <label class="field">
      <span class="field-label">债务 ≤</span>
      <input v-model="model.debtMax" type="text" inputmode="decimal" class="input tabular-nums" placeholder="不限" />
    </label>
    <label class="field">
      <span class="field-label">账号状态</span>
      <select v-model="model.isActive" class="input">
        <option value="all">不限</option>
        <option value="active">活跃</option>
        <option value="inactive">封禁</option>
      </select>
    </label>
    <label class="field field--checkbox">
      <input v-model="model.includeSuperuser" type="checkbox" class="checkbox" />
      <span class="field-label">包含超管（默认排除自己）</span>
    </label>
  </div>
</template>

<style scoped src="@/pages/admin/batch/_batch.css"></style>
