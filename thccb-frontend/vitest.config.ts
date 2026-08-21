// 独立于 vite.config.ts（高敏感文件不动）；只跑纯函数单测，node 环境足够
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  test: {
    include: ['src/**/__tests__/*.spec.ts'],
    environment: 'node',
  },
})
