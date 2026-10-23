<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { NAlert, NButton, NCard, NEmpty, NForm, NFormItem, NInput, NInputNumber, NSelect, NSpin, NSwitch, NTag, useMessage } from 'naive-ui'
import { api } from '../api/client'
import { errorMessage, requestReference } from '../api/errors'
import AppIcon from '../components/AppIcon.vue'
import { appState } from '../stores/app'
import type { ModuleConfigData } from '../api/types'

interface SchemaField {
  type?: string
  title?: string
  description?: string
  enum?: Array<string | number | boolean>
  default?: unknown
  required?: boolean
  writeOnly?: boolean
  format?: string
  [key: string]: unknown
}

const props = defineProps<{ moduleId: string }>()
const message = useMessage()
const config = ref<ModuleConfigData | null>(null)
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const formValues = ref<Record<string, any>>({})
const moduleName = computed(() => appState.modules.data?.find((module) => module.id === props.moduleId)?.name || props.moduleId)
const schemaObject = computed(() => config.value?.schema || {})
const schemaFields = computed(() => {
  const properties = schemaObject.value.properties
  if (!properties || typeof properties !== 'object') return []
  const required = Array.isArray(schemaObject.value.required) ? schemaObject.value.required as string[] : []
  return Object.entries(properties as Record<string, SchemaField>).map(([key, value]) => ({
    key,
    ...value,
    title: value.title || key,
    required: required.includes(key),
    sensitive: value.writeOnly === true || value.format === 'password' || value['x-secret'] === true || value['x-sensitive'] === true,
  }))
})
const sensitiveFields = computed(() => schemaFields.value.filter((field) => field.sensitive))
const unsupportedFields = computed(() => schemaFields.value.filter((field) => !field.sensitive && !field.enum?.length && !['string', 'number', 'integer', 'boolean'].includes(field.type || '')))
const canSave = computed(() => Boolean(config.value && schemaFields.value.length && !sensitiveFields.value.length && !unsupportedFields.value.length && !saving.value))

function selectOptions(field: (typeof schemaFields.value)[number]) {
  return (field.enum ?? []).map((value) => ({ label: String(value), value: typeof value === 'boolean' ? String(value) : value as string | number }))
}

function secretConfigured(value: unknown) {
  if (value === null || value === undefined || value === false || value === '') return false
  if (typeof value === 'object') {
    const reference = value as { credential_ref?: unknown; configured?: unknown; status?: unknown }
    return Boolean(reference.credential_ref || reference.configured || reference.status === 'configured')
  }
  return true
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const result = await api.moduleConfig(props.moduleId)
    config.value = result
    const initial: Record<string, unknown> = {}
    for (const field of schemaFields.value) {
      if (field.sensitive) continue
      const current = result.values[field.key]
      if (current !== undefined && current !== null) initial[field.key] = current
      else if (field.default !== undefined) initial[field.key] = field.default
      else if (field.type === 'boolean') initial[field.key] = false
      else if (field.type === 'number' || field.type === 'integer') initial[field.key] = null
      else initial[field.key] = ''
    }
    formValues.value = initial
  } catch (cause) {
    config.value = null
    error.value = errorMessage(cause)
    const requestId = requestReference(cause)
    if (requestId) error.value += ' 请求编号：' + requestId
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!config.value || !canSave.value) return
  saving.value = true
  error.value = ''
  try {
    const values: Record<string, unknown> = { ...formValues.value }
    for (const field of schemaFields.value) {
      if (!field.enum?.length) continue
      if (field.type === 'boolean' && typeof values[field.key] === 'string') values[field.key] = values[field.key] === 'true'
      if ((field.type === 'number' || field.type === 'integer') && typeof values[field.key] === 'string') values[field.key] = Number(values[field.key])
    }
    await api.saveModuleConfig(props.moduleId, {
      values,
      expected_version: config.value.version,
    })
    message.success('模块设置已保存到本机。')
    await load()
  } catch (cause) {
    error.value = errorMessage(cause)
    const requestId = requestReference(cause)
    if (requestId) error.value += ' 请求编号：' + requestId
  } finally {
    saving.value = false
  }
}

watch(() => props.moduleId, () => void load())
onMounted(() => void load())
</script>

<template>
  <div class="page-stack">
    <header class="page-heading">
      <div><p class="eyebrow">模块配置</p><h1>{{ moduleName }}</h1><p class="page-heading__description">设置字段由后端注册表提供，保存时会校验版本。</p></div>
      <RouterLink class="back-link" to="/modules"><AppIcon name="arrow" :size="15" /> 返回模块清单</RouterLink>
    </header>

    <NAlert v-if="error" type="warning" :show-icon="true" title="配置接口暂不可用">{{ error }}</NAlert>
    <NSpin :show="loading">
      <NCard v-if="config" class="panel-card" :bordered="false">
        <template #header>
          <div class="card-heading"><div><h2>可用设置</h2><p>配置版本 {{ config.version }}</p></div><NTag round type="info">来自模块 schema</NTag></div>
        </template>
        <NAlert v-if="sensitiveFields.length" type="info" :show-icon="true" class="settings-alert">
          此模块包含凭据字段。为避免回显或意外覆盖，当前页面不读取或编辑这些字段；请使用对应的专用账号配置入口。
        </NAlert>
        <NForm v-if="schemaFields.length && !sensitiveFields.length && !unsupportedFields.length" label-placement="top" class="settings-form">
          <NFormItem v-for="field in schemaFields" :key="field.key" :label="field.title" :required="field.required" :feedback="field.description">
            <NSelect v-if="field.enum?.length" v-model:value="formValues[field.key]" :options="selectOptions(field)" />
            <NSwitch v-else-if="field.type === 'boolean'" v-model:value="formValues[field.key]" />
            <NInputNumber v-else-if="field.type === 'number' || field.type === 'integer'" v-model:value="formValues[field.key]" :class="{ 'full-width': true }" />
            <NInput v-else v-model:value="formValues[field.key]" :maxlength="500" />
          </NFormItem>
          <div class="form-actions"><span class="muted-copy">保存后设置版本会更新</span><NButton type="primary" :loading="saving" :disabled="!canSave" @click="save">保存模块设置</NButton></div>
        </NForm>
        <div v-else-if="sensitiveFields.length" class="secret-status-list">
          <div v-for="field in sensitiveFields" :key="field.key" class="secret-status">
            <span>{{ field.title }}</span>
            <strong>{{ secretConfigured(config.values[field.key]) ? '凭据已登记（内容不可查看）' : '尚未登记' }}</strong>
          </div>
        </div>
        <NAlert v-else-if="unsupportedFields.length" type="warning" :show-icon="true">
          当前设置包含未支持的字段类型。页面不会忽略字段后提交，以免覆盖已有配置。
        </NAlert>
        <NEmpty v-else description="该模块没有公开可编辑的设置" />
      </NCard>
      <NEmpty v-else-if="!loading && !error" description="本机服务尚未提供此模块的配置 schema" />
    </NSpin>
  </div>
</template>
