<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { NAlert, NButton, NCard, NEmpty, NForm, NFormItem, NInput, NInputNumber, NPopconfirm, NSelect, NTag, useMessage } from 'naive-ui'
import { api } from '../api/client'
import { errorMessage, requestReference } from '../api/errors'
import type { ModuleItem, TrialBatch, TrialBatchCreate } from '../api/types'
import AppIcon from '../components/AppIcon.vue'
import StatusPill from '../components/StatusPill.vue'
import { appState, refreshPanel } from '../stores/app'

const message = useMessage()
const creating = ref(false)
const actionId = ref<string | null>(null)
const formError = ref('')
const label = ref('')
const moduleId = ref<string | null>(null)
const capabilityId = ref<string | null>(null)
const objectText = ref('')
const expiresAt = ref('')
const stopKind = ref<'manual' | 'after_n'>('manual')
const maxActions = ref<number | null>(1)

const moduleOptions = computed(() => (appState.modules.data ?? [])
  .filter((module) => module.enabled !== false && (module.capabilities?.length ?? 0) > 0)
  .map((module) => ({ label: module.name || module.id, value: module.id })))
const selectedModule = computed<ModuleItem | null>(() => appState.modules.data?.find((module) => module.id === moduleId.value) ?? null)
const capabilityOptions = computed(() => (selectedModule.value?.capabilities ?? [])
  .map((capability) => ({ label: capability.name || capability.id, value: capability.id })))
const batches = computed(() => appState.batches.data ?? [])

function objectIds() {
  return objectText.value.split(/[\r\n,，]+/).map((value) => value.trim()).filter(Boolean)
}

function formatTime(value?: string | null) {
  if (!value) return '未提供'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const timezone = appState.hostSettings.data?.timezone || 'Asia/Shanghai'
  try {
    return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short', timeZone: timezone }).format(date)
  } catch {
    return date.toLocaleString('zh-CN')
  }
}

function batchStatusText(batch: TrialBatch) {
  return batch.state || 'unknown'
}

async function createBatch() {
  formError.value = ''
  const ids = objectIds()
  if (!label.value.trim() || !moduleId.value || !capabilityId.value || !expiresAt.value || ids.length === 0) {
    formError.value = '请填写批次名称、模块、能力、至少一个对象 ID 和截止时间。'
    return
  }
  if (stopKind.value === 'after_n' && (!maxActions.value || maxActions.value < 1)) {
    formError.value = '请设置大于 0 的动作上限。'
    return
  }
  const stopCondition: TrialBatchCreate['stop_condition'] = stopKind.value === 'manual'
    ? { kind: 'manual' }
    : { kind: 'after_n', max_actions: maxActions.value || 1 }
  const payload: TrialBatchCreate = {
    label: label.value.trim(),
    target: { module_id: moduleId.value, capability_id: capabilityId.value, object_ids: ids },
    expires_at: new Date(expiresAt.value).toISOString(),
    stop_condition: stopCondition,
  }
  creating.value = true
  try {
    const created = await api.createBatch(payload)
    await refreshPanel()
    label.value = ''
    moduleId.value = null
    capabilityId.value = null
    objectText.value = ''
    expiresAt.value = ''
    stopKind.value = 'manual'
    message.success('本机批次已保存为草稿：' + created.id)
  } catch (error) {
    formError.value = errorMessage(error)
    const requestId = requestReference(error)
    if (requestId) formError.value += ' 请求编号：' + requestId
  } finally {
    creating.value = false
  }
}

async function startBatch(batch: TrialBatch) {
  actionId.value = batch.id
  formError.value = ''
  try {
    await api.startBatch(batch.id)
    await refreshPanel()
    message.success('已向本机服务提交启动请求。')
  } catch (error) {
    formError.value = errorMessage(error)
    const requestId = requestReference(error)
    if (requestId) formError.value += ' 请求编号：' + requestId
  } finally {
    actionId.value = null
  }
}

async function stopBatch(batch: TrialBatch) {
  actionId.value = batch.id
  formError.value = ''
  try {
    await api.stopBatch(batch.id)
    await refreshPanel()
    message.success('已向本机服务提交停止请求。')
  } catch (error) {
    formError.value = errorMessage(error)
    const requestId = requestReference(error)
    if (requestId) formError.value += ' 请求编号：' + requestId
  } finally {
    actionId.value = null
  }
}

onMounted(() => {
  if (!appState.batches.data && !appState.batches.loading) void refreshPanel()
})
</script>

