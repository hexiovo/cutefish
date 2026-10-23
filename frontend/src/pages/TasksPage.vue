<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NCard, NEmpty, NInput, NPopconfirm, NSelect, NTag } from 'naive-ui'
import { RouterLink } from 'vue-router'
import { api } from '../api/client'
import { errorMessage } from '../api/errors'
import type { CandidateItem, CandidateState, ChatConversation, ChatMessage, ProactivityBatchResult, ProactivityPolicy, TaskItem, TrialBatch } from '../api/types'
import AppIcon from '../components/AppIcon.vue'
import StatusPill from '../components/StatusPill.vue'

const tasks = ref<TaskItem[]>([])
const candidates = ref<CandidateItem[]>([])
const selectedTaskId = ref('')
const candidateState = ref<CandidateState | ''>('')
const title = ref('')
const intent = ref('')
const progress = ref('')
const nextStep = ref('')
const sourceRefsText = ref('')
const candidatePreview = ref<Record<string, unknown> | null>(null)
const loading = ref(false)
const creating = ref(false)
const actionId = ref('')
const error = ref('')
const notice = ref('')
const proactivityPolicy = ref<ProactivityPolicy | null>(null)
const policyDraft = ref({ enabled: false, explicit_pause: false, dnd_enabled: false, manual_takeover: false })
const savingPolicy = ref(false)
const proactivityConversations = ref<ChatConversation[]>([])
const ownerPersonId = ref('')
const selectedConversationId = ref('')
const sourceMessages = ref<ChatMessage[]>([])
const loadingConversations = ref(false)
const loadingSourceMessages = ref(false)
const selectedSourceIds = ref<string[]>([])
const batchLabel = ref('手动候选批次')
const batchExpiresAt = ref(new Date(Date.now() + 60 * 60 * 1000).toISOString().slice(0, 16))
const maxCandidates = ref<1 | 2 | 3>(1)
const batchTaskId = ref('')
const proactivityBatch = ref<TrialBatch | null>(null)
const generatingBatch = ref(false)
const generationResult = ref<ProactivityBatchResult | null>(null)
const batchConsumed = ref(false)

const candidateOptions = [
  { label: '所有状态', value: '' },
  { label: '提出', value: 'proposed' },
  { label: '草稿', value: 'draft' },
  { label: '已准备', value: 'prepared' },
  { label: '已取消', value: 'cancelled' },
  { label: '已过期', value: 'expired' },
]
const selectedTask = computed(() => tasks.value.find((task) => task.id === selectedTaskId.value) ?? null)
const canGenerateBatch = computed(() => Boolean(
  proactivityPolicy.value?.enabled
  && !proactivityPolicy.value.explicit_pause
  && !proactivityPolicy.value.dnd_enabled
  && !proactivityPolicy.value.manual_takeover
  && proactivityBatch.value?.state === 'draft'
  && !batchConsumed.value,
))

function formatTime(value?: string | null) {
  if (!value) return '未提供'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN')
}

function listRefs(value: string) {
  return value.split(/[\r\n]+/).map((item) => item.trim()).filter(Boolean)
}

function formatSourceMessage(message: ChatMessage) {
  const content = message.content.replace(/\s+/g, ' ').trim()
  return content.length > 180 ? `${content.slice(0, 180)}…` : content
}

function formatCandidateContext(context: Record<string, unknown> | null | undefined) {
  if (!context || Object.keys(context).length === 0) return '未提供'
  try {
    return JSON.stringify(context)
  } catch {
    return '上下文无法显示'
  }
}

function resetProactivitySources() {
  sourceMessages.value = []
  selectedSourceIds.value = []
  proactivityBatch.value = null
  generationResult.value = null
  batchConsumed.value = false
}

async function loadTasks() {
  try {
    tasks.value = (await api.tasks()).items
    if (!selectedTaskId.value || !tasks.value.some((item) => item.id === selectedTaskId.value)) selectedTaskId.value = tasks.value[0]?.id ?? ''
  } catch (cause) {
    error.value = errorMessage(cause)
    tasks.value = []
  }
}

