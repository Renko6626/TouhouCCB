<script setup lang="ts">
const props = defineProps<{
  status: string
}>()

const getStatusText = (status: string) => {
  switch (status) {
    case 'trading': return '交易中'
    case 'halt': return '已暂停'
    case 'settled': return '已结算'
    default: return status
  }
}
</script>

<template>
  <span :class="['status-tag', `status-${props.status}`]">
    {{ getStatusText(props.status) }}
  </span>
</template>

<style scoped>
.status-tag {
  display: inline-block;
  padding: 1px 8px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  line-height: 20px;
  border: 1.5px solid #000;
}

.status-trading {
  background: #000;
  color: #fff;
}

.status-halt {
  background: #f0f0f0;
  color: #444;
  border-color: #000;
}

.status-settled {
  background: #fff;
  color: #888;
  border-color: #888;
}
</style>
