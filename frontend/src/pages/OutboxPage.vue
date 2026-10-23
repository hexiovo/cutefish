<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NCard, NEmpty, NPopconfirm, NSelect, NTag } from 'naive-ui'
import { RouterLink } from 'vue-router'
import { ApiRequestError, api } from '../api/client'
import { errorMessage } from '../api/errors'
import type { ChatDraft, OutboxItem, OutboxPreflightResult } from '../api/types'
import AppIcon from '../components/AppIcon.vue'
import StatusPill from '../components/StatusPill.vue'

const items = ref<OutboxItem[]>([])
const chatDrafts = ref<ChatDraft[]>([])
const selected = ref<OutboxItem | null>(null)
const selectedId = ref('')
const state = ref('')
const draftState = ref<'review' | 'stale' | 'approved' | 'sent' | 'cancelled'>('review')
const total = ref(0)
const loading = ref(false)
const actionId = ref('')
const outboxAction = ref<'preflight' | 'dispatch' | ''>('')
const error = ref('')
const draftError = ref('')
const takeoverId = ref('')
const notice = ref('')
const actionError = ref('')
const actionNotice = ref('')
const preflightResult = ref<OutboxPreflightResult | null>(null)
const dispatchLockedIds = ref<Set<string>>(new Set())

const stateOptions = [
  { label: '所有状态', value: '' },
  { label: '待发送草稿', value: 'prepared' },
  { label: '发送处理中', value: 'sending' },
  { label: '已发送', value: 'sent' },
  { label: '失败', value: 'failed' },
  { label: '结果未知', value: 'unknown' },
  { label: '已取消', value: 'cancelled' },
]
const draftStateOptions = [
  { label: '待人工复核', value: 'review' },
  { label: '已过期 / 失效', value: 'stale' },
  { label: '已批准状态', value: 'approved' },
  { label: '已发送记录', value: 'sent' },
  { label: '已取消', value: 'cancelled' },
]
const safePayload = computed(() => {
  if (!selected.value?.payload) return '详情接口未返回草稿正文。'
  try { return JSON.stringify(selected.value.payload, null, 2) } catch { return '草稿正文无法显示。' }
})
const selectedVoiceOutbox = computed(() => {
  const item = selected.value
  return Boolean(item
    && item.action_id === 'qq.voice.send'
    && item.message_type === 'record'
    && item.record_ref?.artifact_id)
})
const canPreflightVoiceOutbox = computed(() => {
  const item = selected.value
  return Boolean(item
    && selectedVoiceOutbox.value
    && item.state === 'prepared'
    && !dispatchLockedIds.value.has(item.id)
    && !outboxAction.value
    && !actionId.value)
})
const preflightValidated = computed(() => {
  const item = selected.value
  const gate = preflightResult.value
  return Boolean(item
    && selectedVoiceOutbox.value
    && item.state === 'prepared'
    && gate?.state === 'validated'
    && gate.outbox_id === item.id
    && gate.conversation_revision === item.expected_revision
    && !dispatchLockedIds.value.has(item.id))
})
const canDispatchVoiceOutbox = computed(() => Boolean(
  preflightValidated.value
  && !outboxAction.value
  && !actionId.value
))

function time(value?: string | null) {
  if (!value) return '未提供'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN')
}

function isUnknownMutationOutcome(value: unknown) {
  return value instanceof ApiRequestError && (
    value.retryable
    || value.httpStatus !== null && value.httpStatus >= 500
    || ['not_connected', 'unknown', 'invalid_response', 'missing_data'].includes(value.code)
  )
}

function isSameVoiceOutboxBinding(left: OutboxItem, right: OutboxItem) {
  return left.id === right.id
    && left.action_id === 'qq.voice.send'
    && right.action_id === 'qq.voice.send'
    && left.state === 'prepared'
    && right.state === 'prepared'
    && left.message_type === 'record'
    && right.message_type === 'record'
    && left.conversation_id === right.conversation_id
    && left.expected_revision === right.expected_revision
    && left.batch_id === right.batch_id
    && left.authorization_id === right.authorization_id
    && left.account_id === right.account_id
    && left.recipient_id === right.recipient_id
    && left.record_ref?.artifact_id === right.record_ref?.artifact_id
    && left.record_ref?.sha256 === right.record_ref?.sha256
    && left.record_ref?.voice_version === right.record_ref?.voice_version
    && left.record_ref?.approval_ref === right.record_ref?.approval_ref
}

