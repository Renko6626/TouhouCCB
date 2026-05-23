<script setup lang="ts">
// 监听 axios interceptor 派发的 'market-title-required' 事件 → 全局 toast 提示。
// 必须 mount 在 NMessageProvider 内部，useMessage() 才能拿到上下文。
import { onMounted, onBeforeUnmount } from 'vue'
import { useMessage } from 'naive-ui'

const message = useMessage()

function onTitleRequired() {
  message.warning('此市场需要特定称号才能交易', { duration: 4000 })
}

onMounted(() => {
  window.addEventListener('market-title-required', onTitleRequired)
})

onBeforeUnmount(() => {
  window.removeEventListener('market-title-required', onTitleRequired)
})
</script>

<template>
  <!-- 无可视 UI；仅在 mount 期间订阅全局事件，渲染一个空注释节点占位 -->
  <span class="market-gate-toast-anchor" aria-hidden="true" style="display: none;"></span>
</template>
