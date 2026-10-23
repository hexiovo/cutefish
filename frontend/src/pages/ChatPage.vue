<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NCard, NEmpty, NInput, NPopconfirm, NSelect, NTag } from 'naive-ui'
import { useRoute } from 'vue-router'
import { ApiRequestError, api } from '../api/client'
import { errorMessage } from '../api/errors'
import type { ChatConversation, ChatMessage, ConversationPurpose, ConversationScope, PersonItem } from '../api/types'
import AppIcon from '../components/AppIcon.vue'
import StatusPill from '../components/StatusPill.vue'

const people = ref<PersonItem[]>([])
const conversations = ref<ChatConversation[]>([])
const messages = ref<ChatMessage[]>([])
const selectedId = ref('')
const selectedScope = ref<ConversationScope>('calibration')
const purpose = ref<ConversationPurpose>('persona_interview')
const title = ref('')
const draft = ref('')
const activeConversation = ref<ChatConversation | null>(null)
const loading = ref(false)
const creating = ref(false)
const sending = ref(false)
const uncertain = ref(false)
const error = ref('')
const requestState = ref('')
const lastRequestId = ref('')
const route = useRoute()

const purposeOptions = [
  { label: '真人格访谈', value: 'persona_interview' },
  { label: '人格试聊', value: 'persona_trial' },
  { label: '普通实聊', value: 'production' },
]
const scopeOptions = [
  { label: '校准隔离区', value: 'calibration' },
  { label: '正式记忆区', value: 'production' },
]
const owner = computed(() => people.value.find((person) => person.role === 'owner') ?? null)
const latestStatus = computed(() => activeConversation.value?.state || 'unknown')
const inputBlocked = computed(() => sending.value || uncertain.value || !activeConversation.value || activeConversation.value.state !== 'active' || activeConversation.value.stop_requested || activeConversation.value.human_takeover)
const createScope = computed<ConversationScope>(() => purpose.value === 'production' ? 'production' : 'calibration')
const trialVersionId = computed(() => typeof route.query.personaVersionId === 'string' ? route.query.personaVersionId : '')

function formatTime(value?: string | null) {
  if (!value) return '时间未提供'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN')
}

function messageRole(message: ChatMessage) {
  if (message.role === 'assistant') return '小鱼 · DeepSeek 回复'
  if (message.role === 'user') return '你'
  return message.role || '系统记录'
}

function isUnknownFailure(value: unknown) {
  return value instanceof ApiRequestError && (value.retryable || ['not_connected', 'unknown', 'provider_timeout', 'internal_error'].includes(value.code))
}

function localRequestIdFromError(value: unknown) {
  if (!(value instanceof ApiRequestError) || typeof value.details !== 'object' || value.details === null) return ''
  const id = (value.details as Record<string, unknown>).local_request_id
  return typeof id === 'string' ? id : ''
}

async function loadPeople() {
  try {
    people.value = (await api.people()).items
  } catch (cause) {
    error.value = errorMessage(cause)
  }
}

async function loadConversations(keepSelection = true) {
  loading.value = true
  error.value = ''
  try {
    conversations.value = (await api.conversations(selectedScope.value)).items
    if (!keepSelection || !conversations.value.some((item) => item.id === selectedId.value)) {
      selectedId.value = conversations.value[0]?.id ?? ''
    }
    if (selectedId.value) await loadConversation(selectedId.value)
    else {
      activeConversation.value = null
      messages.value = []
    }
  } catch (cause) {
    error.value = errorMessage(cause)
    conversations.value = []
  } finally {
    loading.value = false
  }
}

async function loadConversation(id: string) {
  if (!id) return
  error.value = ''
  try {
    const result = await api.conversation(id)
    activeConversation.value = result.conversation
    messages.value = result.messages
    const needsReconciliation = result.conversation.state === 'needs_reconciliation'
    requestState.value = needsReconciliation
      ? '此会话仍需核对请求结果，发送已停用。请检查本轮请求状态后再继续。'
      : ''
    uncertain.value = needsReconciliation
  } catch (cause) {
    error.value = errorMessage(cause)
  }
}

async function createConversation() {
  if (creating.value || (purpose.value === 'persona_trial' && !trialVersionId.value)) return
  creating.value = true
  error.value = ''
  try {
    const conversation = await api.createConversation({
      scope: createScope.value,
      purpose: purpose.value,
      title: title.value.trim() || undefined,
      persona_version_id: trialVersionId.value || undefined,
    })
    title.value = ''
    selectedScope.value = createScope.value
    selectedId.value = conversation.id
    await loadConversations()
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    creating.value = false
  }
}