function replaceOutboxItem(item: OutboxItem) {
  items.value = items.value.map((current) => current.id === item.id ? { ...current, ...item } : current)
  if (selectedId.value === item.id) selected.value = item
}

async function load() {
  loading.value = true
  error.value = ''
  preflightResult.value = null
  try {
    const result = await api.outbox({ state: state.value || undefined, limit: 50, offset: 0 })
    items.value = result.items
    total.value = result.total
    if (selectedId.value && result.items.some((item) => item.id === selectedId.value)) await loadDetail(selectedId.value)
    else {
      selectedId.value = result.items[0]?.id ?? ''
      selected.value = selectedId.value ? await api.outboxItem(selectedId.value) : null
    }
  } catch (cause) {
    error.value = errorMessage(cause)
    items.value = []
    selected.value = null
  } finally {
    loading.value = false
  }
}

async function loadChatDrafts() {
  draftError.value = ''
  try {
    chatDrafts.value = (await api.chatDrafts({ state: draftState.value, limit: 100 })).items
  } catch (cause) {
    chatDrafts.value = []
    draftError.value = errorMessage(cause)
  }
}

async function loadDetail(id: string) {
  if (!id) return
  try {
    selected.value = await api.outboxItem(id)
  } catch (cause) {
    error.value = errorMessage(cause)
    selected.value = null
  }
}

async function cancelItem(item: OutboxItem) {
  preflightResult.value = null
  actionError.value = ''
  actionNotice.value = ''
  actionId.value = item.id
  error.value = ''
  try {
    selected.value = await api.cancelOutbox(item.id)
    await load()
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    actionId.value = ''
  }
}

async function preflightVoiceOutbox(item: OutboxItem) {
  if (!canPreflightVoiceOutbox.value || outboxAction.value) return
  outboxAction.value = 'preflight'
  preflightResult.value = null
  actionError.value = ''
  actionNotice.value = ''
  try {
    const latest = await api.outboxItem(item.id)
    if (selectedId.value !== item.id) return
    replaceOutboxItem(latest)
    if (!isSameVoiceOutboxBinding(item, latest)) {
      actionError.value = 'Outbox 状态或绑定已变化；已更新详情，没有执行预检。请核对后重新操作。'
      return
    }
    const gate = await api.preflightOutbox(item.id)
    if (selectedId.value !== item.id) return
    const afterGate = await api.outboxItem(item.id)
    if (selectedId.value !== item.id) return
    replaceOutboxItem(afterGate)
    if (gate.state !== 'validated'
      || gate.outbox_id !== item.id
      || gate.conversation_revision !== latest.expected_revision
      || !isSameVoiceOutboxBinding(latest, afterGate)) {
      actionError.value = '服务端预检未返回与当前草稿匹配的 validated 门禁；派发保持关闭。'
      return
    }
    preflightResult.value = gate
    actionNotice.value = `服务端预检通过：会话 revision ${gate.conversation_revision}。派发仍需要你单独确认。`
  } catch (cause) {
    actionError.value = errorMessage(cause)
    preflightResult.value = null
  } finally {
    outboxAction.value = ''
  }
}

async function dispatchVoiceOutbox(item: OutboxItem) {
  if (!canDispatchVoiceOutbox.value || outboxAction.value) return
  outboxAction.value = 'dispatch'
  preflightResult.value = null
  actionError.value = ''
  actionNotice.value = ''
  let dispatchStarted = false
  try {
    const latest = await api.outboxItem(item.id)
    if (selectedId.value !== item.id) return
    selected.value = latest
    if (!isSameVoiceOutboxBinding(item, latest)) {
      actionError.value = 'Outbox 状态或绑定已变化；已更新详情，没有派发。请重新预检并核对。'
      return
    }
    const gate = await api.preflightOutbox(item.id)
    if (selectedId.value !== item.id) return
    if (gate.state !== 'validated'
      || gate.outbox_id !== item.id
      || gate.conversation_revision !== latest.expected_revision) {
      actionError.value = '派发前的最新服务端预检未通过；没有调用发送器。'
      return
    }
    preflightResult.value = gate
    dispatchStarted = true
    const result = await api.dispatchOutbox(item.id)
    if (selectedId.value !== item.id) return
    replaceOutboxItem(result)
    preflightResult.value = null
    if (result.state === 'sent') {
      actionNotice.value = '服务端已记录此语音动作 sent。'
    } else if (result.state === 'unknown' || result.state === 'sending') {
      dispatchLockedIds.value = new Set(dispatchLockedIds.value).add(item.id)
      actionNotice.value = `服务端状态为 ${result.state}；重复派发已锁定。请刷新并核对，不要重试。`
    } else if (result.state === 'failed') {
      actionNotice.value = `服务端记录状态为 failed（${result.result_code || '未提供原因'}）；不会自动重试。`
    } else {
      actionNotice.value = `派发接口返回状态 ${result.state}；本次不会自动重试，请重新读取状态后判断。`
    }
  } catch (cause) {
    actionError.value = errorMessage(cause)
    preflightResult.value = null
    if (dispatchStarted && isUnknownMutationOutcome(cause)) {
      dispatchLockedIds.value = new Set(dispatchLockedIds.value).add(item.id)
      actionNotice.value = '派发请求结果未知；重复派发已锁定。正在只读刷新状态，请勿重试。'
    }
    if (selectedId.value === item.id) {
      try { replaceOutboxItem(await api.outboxItem(item.id)) } catch { /* preserve the original action error */ }
    }
  } finally {
    outboxAction.value = ''
  }
}