async function loadCandidates() {
  try {
    const result = await api.candidates({ state: candidateState.value || undefined, limit: 100, offset: 0 })
    candidates.value = result.items
  } catch (cause) {
    error.value = errorMessage(cause)
    candidates.value = []
  }
}

async function refreshAll() {
  loading.value = true
  error.value = ''
  await Promise.all([loadTasks(), loadCandidates(), loadProactivityPolicy()])
  loading.value = false
}

async function loadProactivityPolicy() {
  try {
    const policy = await api.proactivityPolicy()
    proactivityPolicy.value = policy
    policyDraft.value = {
      enabled: policy.enabled,
      explicit_pause: policy.explicit_pause,
      dnd_enabled: policy.dnd_enabled,
      manual_takeover: policy.manual_takeover,
    }
  } catch (cause) {
    proactivityPolicy.value = null
    error.value = errorMessage(cause)
  }
}

async function savePolicy() {
  const current = proactivityPolicy.value
  if (!current || savingPolicy.value) return
  savingPolicy.value = true
  error.value = ''
  notice.value = ''
  try {
    proactivityPolicy.value = await api.saveProactivityPolicy({
      expected_revision: current.revision,
      ...policyDraft.value,
    })
    policyDraft.value = {
      enabled: proactivityPolicy.value.enabled,
      explicit_pause: proactivityPolicy.value.explicit_pause,
      dnd_enabled: proactivityPolicy.value.dnd_enabled,
      manual_takeover: proactivityPolicy.value.manual_takeover,
    }
    notice.value = '手动主动策略已保存；保存策略不会创建批次或调用模型。'
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    savingPolicy.value = false
  }
}

async function loadProactivityConversations() {
  loadingConversations.value = true
  error.value = ''
  notice.value = ''
  try {
    const [conversationResult, peopleResult] = await Promise.all([api.conversations('production'), api.people()])
    ownerPersonId.value = peopleResult.items.find((person) => person.role === 'owner')?.person_id ?? ''
    proactivityConversations.value = conversationResult.items.filter((item) => item.purpose === 'production' && item.channel === 'ui' && item.person_id === ownerPersonId.value)
    if (!proactivityConversations.value.some((item) => item.id === selectedConversationId.value)) {
      selectedConversationId.value = ''
      sourceMessages.value = []
      selectedSourceIds.value = []
    }
    notice.value = `已读取 ${proactivityConversations.value.length} 个 production UI 对话；来源消息仍需你明确选择。`
  } catch (cause) {
    error.value = errorMessage(cause)
    proactivityConversations.value = []
  } finally {
    loadingConversations.value = false
  }
}

async function loadConversationSources(conversationId = selectedConversationId.value) {
  if (!conversationId) return
  loadingSourceMessages.value = true
  error.value = ''
  notice.value = ''
  selectedSourceIds.value = []
  try {
    const detail = await api.conversation(conversationId)
    const conversation = proactivityConversations.value.find((item) => item.id === conversationId)
    if (!conversation || conversation.scope !== 'production' || conversation.purpose !== 'production' || conversation.channel !== 'ui') {
      throw new Error('主动候选只支持 production UI 对话。')
    }
    sourceMessages.value = detail.messages.filter((message) => message.role.toLowerCase() === 'user' && message.scope === 'production' && message.channel === 'ui' && message.person_id === ownerPersonId.value)
    notice.value = `已读取 ${sourceMessages.value.length} 条本人消息；source_event_ids 将提交所选 ChatMessage.id。`
  } catch (cause) {
    error.value = errorMessage(cause)
    sourceMessages.value = []
  } finally {
    loadingSourceMessages.value = false
  }
}