async function sendTurn() {
  const content = draft.value.trim()
  if (!content || inputBlocked.value || !activeConversation.value) return
  const id = activeConversation.value.id
  sending.value = true
  requestState.value = '正在等待本机服务返回请求结果；当前不会自动重发。'
  error.value = ''
  try {
    const result = await api.sendChatTurn(id, { content, idempotency_key: crypto.randomUUID() })
    activeConversation.value = result.conversation
    if (result.inbound_message) messages.value = [...messages.value, result.inbound_message]
    if (result.message) messages.value = [...messages.value, result.message]
    draft.value = ''
    requestState.value = result.request?.state || result.request?.status || '已返回请求记录'
    lastRequestId.value = String(result.request?.local_request_id || '')
    const returnedState = String(result.request?.state || result.request?.status || '').toLowerCase()
    if (['unknown', 'failed_unknown', 'completed_stale'].includes(returnedState)) {
      uncertain.value = true
      requestState.value = returnedState === 'completed_stale'
        ? '请求已完成，但会话在返回前发生变化；该轮已隔离，当前会话不会复用它。'
        : '结果未知：为避免重复计费或重复回复，已停用本轮重发。请查询最近请求确认服务端记录。'
    }
  } catch (cause) {
    error.value = errorMessage(cause)
    if (isUnknownFailure(cause)) {
      lastRequestId.value = localRequestIdFromError(cause)
      uncertain.value = true
      requestState.value = lastRequestId.value
        ? '结果未知：为避免重复计费或重复回复，已停用本轮重发。请查询最近请求确认服务端记录。'
        : '结果未知：为避免重复计费或重复回复，已停用本轮重发。请刷新会话确认服务端状态。'
    } else {
      requestState.value = '请求未完成'
    }
  } finally {
    sending.value = false
  }
}

async function refreshCurrent() {
  if (selectedId.value) await loadConversation(selectedId.value)
  await loadConversations()
}

async function checkLastRequest() {
  if (!lastRequestId.value) return
  error.value = ''
  try {
    const result = await api.chatRequest(lastRequestId.value)
    activeConversation.value = result.conversation
    const incoming = result.inbound_message
    const outgoing = result.message
    if (incoming && !messages.value.some((message) => message.id === incoming.id)) messages.value.push(incoming)
    if (outgoing && !messages.value.some((message) => message.id === outgoing.id)) messages.value.push(outgoing)
    const state = String(result.request?.state || result.request?.status || 'unknown').toLowerCase()
    requestState.value = state === 'completed_stale'
      ? '请求已完成，但会话在返回前发生变化；该轮已隔离，当前会话不会复用它。'
      : state === 'unknown'
        ? '请求结果仍未知；为避免重复计费或回复，输入保持停用。'
        : `请求状态：${state}`
    uncertain.value = state === 'unknown' || state === 'completed_stale'
  } catch (cause) {
    error.value = errorMessage(cause)
  }
}

async function changeConversationState(action: 'stop' | 'takeover') {
  if (!activeConversation.value) return
  error.value = ''
  const currentId = activeConversation.value.id
  try {
    activeConversation.value = action === 'stop'
      ? await api.stopConversation(currentId)
      : await api.takeoverConversation(currentId)
    await loadConversation(currentId)
  } catch (cause) {
    error.value = errorMessage(cause)
  }
}

watch(selectedScope, () => void loadConversations(false))
watch(purpose, () => { selectedScope.value = createScope.value })
watch(selectedId, (id) => {
  if (id !== activeConversation.value?.id) {
    lastRequestId.value = ''
    if (id) void loadConversation(id)
  }
})

onMounted(async () => {
  if (trialVersionId.value) purpose.value = 'persona_trial'
  await loadPeople()
  if (trialVersionId.value) selectedScope.value = 'calibration'
  await loadConversations(false)
  const requestedConversationId = typeof route.query.conversationId === 'string' ? route.query.conversationId : ''
  if (requestedConversationId) {
    await loadConversation(requestedConversationId)
    if (activeConversation.value) {
      selectedScope.value = activeConversation.value.scope
      await loadConversations(false)
      if (conversations.value.some((item) => item.id === requestedConversationId)) {
        selectedId.value = requestedConversationId
        await loadConversation(requestedConversationId)
      }
    }
  }
})
</script>