async function takeoverDraft(draft: ChatDraft) {
  takeoverId.value = draft.id
  draftError.value = ''
  notice.value = ''
  try {
    await api.takeoverConversation(draft.conversation_id)
    notice.value = '会话已由管理员接管；对应的投递预检会因 revision / takeover 状态变化而失效。'
    await loadChatDrafts()
  } catch (cause) {
    draftError.value = errorMessage(cause)
  } finally {
    takeoverId.value = ''
  }
}

watch(state, () => void load())
watch(selectedId, (id) => {
  preflightResult.value = null
  actionError.value = ''
  actionNotice.value = ''
  void loadDetail(id)
})
watch(draftState, () => void loadChatDrafts())
onMounted(() => { void load(); void loadChatDrafts() })
</script>

<template>
  <div class="page-stack">
    <header class="page-heading">
      <div><p class="eyebrow">本地草稿与交接</p><h1>消息草稿</h1><p class="page-heading__description">仅语音草稿可从此页进入服务端预检；派发需要再次确认。未知状态不会重试，待发送草稿可以本地取消。</p></div>
      <div class="page-heading__actions"><NTag round type="warning">语音须预检确认</NTag><NButton quaternary :loading="loading" :disabled="outboxAction !== ''" @click="load">刷新列表</NButton></div>
    </header>

    <NAlert v-if="error" type="warning" :show-icon="true" title="Outbox 请求未完成">{{ error }}</NAlert>
    <NAlert v-if="draftError" type="warning" :show-icon="true" title="QQ 草稿请求未完成">{{ draftError }}</NAlert>
    <NAlert v-if="notice" type="success" :show-icon="true">{{ notice }}</NAlert>
    <NAlert v-if="actionError" type="warning" :show-icon="true" title="语音 Outbox 操作未完成">{{ actionError }}</NAlert>
    <NAlert v-if="actionNotice" type="info" :show-icon="true">{{ actionNotice }}</NAlert>
    <NAlert type="warning" :show-icon="true" title="外发安全状态">
      只有 `qq.voice.send` 的 prepared 草稿可进入预检；只有当前服务端返回 `validated` 才会开放派发确认。派发前会再次读取草稿并预检；`sender_disabled` 等门禁拒绝时不会调用发送器。结果未知或 sending 状态禁止重试。
    </NAlert>

    <div class="outbox-toolbar"><label class="outbox-filter"><span>状态</span><NSelect v-model:value="state" :options="stateOptions" :disabled="outboxAction !== ''" /></label><span class="muted-copy">共 {{ total }} 条 · 当前显示最多 50 条</span></div>
    <section class="outbox-layout">
      <NCard class="panel-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>Outbox 流水</h2><p>列表响应不包含草稿正文。</p></div><AppIcon name="book" /></div></template>
        <div v-if="items.length" class="outbox-list">
          <button v-for="item in items" :key="item.id" class="outbox-item" :class="{ 'outbox-item--active': selectedId === item.id }" :disabled="outboxAction !== ''" @click="selectedId = item.id">
            <span class="outbox-item__top"><strong>{{ item.action_id }}</strong><StatusPill :status="item.state" /></span>
            <small>{{ item.id }}</small>
            <small>会话 {{ item.conversation_id }} · 目标 {{ item.recipient_id }}</small>
            <small>{{ time(item.created_at) }}</small>
          </button>
        </div>
        <NEmpty v-else-if="!loading" description="没有符合条件的 Outbox 项目" />
        <div v-else class="inline-loading">正在读取 Outbox…</div>
      </NCard>

      <NCard class="panel-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>草稿详情</h2><p>正文仅在单条详情接口返回时显示。</p></div><StatusPill :status="selected?.state" /></div></template>
        <div v-if="selected" class="outbox-detail">
          <dl class="outbox-fields">
            <div><dt>草稿 ID</dt><dd>{{ selected.id }}</dd></div>
            <div><dt>会话 revision</dt><dd>{{ selected.expected_revision }}</dd></div>
            <div><dt>批次</dt><dd>{{ selected.batch_id }} · v{{ selected.batch_version }}</dd></div>
            <div><dt>账号 / 目标</dt><dd>{{ selected.account_id }} / {{ selected.recipient_id }}</dd></div>
            <div><dt>人物 / actor</dt><dd>{{ selected.person_id }} / {{ selected.actor_id }}</dd></div>
            <div><dt>分段</dt><dd>{{ selected.segment_index + 1 }} / {{ selected.segment_count }}</dd></div>
            <div v-if="selectedVoiceOutbox"><dt>语音素材 / voice version</dt><dd>{{ selected.record_ref?.artifact_id }} / {{ selected.record_ref?.voice_version }}</dd></div>
            <div><dt>幂等键</dt><dd>{{ selected.idempotency_key }}</dd></div>
            <div><dt>正文摘要</dt><dd>{{ selected.payload_hash }}</dd></div>
            <div><dt>创建时间</dt><dd>{{ time(selected.created_at) }}</dd></div>
            <div><dt>尝试次数</dt><dd>{{ selected.attempt_count }}</dd></div>
            <div><dt>结果代码</dt><dd>{{ selected.result_code || '未提供' }}</dd></div>
          </dl>
          <details class="outbox-payload"><summary>查看本地草稿正文</summary><pre>{{ safePayload }}</pre></details>
          <NAlert v-if="selectedVoiceOutbox && dispatchLockedIds.has(selected.id)" type="warning" :show-icon="true">此语音草稿的派发结果未知或仍在处理中；重复派发已锁定。请只读刷新并核对服务端状态。</NAlert>
          <NAlert v-if="preflightValidated" type="success" :show-icon="true">服务端预检已通过，草稿仍为 prepared；会话 revision {{ preflightResult?.conversation_revision }}。你还需要再次确认派发。</NAlert>
          <div class="outbox-actions">
            <NButton v-if="selectedVoiceOutbox && selected.state === 'prepared'" size="small" secondary :loading="outboxAction === 'preflight'" :disabled="!canPreflightVoiceOutbox" @click="preflightVoiceOutbox(selected)">服务端重新预检</NButton>
            <NPopconfirm v-if="preflightValidated" :disabled="!canDispatchVoiceOutbox" @positive-click="dispatchVoiceOutbox(selected)">
              <template #trigger><NButton type="warning" :loading="outboxAction === 'dispatch'" :disabled="!canDispatchVoiceOutbox">确认派发这条语音</NButton></template>
              确认后将调用本机 QQ 发送器，向账号 {{ selected.account_id }} / 收件人 {{ selected.recipient_id }} 派发素材 {{ selected.record_ref?.artifact_id }}。系统会在此确认后重新读取草稿并预检；门禁不再通过就会停止。确认派发？
            </NPopconfirm>
            <NPopconfirm v-if="selected.state === 'prepared'" @positive-click="cancelItem(selected)">
              <template #trigger><NButton type="error" secondary :loading="actionId === selected.id" :disabled="outboxAction !== ''">取消此草稿</NButton></template>
              只会将待发送草稿标记为已取消，不会尝试发送。确认继续？
            </NPopconfirm>
            <span v-else class="muted-copy">{{ selected.state === 'unknown' || selected.state === 'sending' ? '此状态不可本地取消或重试。' : '此状态没有可执行的外发操作。' }}</span>
          </div>
        </div>
        <NEmpty v-else description="选择一条草稿查看详情" />
      </NCard>
    </section>

    <NCard class="panel-card" :bordered="false">
      <template #header><div class="card-heading"><div><h2>QQ 回复草稿与人工接管</h2><p>只读查看服务端草稿；接管会使会话 revision 变化，不会发送任何内容。</p></div><AppIcon name="chat" /></div></template>
      <div class="outbox-toolbar"><label class="outbox-filter"><span>草稿状态</span><NSelect v-model:value="draftState" :options="draftStateOptions" /></label><NButton quaternary :loading="takeoverId !== ''" @click="loadChatDrafts">刷新 QQ 草稿</NButton></div>
      <div v-if="chatDrafts.length" class="chat-draft-list">
        <article v-for="draft in chatDrafts" :key="draft.id" class="chat-draft-card">
          <div class="chat-draft-card__top"><div><strong>草稿 {{ draft.id }}</strong><small>会话 {{ draft.conversation_id }} · source revision {{ draft.source_revision }} · 草稿 v{{ draft.revision }}</small></div><StatusPill :status="draft.state" /></div>
          <p>{{ draft.content }}</p>
          <small>来源消息：{{ draft.source_message_id }} · {{ draft.scope }} · {{ draft.stale_reason || '未提供失效原因' }}</small>
          <div class="chat-draft-card__actions">
            <RouterLink class="text-link" :to="{ name: 'chat', query: { conversationId: draft.conversation_id } }">打开来源会话</RouterLink>
            <NPopconfirm v-if="draft.state === 'review' || draft.state === 'stale'" @positive-click="takeoverDraft(draft)">
              <template #trigger><NButton size="small" type="primary" secondary :loading="takeoverId === draft.id">接管来源会话</NButton></template>
              接管后会话将停止自动回复，已存在的草稿仍保留供你查看。确认接管？
            </NPopconfirm>
          </div>
        </article>
      </div>
      <NEmpty v-else-if="!draftError" description="此状态下没有 QQ 回复草稿" />
    </NCard>
  </div>