async function createManualBatch() {
  if (!proactivityPolicy.value || !selectedConversationId.value || !selectedSourceIds.value.length || selectedSourceIds.value.length > 20 || !batchLabel.value.trim() || !batchExpiresAt.value || !maxCandidates.value) return
  const expiresAt = new Date(batchExpiresAt.value)
  if (Number.isNaN(expiresAt.getTime()) || expiresAt.getTime() <= Date.now()) {
    error.value = '批次到期时间必须晚于当前时间。'
    return
  }
  error.value = ''
  notice.value = ''
  generationResult.value = null
  batchConsumed.value = false
  actionId.value = 'proactivity:create'
  try {
    proactivityBatch.value = await api.createProactivityBatch({
      label: batchLabel.value.trim(),
      conversation_id: selectedConversationId.value,
      source_event_ids: [...selectedSourceIds.value],
      expires_at: expiresAt.toISOString(),
      max_candidates: maxCandidates.value,
      ...(batchTaskId.value ? { task_id: batchTaskId.value } : {}),
    })
    notice.value = '已创建 manual draft 批次；尚未调用 ChatService。只有你明确触发下一步时才会尝试生成。'
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    actionId.value = ''
  }
}

async function generateManualBatch() {
  const batch = proactivityBatch.value
  if (!batch || batchConsumed.value || generatingBatch.value) return
  generatingBatch.value = true
  error.value = ''
  notice.value = ''
  try {
    generationResult.value = await api.generateProactivityBatch(batch.id)
    batchConsumed.value = true
    notice.value = generationResult.value.state === 'created'
      ? '已完成一次有限候选生成；候选仅保存在本地，不会进入 Outbox、QQ 或语音回复。'
      : `生成请求返回 ${generationResult.value.state}；为避免重复调用，此批次已锁定。请核对 gate 状态后创建新批次。`
    await loadCandidates()
  } catch (cause) {
    error.value = errorMessage(cause)
    batchConsumed.value = true
    notice.value = '生成请求结果未能确认。为避免重复计费或重复调用，此批次已在界面锁定；请先核对服务端状态。'
  } finally {
    generatingBatch.value = false
  }
}

async function createTask() {
  if (!title.value.trim() || !intent.value.trim() || creating.value) return
  creating.value = true
  error.value = ''
  notice.value = ''
  try {
    const task = await api.createTask({
      title: title.value.trim(),
      intent: intent.value.trim(),
      progress: progress.value.trim() || undefined,
      next_step: nextStep.value.trim() || undefined,
      source_refs: listRefs(sourceRefsText.value),
    })
    title.value = ''
    intent.value = ''
    progress.value = ''
    nextStep.value = ''
    sourceRefsText.value = ''
    selectedTaskId.value = task.id
    notice.value = '任务已保存为 open / not_dispatched 草稿；不会启动后台或外部动作。'
    await loadTasks()
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    creating.value = false
  }
}

async function taskAction(task: TaskItem, action: 'pause' | 'resume' | 'cancel' | 'handover', releaseHandover = false) {
  actionId.value = `${task.id}:${action}`
  error.value = ''
  notice.value = ''
  const reasons = {
    pause: '管理员通过本机控制台手动暂停',
    resume: '管理员通过本机控制台手动恢复并重评',
    cancel: '管理员通过本机控制台取消',
    handover: '管理员通过本机控制台本地接管',
  }
  try {
    const updated = await api.taskAction(task.id, action, {
      expected_revision: task.revision,
      reason: reasons[action],
      ...(releaseHandover ? { release_handover: true } : {}),
    })
    notice.value = action === 'handover'
      ? '已接管本地任务；没有授权外发或运行外部动作。'
      : action === 'resume' && releaseHandover
        ? '已明确解除人工接管并触发本地重评；若独立硬锁仍存在，任务会保持暂停。'
        : action === 'resume' && task.manual_takeover
          ? '已触发本地重评并保留人工接管；任务仍会保持暂停。'
          : action === 'resume'
            ? '已请求本地重评；不会自动执行任务。独立硬锁仍在时任务会保持暂停。'
            : '任务状态已更新。'
    await loadTasks()
    if (updated.id) selectedTaskId.value = updated.id
  } catch (cause) {
    error.value = errorMessage(cause)
    await loadTasks()
  } finally {
    actionId.value = ''
  }
}

