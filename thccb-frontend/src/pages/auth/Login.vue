<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useNotificationStore } from '@/stores/notification'
import { NForm, NFormItem, NInput, NButton, NCheckbox, NAlert } from 'naive-ui'

const router = useRouter()
const authStore = useAuthStore()
const notificationStore = useNotificationStore()

// 表单数据
const formData = ref({
  username: '',
  password: '',
  remember: false
})

const loading = ref(false)
const error = ref('')

// 表单验证规则
const rules = {
  username: {
    required: true,
    message: '请输入用户名',
    trigger: 'blur'
  },
  password: {
    required: true,
    message: '请输入密码',
    trigger: 'blur'
  }
}

// 处理登录
const handleLogin = async () => {
  error.value = ''
  
  if (!formData.value.username || !formData.value.password) {
    error.value = '请输入用户名和密码'
    return
  }

  loading.value = true
  try {
    await authStore.login({
      username: formData.value.username,
      password: formData.value.password
    })
    
    notificationStore.addNotification({
      type: 'success',
      title: '登录成功',
      message: '欢迎回来！'
    })
    
    // 跳转到首页或重定向页面
    const redirect = router.currentRoute.value.query.redirect as string
    router.push(redirect || '/')
  } catch (err: any) {
    error.value = err.response?.data?.detail || '登录失败，请检查用户名和密码'
    notificationStore.addNotification({
      type: 'error',
      title: '登录失败',
      message: error.value
    })
  } finally {
    loading.value = false
  }
}

// 处理回车键
const handleKeyPress = (event: KeyboardEvent) => {
  if (event.key === 'Enter') {
    handleLogin()
  }
}
</script>

<template>
  <div class="login-page">
    <div class="max-w-md mx-auto">
      <!-- 页面标题 -->
      <div class="text-center mb-8">
        <h1 class="text-3xl font-bold text-gray-800 dark:text-gray-200 mb-2">
          登录账号
        </h1>
        <p class="text-gray-600 dark:text-gray-400">
          请输入您的用户名和密码登录系统
        </p>
      </div>

      <!-- 错误提示 -->
      <div v-if="error" class="mb-6">
        <NAlert type="error" :title="error" />
      </div>

      <!-- 登录表单 -->
      <NForm
        ref="formRef"
        :model="formData"
        :rules="rules"
        @keypress.enter="handleKeyPress"
      >
        <div class="space-y-4">
          <!-- 用户名 -->
          <NFormItem label="用户名" path="username">
            <NInput
              v-model:value="formData.username"
              placeholder="请输入用户名"
              size="large"
              :disabled="loading"
            >
              <template #prefix>
                <i class="i-mdi-account"></i>
              </template>
            </NInput>
          </NFormItem>

          <!-- 密码 -->
          <NFormItem label="密码" path="password">
            <NInput
              v-model:value="formData.password"
              type="password"
              placeholder="请输入密码"
              size="large"
              :disabled="loading"
              show-password-on="click"
            >
              <template #prefix>
                <i class="i-mdi-lock"></i>
              </template>
            </NInput>
          </NFormItem>

          <!-- 记住我 -->
          <div class="flex justify-between items-center">
            <NCheckbox v-model:checked="formData.remember" :disabled="loading">
              记住我
            </NCheckbox>
            
            <router-link 
              to="/auth/register" 
              class="text-sm text-primary-600 hover:text-primary-700 dark:text-primary-400 dark:hover:text-primary-300"
            >
              没有账号？立即注册
            </router-link>
          </div>

          <!-- 登录按钮 -->
          <div class="pt-4">
            <NButton
              type="primary"
              size="large"
              :loading="loading"
              @click="handleLogin"
              class="w-full"
            >
              {{ loading ? '登录中...' : '登录' }}
            </NButton>
          </div>
        </div>
      </NForm>

      <!-- 测试账号提示 -->
      <div class="mt-8">
        <NAlert type="info">
          <template #icon>
            <i class="i-mdi-information"></i>
          </template>
          <div class="text-sm">
            <div class="font-medium mb-1">测试账号:</div>
            <div class="space-y-1">
              <div>用户名: <code class="bg-gray-100 dark:bg-gray-800 px-2 py-1 rounded">testuser</code></div>
              <div>密码: <code class="bg-gray-100 dark:bg-gray-800 px-2 py-1 rounded">testpassword</code></div>
            </div>
          </div>
        </NAlert>
      </div>

      <!-- 返回首页 -->
      <div class="mt-6 text-center">
        <router-link 
          to="/" 
          class="text-gray-600 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-300"
        >
          <i class="i-mdi-arrow-left mr-1"></i>
          返回首页
        </router-link>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  max-width: 400px;
  margin: 0 auto;
  padding: 2rem 1rem;
}

code {
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  font-size: 0.9em;
}
</style>