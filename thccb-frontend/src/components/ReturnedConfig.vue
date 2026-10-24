<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NForm, NFormItem, NInput, NSwitch, NTag, useDialog, useMessage } from 'naive-ui'
import { returnedAdminApi, type ReturnedConfig } from '@/api/returned'

const props = defineProps<{ disabled?: boolean }>()
const emit = defineEmits<{ dirty: [value: boolean]; saving: [value: boolean] }>()
const msg = useMessage()
const dialog = useDialog()
const saved = ref<ReturnedConfig | null>(null)
const enabled = ref(false)
const url = ref('')
const secret = ref('')
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const locked = computed(() => props.disabled || loading.value || saving.value || !!saved.value?.active_round_key)
const dirty = computed(() => saved.value != null && (enabled.value !== saved.value.enabled
  || url.value !== saved.value.returned_url || secret.value.length > 0))
watch(dirty, value => emit('dirty', value))

function install(value: ReturnedConfig) {
  saved.value = value
  enabled.value = value.enabled
  url.value = value.returned_url
  secret.value = ''
}

async function load() {
  loading.value = true
  error.value = ''
  try { install(await returnedAdminApi.getConfig()) }
  catch (e) { error.value = (e as Error).message || '无法读取联动配置，请重试' }
  finally { loading.value = false }
}

async function save() {
  if (locked.value) return
  saving.value = true
  emit('saving', true)
  error.value = ''
  try {
    install(await returnedAdminApi.saveConfig({ enabled: enabled.value, returned_url: url.value, secret: secret.value }))
    msg.success('逆转录者联动配置已保存并生效')
  } catch (e) { error.value = (e as Error).message || '保存失败，请重试' }
  finally { saving.value = false; emit('saving', false) }
}

function generate() {
  secret.value = Array.from(crypto.getRandomValues(new Uint8Array(32)), b => b.toString(16).padStart(2, '0')).join('')
}

async function copy() {
  try { await navigator.clipboard.writeText(secret.value); msg.success('已复制新密钥，请粘贴到逆转录者控制器') }
  catch { msg.warning('无法自动复制，请显示密钥后手动复制') }
}

function cancelRound() {
  const key = saved.value?.active_round_key
  if (!key || props.disabled || saving.value) return
  dialog.warning({
    title: '取消当前竞猜',
    content: '这会取消当前竞猜，并将已投入的金圆券原额退回。已结算的轮次不会被撤销。',
    positiveText: '取消竞猜并退款', negativeText: '保留竞猜',
    onPositiveClick: async () => {
      saving.value = true
      emit('saving', true)
      try {
        await returnedAdminApi.cancelRound(key)
        await load()
        msg.success('竞猜已取消，投入已退回')
      } catch (e) { error.value = (e as Error).message || '取消失败，请刷新状态后重试' }
      finally { saving.value = false; emit('saving', false) }
    },
  })
}

onMounted(load)
</script>

<template>
  <section class="returned-config">
    <div class="config-heading">
      <h2>逆转录者观众竞猜</h2>
      <NButton size="small" :loading="loading" :disabled="saving || disabled" @click="load">刷新状态</NButton>
    </div>
    <p>在这里保存联动设置，并在逆转录者控制器填写相同密钥。保存后立即生效。</p>
    <NAlert v-if="error" type="error" :show-icon="false">{{ error }}</NAlert>
    <NAlert v-if="saved?.active_round_key" type="warning" :show-icon="false">
      当前竞猜 {{ saved.active_round_key }} 尚未结束，请在逆转录者结算或取消后再调整配置。
      <div class="cancel-action">
        <NButton size="small" :loading="saving" :disabled="disabled || loading" @click="cancelRound">取消当前竞猜并退款</NButton>
      </div>
    </NAlert>
    <NForm label-placement="top" :disabled="locked || !saved" class="config-form">
      <NFormItem label="启用观众竞猜">
        <NSwitch v-model:value="enabled" />
        <span class="switch-label">{{ enabled ? '启用' : '关闭' }}</span>
      </NFormItem>
      <NFormItem label="逆转录者地址">
        <NInput v-model:value="url" placeholder="https://returned.example.com（可选，用于打开控制器）" />
      </NFormItem>
      <NFormItem label="联动密钥">
        <div class="secret-field">
          <NInput v-model:value="secret" type="password" show-password-on="click" autocomplete="new-password"
            :placeholder="saved?.secret_configured ? '已配置，留空保留原密钥' : '输入至少 32 个字符，或生成新密钥'" />
          <div class="secret-actions">
            <NButton size="small" @click="generate">生成新密钥</NButton>
            <NButton size="small" :disabled="!secret" @click="copy">复制新密钥</NButton>
            <NTag size="small" :type="saved?.secret_configured ? 'success' : 'default'">
              {{ saved?.secret_configured ? '已配置' : '未配置' }}
            </NTag>
          </div>
        </div>
      </NFormItem>
      <div class="config-actions">
        <NButton type="primary" :loading="saving" :disabled="locked || !dirty" @click="save">保存联动配置</NButton>
        <a v-if="saved?.returned_url" :href="saved.returned_url" target="_blank" rel="noopener noreferrer">打开逆转录者</a>
      </div>
    </NForm>
    <p class="config-note">每轮投入 100 金圆券；误差不超过 3.2、11.2、20、30 个百分点时，分别到账 300、200、150、75。</p>
  </section>
</template>

<style scoped>
.returned-config { padding: 20px; border: 2px solid var(--fg, #111); margin: 20px 0; }
.config-heading, .config-actions, .secret-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.config-heading { justify-content: space-between; }
h2 { margin: 0; font-size: 20px; }
p { line-height: 1.6; }
.config-form { max-width: 680px; margin-top: 20px; }
.switch-label { margin-left: 10px; }
.secret-field { width: 100%; }
.secret-actions { margin-top: 10px; }
.cancel-action { margin-top: 10px; }
.config-note { color: #666; font-size: 13px; }
@media (max-width: 950px) { .returned-config { padding: 14px; } }
</style>
