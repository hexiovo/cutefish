<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NCard, NEmpty, NForm, NFormItem, NInput, NSelect, NSpin, NTag, useMessage } from 'naive-ui'
import { api } from '../api/client'
import { errorMessage, requestReference } from '../api/errors'
import type { DeepSeekCredentialStatus } from '../api/types'
import AppIcon from '../components/AppIcon.vue'
import StatusPill from '../components/StatusPill.vue'
import { appState } from '../stores/app'

const message = useMessage()
const theme = ref<'light' | 'dark' | 'system'>('system')
const saving = ref(false)
const formError = ref('')
const credential = ref<DeepSeekCredentialStatus | null>(null)
const apiKey = ref('')
const credentialLoading = ref(false)
const credentialSaving = ref(false)
const credentialError = ref('')
const credentialNotice = ref('')
const settings = computed(() => appState.uiSettings.data)
const host = computed(() => appState.hostSettings.data)
const themeOptions = [
  { label: '跟随系统', value: 'system' },
  { label: '浅色', value: 'light' },
  { label: '深色', value: 'dark' },
]

watch(settings, (value) => {
  if (value) theme.value = value.theme
}, { immediate: true })

const dirty = computed(() => Boolean(settings.value && theme.value !== settings.value.theme))

async function save() {
  if (!settings.value || !dirty.value) return
  saving.value = true
  formError.value = ''
  try {
    const updated = await api.saveUiSettings({
      theme: theme.value,
      locale: 'zh-CN',
      expected_version: settings.value.version,
    })
    appState.uiSettings.data = updated
    message.success('偏好设置已保存到本机。')
  } catch (error) {
    formError.value = errorMessage(error)
    const requestId = requestReference(error)
    if (requestId) formError.value += ' 请求编号：' + requestId
  } finally {
    saving.value = false
  }
}

function rootLabel(rootKey: string) {
  const labels: Record<string, string> = {
    room_root: '小鱼的房间',
    data_root: '业务数据',
    backup_root: '备份目录',
    cache_root: '缓存目录',
    media_root: '媒体目录',
  }
  return labels[rootKey] || rootKey
}

async function loadDeepSeekCredential() {
  credentialLoading.value = true
  credentialError.value = ''
  try {
    credential.value = await api.deepSeekCredentialStatus()
  } catch (error) {
    credentialError.value = errorMessage(error)
  } finally {
    credentialLoading.value = false
  }
}

async function saveDeepSeekCredential() {
  if (!apiKey.value.trim() || credentialSaving.value) return
  credentialSaving.value = true
  credentialError.value = ''
  credentialNotice.value = ''
  try {
    credential.value = await api.saveDeepSeekCredential(apiKey.value)
    apiKey.value = ''
    credentialNotice.value = 'DeepSeek API Key 已保存；页面不会回显凭据，也没有测试连通性。'
    message.success('DeepSeek API Key 已保存到本机凭据存储。')
  } catch (error) {
    credentialError.value = errorMessage(error)
    const requestId = requestReference(error)
    if (requestId) credentialError.value += ' 请求编号：' + requestId
  } finally {
    credentialSaving.value = false
  }
}

onMounted(() => void loadDeepSeekCredential())
</script>