<template>
  <div class="page-stack">
    <header class="page-heading">
      <div>
        <p class="eyebrow">真人 DeepSeek 对话</p>
        <h1>实聊台</h1>
        <p class="page-heading__description">会话固定使用已认证的管理员身份。仅在你明确点击发送时提交真实请求；结果未知时不会自动重试，也不会虚构回复。</p>
      </div>
      <div class="page-heading__actions">
        <StatusPill :status="latestStatus" />
        <NButton quaternary :loading="loading" @click="refreshCurrent">刷新会话</NButton>
      </div>
    </header>

    <NAlert v-if="error" type="warning" :show-icon="true" title="本机请求未完成">{{ error }}</NAlert>
    <NAlert type="info" :show-icon="true" title="真实对话边界">
      发送会产生 DeepSeek 调用和费用。校准会话与正式记忆隔离；访谈和试聊结果不会自动批准人格。
    </NAlert>

    <section class="chat-layout">
      <NCard class="panel-card chat-sidebar" :bordered="false">
        <template #header><div class="card-heading"><div><h2>新建或选择会话</h2><p>本机界面固定使用已认证管理员身份。</p></div><AppIcon name="chat" /></div></template>
        <div class="chat-create-form">
          <label class="field-label" for="chat-purpose">用途</label>
          <NSelect id="chat-purpose" v-model:value="purpose" :options="purposeOptions" />
          <p class="muted-copy">{{ purpose === 'production' ? '正式记忆范围' : '校准隔离范围' }} · {{ purpose === 'persona_trial' ? (trialVersionId ? `绑定版本 ${trialVersionId}` : '需从人格页面选择一个草案版本') : '由服务端按用途固定' }}</p>
          <label class="field-label" for="chat-title">会话名称（可选）</label>
          <NInput id="chat-title" v-model:value="title" maxlength="120" placeholder="例如：语气访谈第一轮" />
          <NButton type="primary" :disabled="purpose === 'persona_trial' && !trialVersionId" :loading="creating" @click="createConversation">新建会话</NButton>
        </div>
        <div class="chat-scope-row">
          <span class="field-label">当前列表</span>
          <NSelect v-model:value="selectedScope" :options="scopeOptions" size="small" />
        </div>
        <div v-if="conversations.length" class="conversation-list">
          <button v-for="conversation in conversations" :key="conversation.id" class="conversation-item" :class="{ 'conversation-item--active': selectedId === conversation.id }" @click="selectedId = conversation.id">
            <span class="conversation-item__top"><strong>{{ conversation.title || conversation.purpose }}</strong><StatusPill :status="conversation.state" /></span>
            <small>{{ conversation.person_id }} · {{ formatTime(conversation.updated_at) }}</small>
            <small>版本 {{ conversation.revision }} · {{ conversation.channel }} · {{ conversation.delivery_mode }}</small>
          </button>
        </div>
        <NEmpty v-else-if="!loading" description="此范围还没有会话" />
        <div v-else class="inline-loading">正在读取会话…</div>
      </NCard>

      <NCard class="panel-card chat-main" :bordered="false">
        <template #header>
          <div class="card-heading">
            <div><h2>{{ activeConversation?.title || '对话内容' }}</h2><p>{{ owner?.display_name || '管理员' }} · {{ activeConversation?.purpose || '—' }}</p></div>
            <div class="chat-header-tags"><NTag round size="small">{{ activeConversation?.scope || selectedScope }}</NTag><StatusPill v-if="activeConversation?.human_takeover" status="paused" /></div>
          </div>
        </template>
        <div v-if="activeConversation" class="chat-control-bar">
          <span class="muted-copy">会话 revision {{ activeConversation.revision }} · {{ activeConversation.channel }} / {{ activeConversation.delivery_mode }}</span>
          <div class="chat-control-bar__actions">
            <NPopconfirm v-if="!['stopped', 'taken_over'].includes(activeConversation.state)" @positive-click="changeConversationState('stop')">
              <template #trigger><NButton size="small" quaternary type="warning">停止会话</NButton></template>
              停止后不会再接受新一轮消息。确认停止？
            </NPopconfirm>
            <NPopconfirm v-if="!activeConversation.human_takeover && activeConversation.state !== 'taken_over'" @positive-click="changeConversationState('takeover')">
              <template #trigger><NButton size="small" quaternary type="primary">我来接管</NButton></template>
              接管会使当前 revision 失效并阻止自动回复。确认由你接管？
            </NPopconfirm>
            <NButton v-if="lastRequestId" size="small" quaternary @click="checkLastRequest">查询最近请求</NButton>
          </div>
        </div>
        <div v-if="requestState" class="request-progress" role="status"><span class="request-progress__dot" :class="{ 'request-progress__dot--active': sending }"></span>{{ requestState }}</div>
        <div v-if="messages.length" class="message-list" aria-live="polite">
          <article v-for="message in messages" :key="message.id" class="chat-message" :class="message.role === 'assistant' ? 'chat-message--assistant' : 'chat-message--user'">
            <div class="chat-message__heading"><strong>{{ messageRole(message) }}</strong><time>{{ formatTime(message.created_at) }}</time></div>
            <p>{{ message.content }}</p>
            <small v-if="message.provider || message.model">{{ [message.provider, message.model].filter(Boolean).join(' · ') }}</small>
          </article>
        </div>
        <NEmpty v-else-if="activeConversation" description="此会话还没有消息。输入并发送后才会发起真实请求。" />
        <NEmpty v-else description="请先选择或新建一个会话" />
        <div class="chat-composer">
          <NInput v-model:value="draft" type="textarea" :autosize="{ minRows: 3, maxRows: 7 }" maxlength="12000" placeholder="写下你要发送的一条消息…" :disabled="inputBlocked" @keydown.ctrl.enter.prevent="sendTurn" />
          <div class="chat-composer__footer">
            <span class="muted-copy">{{ activeConversation?.human_takeover ? '本会话已由你接管，真实回复暂不可用。' : activeConversation?.stop_requested ? '本会话已请求停止。' : 'Ctrl + Enter 发送 · 每次点击都可能产生费用' }}</span>
            <NButton type="primary" :disabled="!draft.trim() || inputBlocked" :loading="sending" @click="sendTurn">发送一轮</NButton>
          </div>
          <NButton v-if="uncertain" quaternary @click="refreshCurrent">刷新确认结果</NButton>
        </div>
      </NCard>
    </section>
  </div>
