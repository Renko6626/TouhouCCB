<script setup lang="ts">
import { ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { NMenu } from 'naive-ui'

interface Props {
  collapsed: boolean
}

const props = defineProps<Props>()

const router = useRouter()
const route = useRoute()

const activeKey = ref(route.path)

// 菜单选项
const menuOptions = [
  {
    label: '首页',
    key: '/',
    icon: () => '🏠'
  },
  {
    label: '市场列表',
    key: '/market/list',
    icon: () => '📈'
  },
  {
    label: '我的资产',
    key: '/user/portfolio',
    icon: () => '💰'
  }
]

// 处理菜单点击
const handleMenuClick = (key: string) => {
  router.push(key)
  activeKey.value = key
}
</script>

<template>
  <div class="app-sidebar">
    <NMenu
      :collapsed="props.collapsed"
      :collapsed-width="64"
      :collapsed-icon-size="22"
      :options="menuOptions"
      :value="activeKey"
      @update:value="handleMenuClick"
    />
  </div>
</template>

<style scoped>
.app-sidebar {
  height: 100%;
  padding: 16px 0;
}
</style>