<template>
  <div class="page-stack">
    <header class="page-heading">
      <div><p class="eyebrow">本机选项</p><h1>偏好设置</h1><p class="page-heading__description">设置通过本机服务保存；路径状态只展示已登记的根目录。</p></div>
      <NTag round type="info">简体中文</NTag>
    </header>

    <NAlert v-if="formError" type="error" :show-icon="true" title="保存未完成">{{ formError }}</NAlert>

    <section class="settings-grid">
      <NCard class="panel-card" :bordered="false">
        <template #header>
          <div class="card-heading"><div><h2>外观与语言</h2><p>选择适合当前设备的显示方式。</p></div><AppIcon name="settings" /></div>
        </template>
        <NSpin :show="appState.uiSettings.loading && !settings">
          <NForm v-if="settings" label-placement="top" class="settings-form">
            <NFormItem label="主题">
              <NSelect v-model:value="theme" :options="themeOptions" :disabled="saving" />
            </NFormItem>
            <NFormItem label="界面语言">
              <NSelect :value="'zh-CN'" :options="[{ label: '简体中文', value: 'zh-CN' }]" disabled />
            </NFormItem>
            <div class="form-actions">
              <span class="muted-copy">设置版本 {{ settings.version }}</span>
              <NButton type="primary" :disabled="!dirty" :loading="saving" @click="save">保存设置</NButton>
            </div>
          </NForm>
          <NAlert v-else-if="appState.uiSettings.error" type="warning" :show-icon="true" title="偏好接口暂不可用">{{ appState.uiSettings.error.message }}</NAlert>
          <NEmpty v-else description="本机服务尚未返回偏好设置" />
        </NSpin>
      </NCard>

      <NCard class="panel-card" :bordered="false">
        <template #header>
          <div class="card-heading"><div><h2>主机与路径</h2><p>路径以逻辑根目录显示，不扫描其他磁盘。</p></div><AppIcon name="server" /></div>
        </template>
        <div v-if="host" class="host-summary">
          <div class="host-summary__top">
            <div><span class="muted-label">主机标识</span><strong>{{ host.host_id || '未提供' }}</strong></div>
            <StatusPill :status="host.mode || 'unknown'" kind="plain" />
          </div>
          <div v-if="host.roots?.length" class="root-list">
            <div v-for="root in host.roots" :key="root.root_key" class="root-row">
              <div><strong>{{ rootLabel(root.root_key) }}</strong><small>{{ root.display_path || (root.configured ? '路径已登记，显示路径不可用' : '尚未配置') }}</small></div>
              <StatusPill :status="root.available === true ? 'ready' : root.available === false ? 'not_connected' : root.configured ? 'configured' : 'unknown'" kind="runtime" />
            </div>
          </div>
          <NEmpty v-else description="本机服务没有提供路径根目录" />
          <p class="settings-note">房间、业务数据、缓存与备份使用独立根目录。更换路径需要后端提供受限的主机设置接口。</p>
        </div>
        <NAlert v-else-if="appState.hostSettings.error" type="warning" :show-icon="true" title="主机设置暂不可用">{{ appState.hostSettings.error.message }}</NAlert>
        <NEmpty v-else description="主机设置尚未由本机服务提供" />
      </NCard>

      <NCard class="panel-card" :bordered="false">
        <template #header>
          <div class="card-heading"><div><h2>DeepSeek 凭据</h2><p>凭据单独保存在本机凭据存储中。</p></div><AppIcon name="server" /></div>
        </template>
        <div class="credential-form">
          <NAlert v-if="credentialError" type="error" :show-icon="true">{{ credentialError }}</NAlert>
          <NAlert v-if="credentialNotice" type="success" :show-icon="true">{{ credentialNotice }}</NAlert>
          <div class="credential-status"><span>配置状态</span><strong>{{ credentialLoading ? '正在读取…' : credential ? credential.configured ? '已配置' : '未配置' : '未知' }}</strong></div>
          <NInput v-model:value="apiKey" type="password" show-password-on="click" autocomplete="new-password" maxlength="4096" placeholder="输入 DeepSeek API Key" aria-label="DeepSeek API Key" :disabled="credentialLoading || credentialSaving" />
          <small>密钥只在提交期间保留于页面内存；成功后立即清空。状态接口只返回是否已配置，不会回显密钥；保存不会测试连通性或调用模型。</small>
          <div class="form-actions"><NButton quaternary :loading="credentialLoading" :disabled="credentialSaving" @click="loadDeepSeekCredential">刷新状态</NButton><NButton type="primary" :loading="credentialSaving" :disabled="credentialLoading || !apiKey.trim()" @click="saveDeepSeekCredential">保存 API Key</NButton></div>
        </div>
      </NCard>
    </section>
  </div>
</template>

<style scoped>
.credential-form { display: grid; max-width: 680px; gap: 10px; }
.credential-form > small { color: var(--text-muted); font-size: 10px; line-height: 1.5; }
.credential-status { display: flex; align-items: center; justify-content: space-between; gap: 12px; color: var(--text-muted); font-size: 11px; }
.credential-status strong { color: var(--text); }
</style>