</template>

<style scoped>
.chat-layout { display: grid; min-width: 0; grid-template-columns: minmax(280px, 350px) minmax(0, 1fr); gap: 16px; align-items: start; }
.chat-sidebar, .chat-main { min-width: 0; }
.chat-create-form { display: grid; gap: 8px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }
.chat-create-form .field-label { margin-top: 4px; }
.chat-scope-row { display: grid; gap: 7px; margin: 14px 0 8px; }
.conversation-list { display: grid; max-height: 46vh; gap: 7px; overflow-y: auto; }
.conversation-item { display: grid; gap: 5px; width: 100%; padding: 11px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface); color: var(--text); text-align: left; cursor: pointer; }
.conversation-item--active { border-color: var(--primary); background: var(--primary-soft); }
.conversation-item__top { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
.conversation-item__top strong { min-width: 0; overflow-wrap: anywhere; font-size: 12px; }
.conversation-item small { overflow-wrap: anywhere; color: var(--text-muted); font-size: 10px; }
.chat-header-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.chat-control-bar { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 13px; padding: 9px 0 12px; border-bottom: 1px solid var(--border); }
.chat-control-bar__actions { display: flex; flex-wrap: wrap; gap: 7px; }
.request-progress { display: flex; align-items: flex-start; gap: 9px; margin-bottom: 14px; padding: 10px 12px; border-radius: 11px; background: var(--surface-soft); color: var(--text-soft); font-size: 12px; }
.request-progress__dot { width: 7px; height: 7px; flex: 0 0 auto; margin-top: 5px; border-radius: 50%; background: var(--warning); }
.request-progress__dot--active { background: var(--primary); box-shadow: 0 0 0 4px var(--primary-soft); }
.message-list { display: grid; max-height: 58vh; gap: 12px; overflow-y: auto; padding: 2px 2px 12px; }
.chat-message { max-width: min(88%, 820px); padding: 12px 14px; border: 1px solid var(--border); border-radius: 15px; background: var(--surface-soft); }
.chat-message--user { justify-self: end; border-color: color-mix(in srgb, var(--primary) 36%, var(--border)); background: var(--primary-soft); }
.chat-message--assistant { justify-self: start; }
.chat-message__heading { display: flex; flex-wrap: wrap; align-items: center; gap: 7px 12px; color: var(--text-soft); font-size: 11px; }
.chat-message__heading time, .chat-message small { color: var(--text-muted); font-size: 10px; }
.chat-message p { margin: 8px 0 0; color: var(--text); overflow-wrap: anywhere; white-space: pre-wrap; }
.chat-message small { display: block; margin-top: 7px; }
.chat-composer { display: grid; gap: 9px; margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--border); }
.chat-composer__footer { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; }
@media (max-width: 860px) { .chat-layout { grid-template-columns: 1fr; } .conversation-list { max-height: 32vh; } }
@media (max-width: 620px) { .chat-message { max-width: 100%; } .chat-composer__footer { align-items: flex-start; flex-direction: column; } }
</style>