<template>
  <div class="page-stack">
    <header class="page-heading">
      <div><p class="eyebrow">手动运行</p><h1>运行批次</h1><p class="page-heading__description">只为本次小范围活动创建有截止时间的批次，启动需要再次手动操作。</p></div>
      <NTag round type="info">local_trial</NTag>
    </header>

    <NAlert type="warning" :show-icon="true" title="批次边界">
      创建或启动批次本身不会授权消息外发、公开写入或付费服务。后端仍会在每次动作前复核权限与预算。
    </NAlert>
    <NAlert v-if="formError" type="error" :show-icon="true" title="批次操作未完成">{{ formError }}</NAlert>

    <section class="runtime-grid">
      <NCard class="panel-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>新建本机批次</h2><p>指定对象、停止规则与强制截止时间。</p></div><AppIcon name="plus" /></div></template>
        <NForm label-placement="top" class="settings-form">
          <NFormItem label="批次名称" required>
            <NInput v-model:value="label" maxlength="80" placeholder="例如：本次本地连接检查" />
          </NFormItem>
          <div class="form-two-columns">
            <NFormItem label="模块" required>
              <NSelect v-model:value="moduleId" :options="moduleOptions" placeholder="选择已注册模块" @update:value="capabilityId = null" />
            </NFormItem>
            <NFormItem label="能力" required>
              <NSelect v-model:value="capabilityId" :options="capabilityOptions" :disabled="!moduleId" placeholder="选择本次能力" />
            </NFormItem>
          </div>
          <NFormItem label="本次对象 ID" required>
            <NInput v-model:value="objectText" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }" placeholder="每行一个对象 ID；不会默认处理全部对象" />
          </NFormItem>
          <div class="form-two-columns">
            <NFormItem label="停止规则" required>
              <NSelect v-model:value="stopKind" :options="[{ label: '由我手动停止', value: 'manual' }, { label: '达到动作上限后停止', value: 'after_n' }]" />
            </NFormItem>
            <NFormItem v-if="stopKind === 'after_n'" label="动作上限" required>
              <NInputNumber v-model:value="maxActions" :min="1" :max="100" class="full-width" />
            </NFormItem>
          </div>
          <NFormItem label="强制截止时间" required>
            <input v-model="expiresAt" type="datetime-local" class="native-input" aria-label="批次强制截止时间" />
            <template #feedback>按 {{ appState.hostSettings.data?.timezone || '本机时区' }} 解释；到期后批次自动停止。</template>
          </NFormItem>
          <div class="form-actions">
            <span class="muted-copy">需至少指定一个对象 ID</span>
            <NButton type="primary" :loading="creating" @click="createBatch">保存为草稿</NButton>
          </div>
        </NForm>
        <NAlert v-if="moduleOptions.length === 0 && appState.modules.data" type="info" :show-icon="true" class="runtime-note">目前没有返回可选择的模块能力。</NAlert>
        <NAlert v-if="appState.batches.error" type="warning" :show-icon="true" class="runtime-note">批次接口暂不可用：{{ appState.batches.error.message }}</NAlert>
      </NCard>

      <NCard class="panel-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>当前批次</h2><p>状态与截止时间由本机服务维护。</p></div><AppIcon name="runtime" /></div></template>
        <div v-if="batches.length" class="batch-list">
          <article v-for="batch in batches" :key="batch.id" class="batch-card">
            <div class="batch-card__top"><div><h3>{{ batch.label || batch.id }}</h3><small>{{ batch.id }}</small></div><StatusPill :status="batchStatusText(batch)" /></div>
            <div class="batch-card__meta">
              <span><b>对象</b>{{ batch.target?.object_ids?.length ?? '未提供' }}</span>
              <span><b>截止</b>{{ formatTime(batch.expires_at) }}</span>
              <span><b>停止规则</b>{{ batch.stop_condition?.kind === 'after_n' ? '动作上限 ' + batch.stop_condition.max_actions : batch.stop_condition?.kind === 'manual' ? '手动停止' : '未提供' }}</span>
            </div>
            <p v-if="batch.stop_reason" class="batch-card__reason">{{ batch.stop_reason }}</p>
            <div class="batch-card__actions">
              <NButton v-if="batch.state === 'draft' || batch.state === 'stopped'" size="small" type="primary" :loading="actionId === batch.id" @click="startBatch(batch)">
                <template #icon><AppIcon name="play" :size="15" /></template>手动启动
              </NButton>
              <NPopconfirm v-if="batch.state === 'running'" @positive-click="stopBatch(batch)">
                <template #trigger><NButton size="small" type="error" secondary :loading="actionId === batch.id"><template #icon><AppIcon name="stop" :size="15" /></template>停止批次</NButton></template>
                确认停止这个本机批次？
              </NPopconfirm>
              <span v-if="batch.mode">模式：{{ batch.mode }}</span>
            </div>
          </article>
        </div>
        <NEmpty v-else-if="!appState.batches.loading && !appState.batches.error" description="本机服务尚未返回运行批次" />
        <div v-else-if="appState.batches.loading" class="inline-loading">正在读取批次…</div>
        <NEmpty v-else description="批次数据不可用；请确认本机服务已启动" />
      </NCard>
    </section>
  </div>
</template>
