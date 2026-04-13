<script setup lang="ts">
import { useRoute } from 'vue-router'

const route = useRoute()
const pageTitle = route.meta?.title as string | undefined
</script>

<template>
  <div class="auth-root">
    <!-- 背景网格纹理 -->
    <div class="auth-bg-grid"></div>

    <div class="auth-wrapper">
      <!-- 品牌区 -->
      <div class="auth-brand">
        <router-link to="/" class="brand-link">
          <div class="brand-logo">T</div>
          <span class="brand-name">东方炒炒币</span>
        </router-link>
        <p class="brand-tagline">预测市场交易平台</p>
      </div>

      <!-- 认证卡片 -->
      <div class="auth-card">
        <!-- 卡片标题栏 -->
        <div v-if="pageTitle" class="auth-card-header">
          <span class="auth-card-title">{{ pageTitle }}</span>
        </div>

        <!-- 页面内容 -->
        <div class="auth-card-body">
          <slot>
            <router-view />
          </slot>
        </div>

        <!-- 底部导航链接 -->
        <div class="auth-card-footer">
          <router-link v-if="route.name !== 'login'" to="/auth/login" class="auth-link">
            已有账号？立即登录
          </router-link>
          <router-link v-if="route.name !== 'register'" to="/auth/register" class="auth-link">
            没有账号？立即注册
          </router-link>
          <router-link to="/" class="auth-link auth-link-muted">← 返回首页</router-link>
        </div>
      </div>

      <!-- 底部版权 -->
      <div class="auth-footer">
        © {{ new Date().getFullYear() }} 东方炒炒币
      </div>
    </div>
  </div>
</template>

<style scoped>
.auth-root {
  min-height: 100vh;
  background: #ffffff;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
}

/* 细密网格背景 */
.auth-bg-grid {
  position: fixed;
  inset: 0;
  pointer-events: none;
  background-image:
    linear-gradient(rgba(0,0,0,0.04) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0,0,0,0.04) 1px, transparent 1px);
  background-size: 24px 24px;
  z-index: 0;
}

.auth-wrapper {
  position: relative;
  z-index: 1;
  width: 100%;
  max-width: 420px;
  padding: 32px 16px;
  display: flex;
  flex-direction: column;
  gap: 0;
}

/* 品牌区 */
.auth-brand {
  margin-bottom: 28px;
}

.brand-link {
  display: flex;
  align-items: center;
  gap: 10px;
  text-decoration: none;
  margin-bottom: 6px;
}

.brand-logo {
  width: 40px;
  height: 40px;
  background: #000000;
  color: #ffffff;
  font-weight: 700;
  font-size: 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.brand-name {
  font-size: 22px;
  font-weight: 700;
  color: #000000;
  letter-spacing: 0.02em;
}

.brand-tagline {
  font-size: 13px;
  color: #666666;
  margin-left: 50px;
}

/* 认证卡片 */
.auth-card {
  border: 4px solid #000000;
  background: #ffffff;
  box-shadow: 8px 8px 0 #000000;
}

.auth-card-header {
  border-bottom: 2px solid #000000;
  padding: 14px 24px;
  background: #000000;
}

.auth-card-title {
  font-size: 14px;
  font-weight: 700;
  color: #ffffff;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

.auth-card-body {
  padding: 28px 24px 20px;
}

.auth-card-footer {
  border-top: 2px solid #000000;
  padding: 14px 24px;
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  background: #f5f5f5;
}

.auth-link {
  font-size: 13px;
  color: #000000;
  text-decoration: underline;
  text-underline-offset: 3px;
  font-weight: 500;
}

.auth-link:hover {
  color: #333333;
}

.auth-link-muted {
  margin-left: auto;
  color: #666666;
  font-weight: 400;
}

.auth-footer {
  margin-top: 20px;
  text-align: center;
  font-size: 12px;
  color: #999999;
}

@media (max-width: 480px) {
  .auth-wrapper {
    padding: 24px 12px;
  }
  .auth-card-body {
    padding: 20px 16px 16px;
  }
  .auth-card-footer {
    padding: 12px 16px;
  }
}
</style>