async function candidateAction(candidate: CandidateItem, action: 'preview' | 'cancel' | 'reassess') {
  actionId.value = `${candidate.id}:${action}`
  error.value = ''
  notice.value = ''
  candidatePreview.value = null
  try {
    const result = action === 'preview'
      ? await api.candidatePreview(candidate.id)
      : await api.candidateAction(candidate.id, action, {
        expected_revision: candidate.revision,
        reason: action === 'cancel' ? '管理员通过本机控制台取消候选' : undefined,
      })
    if (action === 'preview') candidatePreview.value = result
    notice.value = action === 'preview' ? '本地预览已生成；没有调用模型、网络或外部执行。' : action === 'reassess' ? '本地候选已重评；没有自动执行或补发。' : '候选已取消。'
    await loadCandidates()
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    actionId.value = ''
  }
}

watch(candidateState, () => void loadCandidates())
onMounted(() => void refreshAll())
</script>

<template>
  <div class="page-stack">
    <header class="page-heading">
      <div><p class="eyebrow">手动本地控制</p><h1>任务与候选</h1><p class="page-heading__description">任务状态、候选和外部投递状态彼此分开。新任务只保存草稿；恢复只触发本地重评，不自动执行或补发。</p></div>
      <div class="page-heading__actions"><NTag round type="info">local_trial</NTag><NButton quaternary :loading="loading" @click="refreshAll">刷新</NButton></div>
    </header>

    <NAlert v-if="error" type="warning" :show-icon="true" title="本地任务操作未完成">{{ error }}</NAlert>
    <NAlert v-if="notice" type="success" :show-icon="true">{{ notice }}</NAlert>
    <NAlert type="info" :show-icon="true" title="控制边界">
      暂停、取消和接管只更新本地状态。任何外部投递依然需要独立授权；未知投递结果保持原样，不重新执行。
    </NAlert>

    <section class="proactivity-grid">
      <NCard class="panel-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>主动候选策略</h2><p>策略只允许手动批次；保存本身不会调用模型。</p></div><AppIcon name="shield" /></div></template>
        <div class="proactivity-panel">
          <div class="proactivity-status"><span>状态</span><StatusPill :status="proactivityPolicy ? proactivityPolicy.enabled ? 'enabled' : 'disabled' : 'unknown'" /><span>模式</span><strong>{{ proactivityPolicy?.mode || '未知' }} · revision {{ proactivityPolicy?.revision ?? '未知' }}</strong></div>
          <NAlert type="warning" :show-icon="true">自动调度保持关闭。启用策略不会自动生成候选；每次调用仍需手工选来源、创建批次并单独确认。</NAlert>
          <label class="proactivity-checkbox"><input v-model="policyDraft.enabled" type="checkbox" :disabled="!proactivityPolicy || savingPolicy"><span>允许手动生成候选</span></label>
          <label class="proactivity-checkbox"><input v-model="policyDraft.explicit_pause" type="checkbox" :disabled="!proactivityPolicy || savingPolicy"><span>显式暂停</span></label>
          <label class="proactivity-checkbox"><input v-model="policyDraft.dnd_enabled" type="checkbox" :disabled="!proactivityPolicy || savingPolicy"><span>勿扰</span></label>
          <label class="proactivity-checkbox"><input v-model="policyDraft.manual_takeover" type="checkbox" :disabled="!proactivityPolicy || savingPolicy"><span>人工接管</span></label>
          <NButton type="primary" :disabled="!proactivityPolicy" :loading="savingPolicy" @click="savePolicy">保存策略（CAS revision）</NButton>
          <small>保存时使用当前 revision；发生冲突时请刷新状态后重新检查。</small>
        </div>
      </NCard>

      <NCard class="panel-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>手动候选生成</h2><p>最多选择 20 条本人 production UI 消息；候选最多 3 条。</p></div><AppIcon name="plus" /></div></template>
        <div class="proactivity-panel">
          <div class="proactivity-actions"><NButton size="small" secondary :loading="loadingConversations" @click="loadProactivityConversations">读取 production UI 对话</NButton><NButton size="small" quaternary :disabled="!selectedConversationId" :loading="loadingSourceMessages" @click="loadConversationSources()">读取所选对话的消息</NButton></div>
          <label class="field-label" for="proactivity-conversation">来源对话</label>
          <NSelect id="proactivity-conversation" v-model:value="selectedConversationId" :options="proactivityConversations.map((conversation) => ({ label: `${conversation.title || conversation.id} · ${conversation.id}`, value: conversation.id }))" :disabled="loadingConversations || loadingSourceMessages" placeholder="先手动读取 production UI 对话" @update:value="resetProactivitySources" />
          <div v-if="sourceMessages.length" class="source-message-list">
            <label v-for="message in sourceMessages" :key="message.id" class="source-message-row">
              <input v-model="selectedSourceIds" type="checkbox" :value="message.id" :disabled="selectedSourceIds.length >= 20 && !selectedSourceIds.includes(message.id)">
              <span><strong>{{ formatTime(message.created_at) }} · {{ message.id }}</strong><small>{{ formatSourceMessage(message) || '空消息' }}</small></span>
            </label>
          </div>
          <NEmpty v-else description="选择对话并手动载入来源消息" />
          <small>所选 ID 会作为 source_event_ids 提交，但它们实际是 ChatMessage.id。不会读取 QQ 群聊，也不会自动挑选消息。</small>
          <label class="field-label" for="proactivity-label">批次名称</label><NInput id="proactivity-label" v-model:value="batchLabel" maxlength="120" />
          <label class="field-label" for="proactivity-expiry">到期时间（本地时间）</label><input id="proactivity-expiry" v-model="batchExpiresAt" class="datetime-input" type="datetime-local">
          <label class="field-label" for="proactivity-max">候选上限</label><NSelect id="proactivity-max" v-model:value="maxCandidates" :options="[{ label: '1 条', value: 1 }, { label: '2 条', value: 2 }, { label: '3 条', value: 3 }]" />
          <label class="field-label" for="proactivity-task">关联任务（可选）</label><NSelect id="proactivity-task" v-model:value="batchTaskId" :options="tasks.map((task) => ({ label: `${task.title} · ${task.id}`, value: task.id }))" clearable placeholder="不关联" />
          <NButton secondary :disabled="!selectedConversationId || selectedSourceIds.length === 0 || selectedSourceIds.length > 20 || !batchLabel.trim() || actionId === 'proactivity:create'" :loading="actionId === 'proactivity:create'" @click="createManualBatch">创建 manual draft 批次</NButton>
          <div v-if="proactivityBatch" class="proactivity-batch-result">
            <div><span>批次</span><strong>{{ proactivityBatch.id }}</strong></div>
            <div><span>状态</span><StatusPill :status="proactivityBatch.state" /></div>
            <small>创建批次没有调用 ChatService。下一步最多进行一次生成，可能使用已配置的付费服务。</small>
            <NPopconfirm :disabled="!canGenerateBatch" @positive-click="generateManualBatch">
              <template #trigger><NButton type="primary" :disabled="!canGenerateBatch" :loading="generatingBatch">明确确认：生成有限候选</NButton></template>
              可能产生一次 ChatService 费用。最多一次模型请求、最多 {{ maxCandidates }} 条候选；只保存本地，不发 QQ、不进入 Outbox，也不生成语音。确认继续？
            </NPopconfirm>
          </div>
          <div v-if="generationResult" class="proactivity-batch-result">
            <div><span>结果</span><StatusPill :status="generationResult.state" /></div>
            <div><span>Policy gate</span><StatusPill :status="generationResult.checks.policy" /></div>
            <div><span>Conversation gate</span><StatusPill :status="generationResult.checks.conversation" /></div>
            <div><span>Batch gate</span><StatusPill :status="generationResult.checks.batch" /></div>
            <div><span>Budget gate</span><StatusPill :status="generationResult.checks.budget" /></div>
            <pre>{{ JSON.stringify({ usage: generationResult.usage, ledger: generationResult.ledger, candidates: generationResult.candidates.map((candidate) => ({ id: candidate.id, title: candidate.title, state: candidate.state })), chat_result: generationResult.chat_result }, null, 2) }}</pre>
          </div>
        </div>
      </NCard>
    </section>

    <section class="tasks-grid">
      <NCard class="panel-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>登记一个任务</h2><p>只记录目标与上下文，不接收命令或派发正文。</p></div><AppIcon name="plus" /></div></template>
        <div class="task-form">
          <label class="field-label" for="task-title">任务标题</label>
          <NInput id="task-title" v-model:value="title" maxlength="160" placeholder="简短描述本地目标" />
          <label class="field-label" for="task-intent">任务意图</label>
          <NInput id="task-intent" v-model:value="intent" type="textarea" :autosize="{ minRows: 3, maxRows: 6 }" maxlength="4000" placeholder="说明要解决什么，不要粘贴可执行命令" />
          <label class="field-label" for="task-progress">当前进度（可选）</label>
          <NInput id="task-progress" v-model:value="progress" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" maxlength="2000" />
          <label class="field-label" for="task-next-step">下一步（可选）</label>
          <NInput id="task-next-step" v-model:value="nextStep" maxlength="1000" />
          <label class="field-label" for="task-sources">来源引用（每行一个，可选）</label>
          <NInput id="task-sources" v-model:value="sourceRefsText" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" maxlength="4000" />
          <NButton type="primary" :disabled="!title.trim() || !intent.trim()" :loading="creating" @click="createTask">保存为本地草稿</NButton>
        </div>
      </NCard>

      <NCard class="panel-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>持久任务</h2><p>{{ tasks.length }} 条任务由本机服务维护。</p></div><AppIcon name="runtime" /></div></template>
        <div v-if="tasks.length" class="task-list">
          <button v-for="task in tasks" :key="task.id" class="task-row" :class="{ 'task-row--active': selectedTaskId === task.id }" @click="selectedTaskId = task.id">
            <span class="task-row__top"><strong>{{ task.title }}</strong><StatusPill :status="task.status" /></span>
            <small>{{ task.id }} · revision {{ task.revision }}</small>
            <span class="task-row__bottom"><span>外部状态</span><StatusPill :status="task.effect_state" /></span>
          </button>
        </div>
        <NEmpty v-else-if="!loading" description="还没有登记任务" />
        <div v-else class="inline-loading">正在读取任务…</div>
      </NCard>

      <NCard v-if="selectedTask" class="panel-card task-detail" :bordered="false">
        <template #header><div class="card-heading"><div><h2>{{ selectedTask.title }}</h2><p>{{ selectedTask.id }} · revision {{ selectedTask.revision }}</p></div><StatusPill :status="selectedTask.status" /></div></template>
        <p class="task-detail__intent">{{ selectedTask.intent }}</p>
        <div class="task-detail__meta">
          <div><span>进度</span><strong>{{ selectedTask.progress || '未提供' }}</strong></div>
          <div><span>下一步</span><strong>{{ selectedTask.next_step || '未提供' }}</strong></div>
          <div><span>效果状态</span><StatusPill :status="selectedTask.effect_state" /></div>
          <div><span>显式暂停</span><strong>{{ selectedTask.explicit_pause ? '是' : '否' }}</strong></div>
          <div><span>人工接管</span><strong>{{ selectedTask.manual_takeover ? '是' : '否' }}</strong></div>
          <div><span>暂停原因</span><strong>{{ selectedTask.pause_reason || '未提供' }}</strong></div>
          <div><span>来源</span><strong>{{ selectedTask.source_refs.join(' · ') || '未提供' }}</strong></div>
          <div><span>能力引用</span><strong>{{ selectedTask.capability_refs.join(' · ') || '未提供' }}</strong></div>
          <div><span>预算引用</span><strong>{{ selectedTask.budget_refs.join(' · ') || '未提供' }}</strong></div>
          <div><span>更新</span><strong>{{ formatTime(selectedTask.updated_at) }}</strong></div>
        </div>
        <div class="task-actions">
          <NButton v-if="selectedTask.status === 'active'" secondary :loading="actionId === `${selectedTask.id}:pause`" @click="taskAction(selectedTask, 'pause')">暂停</NButton>
          <NButton v-if="selectedTask.status === 'paused'" type="primary" secondary :loading="actionId === `${selectedTask.id}:resume`" @click="taskAction(selectedTask, 'resume')">{{ selectedTask.manual_takeover ? '重评（保留接管）' : '恢复并重评' }}</NButton>
          <NPopconfirm v-if="selectedTask.status === 'paused' && selectedTask.manual_takeover" @positive-click="taskAction(selectedTask, 'resume', true)">
            <template #trigger><NButton secondary :loading="actionId === `${selectedTask.id}:resume`">解除接管并重评</NButton></template>
            仅明确确认后才会清除此任务的人工接管标记。独立硬锁仍在时任务会保持暂停。确认解除接管？
          </NPopconfirm>
          <NButton v-if="!selectedTask.manual_takeover && !['done', 'abandoned'].includes(selectedTask.status)" secondary :loading="actionId === `${selectedTask.id}:handover`" @click="taskAction(selectedTask, 'handover')">本地接管</NButton>
          <NPopconfirm v-if="!['done', 'abandoned'].includes(selectedTask.status)" @positive-click="taskAction(selectedTask, 'cancel')">
            <template #trigger><NButton type="error" quaternary :loading="actionId === `${selectedTask.id}:cancel`">取消任务</NButton></template>
            确认取消此任务？已派发或结果未知的外部状态会保留，不会被重置。
          </NPopconfirm>
        </div>
      </NCard>

      <NCard class="panel-card candidates-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>候选项目</h2><p>是否发出以 Outbox 为准；此页没有派发功能。</p></div><AppIcon name="book" /></div></template>
        <label class="candidate-filter"><span>候选状态</span><NSelect v-model:value="candidateState" :options="candidateOptions" size="small" /></label>
        <div v-if="candidates.length" class="candidate-list">
          <article v-for="candidate in candidates" :key="candidate.id" class="candidate-card">
            <div class="candidate-card__top"><div><h3>{{ candidate.title }}</h3><small>{{ candidate.id }} · revision {{ candidate.revision }}</small></div><StatusPill :status="candidate.state" /></div>
            <p>{{ candidate.reason }}</p>
            <div class="candidate-card__details"><span>上下文：{{ formatCandidateContext(candidate.context) }}</span><span>目标：{{ candidate.target_ref || '未提供' }}</span><span>来源：{{ candidate.source_event_refs.join(' · ') || '未提供' }}</span><span>经验：{{ candidate.experience_ref || '未提供' }}</span><span>预算估算：{{ candidate.budget_estimate ?? '未知' }}</span><span>延迟原因：{{ candidate.defer_reason || '未提供' }}</span><span>期限：{{ formatTime(candidate.expires_at) }}</span></div>
            <div class="candidate-card__actions">
              <NButton size="small" secondary :loading="actionId === `${candidate.id}:preview`" @click="candidateAction(candidate, 'preview')">本地预览</NButton>
              <NButton v-if="candidate.state === 'expired' || candidate.defer_reason" size="small" quaternary :loading="actionId === `${candidate.id}:reassess`" @click="candidateAction(candidate, 'reassess')">重评候选</NButton>
              <NPopconfirm v-if="candidate.state === 'proposed' || candidate.state === 'draft' || candidate.state === 'prepared'" @positive-click="candidateAction(candidate, 'cancel')">
                <template #trigger><NButton size="small" type="error" quaternary :loading="actionId === `${candidate.id}:cancel`">取消候选</NButton></template>
                确认取消此候选？不会派发消息。
              </NPopconfirm>
              <RouterLink v-if="candidate.outbox_id" class="text-link" :to="'/outbox'">查看 Outbox 记录</RouterLink>
            </div>
          </article>
        </div>
        <NEmpty v-else description="没有符合条件的候选" />
        <details v-if="candidatePreview" class="candidate-preview"><summary>最近一次本地预览结果</summary><pre>{{ JSON.stringify(candidatePreview, null, 2) }}</pre></details>
      </NCard>
    </section>
  </div>
