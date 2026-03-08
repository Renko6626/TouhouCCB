import './assets/main.css'
import 'virtual:uno.css'

import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createDiscreteApi } from 'naive-ui'

import App from './App.vue'
import router from './router'

const app = createApp(App)

// 配置Naive-UI的离散组件API
const { message, notification, dialog, loadingBar } = createDiscreteApi([
  'message',
  'notification',
  'dialog',
  'loadingBar'
])

// 将Naive-UI组件挂载到全局属性
app.config.globalProperties.$message = message
app.config.globalProperties.$notification = notification
app.config.globalProperties.$dialog = dialog
app.config.globalProperties.$loadingBar = loadingBar

app.use(createPinia())
app.use(router)

app.mount('#app')
