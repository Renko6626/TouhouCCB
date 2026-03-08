<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useNotificationStore } from '@/stores/notification'
import { NForm, NFormItem, NInput, NButton, NAlert } from 'naive-ui'

const router = useRouter()
const authStore = useAuthStore()
const notificationStore = useNotificationStore()

// 表单数据
const formData = ref({
  username: '',
  password: '',
  confirmPassword: '',
  activationCode: ''
})

const loading = ref(false)
const error = ref('')

// 表单验证规则
const validatePassword = (rule: any, value: string, callback: Function) => {
  if (!value) {
    callback(new Error('请输入密码'))
  } else if (value.length < 6) {
    callback(new Error('密码长度至少6位'))
  } else {
    callback()
  }
}

const validateConfirmPassword = (rule: any, value: string, callback: Function) => {
  if (!value) {
    callback(new Error('请确认密码'))
  } else if (value !== formData.value.password) {
    callback(new Error('两次输入的密码不一致'))
  } else {
    callback()
  }
}

const rules = {
  username: {
    required: true,
    message: '请输入用户名',
    trigger: 'blur'
  },
  password: {
    required: true,
    validator: validatePassword,
    trigger: 'blur'
  },
  confirmPassword: {
    required: true,
    validator: validateConfirmPassword,
    trigger: 'blur'
  },
  activationCode: {
    required: true,
    message: '请输入激活码',
    trigger: 'blur'
  }
}

// 处理注册
const handleRegister = async () => {
  error.value = ''
  
  if (!formData.value.username || !formData.value.password || !formData.value.activationCode) {
    error.value = '请填写所有必填项'
    return
  }

  if (formData.value.password !== formData.value.confirmPassword) {
    error.value = '两次输入的密码不一致'
    return
  }

  loading.value = true
  try {
    await authStore.register({
      username: formData.value.username,
      password: formData.value.password,
      activation_code: formData.value.activationCode
    })
    
    notificationStore.addNotification({
      type: 'success',
      title: '注册成功',
      message: '账号注册成功，请登录'
    })
    
    // 跳转到登录页
    router.push('/auth/login')
  } catch (err: any) {
    error.value = err.response?.data?.detail || '注册失败，请检查输入信息'
    notificationStore.addNotification({
      type: 'error',
      title: '注册失败',
      message: error.value
    })
  } finally {
    loading.value = false
  }
}

// 处理回车键
const handleKeyPress = (event: KeyboardEvent) => {
  if (event.key === 'Enter') {
    handleRegister()
  }
}
</script>

<template>
  <div class="register-page">
    <div class="max-w-md mx-auto">
      <!-- 页面标题 -->
      <div class="text-center mb-8">
        <h1 class="text-3xl font-bold text-gray-800 dark:text-gray-200 mb-2">
          注册账号
        </h1>
        <p class="text-gray-600 dark:text-gray-400">
          创建新账号，开始您的预测市场交易之旅
        </p>
      </div>

      <!-- 错误提示 -->
      <div v-if="error" class="mb-6">
        <NAlert type="error" :title="error" />
      </div>

      <!-- 注册表单 -->
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
              placeholder="请输入密码（至少6位）"
              size="large"
              :disabled="loading"
              show-password-on="click"
            >
              <template #prefix>
                <i class="i-mdi-lock"></i>
              </template>
            </NInput>
          </NFormItem>

          <!-- 确认密码 -->
          <NFormItem label="确认密码" path="confirmPassword">
            <NInput
              v-model:value="formData.confirmPassword"
              type="password"
              placeholder="请再次输入密码"
              size="large"
              :disabled="loading"
              show-password-on="click"
            >
              <template #prefix>
                <i class="i-mdi-lock-check"></i>
              </template>
            </NInput>
          </NFormItem>

          <!-- 激活码 -->
          <NFormItem label="激活码" path="activationCode">
            <NInput
              v-model:value="formData.activationCode"
              placeholder="请输入激活码"
              size="large"
              :disabled="loading"
            >
              <template #prefix>
                <i class="i-mdi-key"></i>
              </template>
            </NInput>
          </NFormItem>

          <!-- 注册按钮 -->
          <div class="pt-4">
            <NButton
              type="primary"
              size="large"
              :loading="loading"
              @click="handleRegister"
              class="w-full"
            >
              {{ loading ? '注册中...' : '注册' }}
            </NButton>
          </div>
        </div>
      </NForm>

      <!-- 已有账号提示 -->
      <div class="mt-6 text-center">
        <router-link 
          to="/auth/login" 
          class="text-primary-600 hover:text-primary-700 dark:text-primary-400 dark:hover:text-primary-300"
        >
          已有账号？立即登录
        </router-link>
      </div>

      <!-- 返回首页 -->
      <div class="mt-4 text-center">
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
.register-page {
  max-width: 400px;
  margin: 0 auto;
  padding: 2rem 1rem;
}
</style>