</template>

<style scoped>
.proactivity-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 400px), 1fr)); gap: 15px; align-items: start; }
.proactivity-panel { display: grid; gap: 10px; }
.proactivity-status { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; color: var(--text-muted); font-size: 11px; }
.proactivity-status strong { color: var(--text); }
.proactivity-checkbox { display: flex; align-items: center; gap: 8px; color: var(--text-soft); font-size: 11px; }
.proactivity-panel > small, .proactivity-batch-result > small { color: var(--text-muted); font-size: 10px; line-height: 1.5; }
.datetime-input { width: 100%; min-width: 0; padding: 8px 10px; border: 1px solid var(--border); border-radius: 9px; background: var(--surface); color: var(--text); font: inherit; }
.proactivity-actions { display: flex; flex-wrap: wrap; gap: 7px; }
.source-message-list { display: grid; max-height: 300px; gap: 6px; overflow: auto; }
.source-message-row { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 8px; padding: 9px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface-soft); }
.source-message-row input { margin-top: 2px; }
.source-message-row span { display: grid; min-width: 0; gap: 4px; }
.source-message-row strong { color: var(--text-soft); font-size: 10px; overflow-wrap: anywhere; }
.source-message-row small { color: var(--text-muted); font-size: 10px; line-height: 1.45; overflow-wrap: anywhere; }
.proactivity-batch-result { display: grid; gap: 7px; padding: 11px; border: 1px solid var(--border); border-radius: 11px; background: var(--surface-soft); }
.proactivity-batch-result > div { display: flex; align-items: center; justify-content: space-between; gap: 8px; color: var(--text-muted); font-size: 10px; }
.proactivity-batch-result strong { overflow-wrap: anywhere; color: var(--text-soft); }
.proactivity-batch-result pre { max-height: 250px; overflow: auto; margin: 0; color: var(--text-muted); font-size: 10px; white-space: pre-wrap; overflow-wrap: anywhere; }
.tasks-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 15px; align-items: start; }
.task-form { display: grid; gap: 8px; }
.task-list, .candidate-list { display: grid; gap: 9px; }
.task-row { display: grid; gap: 6px; width: 100%; padding: 12px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface); color: var(--text); text-align: left; cursor: pointer; }
.task-row--active { border-color: var(--primary); background: var(--primary-soft); }
.task-row__top, .task-row__bottom { display: flex; align-items: center; justify-content: space-between; gap: 9px; }
.task-row__top strong { overflow-wrap: anywhere; font-size: 12px; }
.task-row small, .task-row__bottom { color: var(--text-muted); font-size: 10px; }
.task-detail { grid-column: 1 / -1; }
.task-detail__intent { padding: 13px; border-radius: 12px; background: var(--surface-soft); color: var(--text); white-space: pre-wrap; }
.task-detail__meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 210px), 1fr)); gap: 9px 16px; }
.task-detail__meta > div { display: grid; min-width: 0; gap: 3px; }
.task-detail__meta span { color: var(--text-muted); font-size: 10px; }
.task-detail__meta strong { overflow-wrap: anywhere; color: var(--text-soft); font-size: 11px; }
.task-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; margin-top: 16px; }
.candidates-card { grid-column: 1 / -1; }
.candidate-filter { display: grid; width: min(100%, 230px); gap: 5px; margin-bottom: 12px; color: var(--text-muted); font-size: 11px; font-weight: 700; }
.candidate-card { padding: 13px; border: 1px solid var(--border); border-radius: 13px; background: var(--surface); }
.candidate-card__top { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.candidate-card__top h3 { margin: 0; color: var(--text); font-size: 13px; }
.candidate-card__top small, .candidate-card p, .candidate-card__details { color: var(--text-muted); font-size: 10px; overflow-wrap: anywhere; }
.candidate-card__top small { display: block; margin-top: 3px; }
.candidate-card p { color: var(--text-soft); }
.candidate-card__details { display: grid; gap: 4px; }
.candidate-card__actions { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; margin-top: 12px; }
.candidate-preview { margin-top: 14px; padding: 12px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-soft); }
.candidate-preview summary { color: var(--primary); font-size: 12px; font-weight: 700; cursor: pointer; }
.candidate-preview pre { max-height: 300px; overflow: auto; color: var(--text-soft); font-size: 10px; white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 920px) { .tasks-grid { grid-template-columns: 1fr; } .task-detail, .candidates-card { grid-column: auto; } }
</style>