</template>

<style scoped>
.outbox-toolbar { display: flex; flex-wrap: wrap; align-items: end; justify-content: space-between; gap: 12px; }
.outbox-filter { display: grid; width: min(100%, 250px); gap: 5px; color: var(--text-muted); font-size: 11px; font-weight: 700; }
.outbox-layout { display: grid; min-width: 0; grid-template-columns: minmax(280px, 0.8fr) minmax(0, 1.2fr); gap: 16px; }
.outbox-list { display: grid; max-height: 65vh; gap: 8px; overflow-y: auto; }
.outbox-item { display: grid; gap: 5px; width: 100%; padding: 11px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface); color: var(--text); text-align: left; cursor: pointer; }
.outbox-item--active { border-color: var(--primary); background: var(--primary-soft); }
.outbox-item__top { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
.outbox-item strong { font-size: 12px; }
.outbox-item small { overflow-wrap: anywhere; color: var(--text-muted); font-size: 10px; }
.outbox-fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 210px), 1fr)); gap: 10px 16px; margin: 0; }
.outbox-fields > div { display: grid; min-width: 0; gap: 3px; }
.outbox-fields dt { color: var(--text-muted); font-size: 10px; }
.outbox-fields dd { margin: 0; overflow-wrap: anywhere; color: var(--text-soft); font-size: 11px; }
.outbox-payload { margin-top: 16px; padding: 12px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-soft); }
.outbox-payload summary { color: var(--primary); font-size: 12px; font-weight: 700; cursor: pointer; }
.outbox-payload pre { max-height: 360px; overflow: auto; color: var(--text-soft); font-size: 11px; white-space: pre-wrap; overflow-wrap: anywhere; }
.outbox-actions { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 8px; margin-top: 14px; }
.chat-draft-list { display: grid; gap: 11px; margin-top: 14px; }
.chat-draft-card { padding: 13px; border: 1px solid var(--border); border-radius: 13px; background: var(--surface); }
.chat-draft-card__top { display: flex; align-items: flex-start; justify-content: space-between; gap: 9px; }
.chat-draft-card__top > div { display: grid; gap: 3px; }
.chat-draft-card strong { color: var(--text); font-size: 12px; }
.chat-draft-card small { color: var(--text-muted); font-size: 10px; overflow-wrap: anywhere; }
.chat-draft-card p { max-height: 180px; overflow: auto; color: var(--text-soft); font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
.chat-draft-card__actions { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 8px; margin-top: 10px; }
@media (max-width: 820px) { .outbox-layout { grid-template-columns: 1fr; } .outbox-list { max-height: 32vh; } }
</style>
