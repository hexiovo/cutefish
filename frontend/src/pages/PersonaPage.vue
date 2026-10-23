<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NCard, NEmpty, NInput, NPopconfirm, NSelect, NTag } from 'naive-ui'
import { RouterLink } from 'vue-router'
import { api } from '../api/client'
import { errorMessage } from '../api/errors'
import type { ChatConversation, ChatMessage, ConfirmedDesignImportInfo, ConfirmedDesignImportSource, PersonaInterview, PersonaVersion } from '../api/types'
import AppIcon from '../components/AppIcon.vue'
import StatusPill from '../components/StatusPill.vue'
import { appState } from '../stores/app'

interface ConfirmedDesignImportSummary {
  import_status: 'created' | 'already_imported'
  draft_id: string
  draft_version: number
  draft_state: string
  approved_at: string | null
  source_bundle_hash: string
  sources: ConfirmedDesignImportSource[]
  conversation_id: string
}

const current = ref<PersonaVersion | null>(null)
const versions = ref<PersonaVersion[]>([])
const interviews = ref<PersonaInterview[]>([])
const selectedVersionId = ref('')
const selectedInterviewId = ref('')
const interview = ref<PersonaInterview | null>(null)
const conversation = ref<ChatConversation | null>(null)
const messages = ref<ChatMessage[]>([])
const interviewTitle = ref('')
const versionLabel = ref('')
const versionContent = ref('')
const basedOnVersionId = ref('')
const feedbackNotes = ref<Record<string, string>>({})
const feedbackStates = ref<Record<string, string>>({})
const selectedEvidenceKey = ref('')
const loading = ref(false)
const creatingInterview = ref(false)
const savingDraft = ref(false)
const importingConfirmedDesign = ref(false)
const actionId = ref('')
const error = ref('')
const notice = ref('')
const confirmedDesignImportInfo = ref<ConfirmedDesignImportInfo | null>(null)
const confirmedDesignImportError = ref('')
const confirmedDesignImportResult = ref<ConfirmedDesignImportSummary | null>(null)
const approvalEvidence = ref<Record<string, unknown> | null>(null)

const isOwner = computed(() => appState.authenticated && appState.principal?.role === 'owner')
const selectedVersion = computed(() => versions.value.find((version) => version.id === selectedVersionId.value) ?? null)
const draftVersions = computed(() => versions.value.filter((version) => version.state === 'draft'))
const existingConfirmedDesignDraft = computed(() => {
  const bundleHash = confirmedDesignImportInfo.value?.bundle_hash
  return bundleHash
    ? versions.value.find((version) => version.source_kind === 'confirmed_design' && version.source_bundle_hash === bundleHash) ?? null
    : null
})
const confirmedDesignConversationId = computed(() => {
  const draft = existingConfirmedDesignDraft.value
  if (!draft) return confirmedDesignImportResult.value?.conversation_id ?? ''
  if (confirmedDesignImportResult.value?.draft_id === draft.id) return confirmedDesignImportResult.value.conversation_id
  return interviews.value.find((item) => item.id === draft.source_interview_id)?.conversation_id ?? ''
})
const selectedConfirmedDesignConversationId = computed(() => {
  const version = selectedVersion.value
  if (!version || version.source_kind !== 'confirmed_design') return ''
  if (confirmedDesignImportResult.value?.draft_id === version.id) return confirmedDesignImportResult.value.conversation_id
  return interviews.value.find((item) => item.id === version.source_interview_id)?.conversation_id ?? ''
})
const canImportConfirmedDesign = computed(() => {
  const info = confirmedDesignImportInfo.value
  return Boolean(isOwner.value
    && info?.available
    && info.status === 'available'
    && info.schema_version === 1
    && info.bundle_hash
    && /^[a-f0-9]{64}$/.test(info.bundle_hash)
    && info.sources.length > 0
    && info.sources.every((source) => source.status === 'verified' && /^[a-f0-9]{64}$/.test(source.sha256))
    && !existingConfirmedDesignDraft.value
    && !importingConfirmedDesign.value)
})
const versionOptions = computed(() => versions.value.map((version) => ({
  label: `v${version.version_number} · ${version.version_label} · ${version.state}`,
  value: version.id,
})))
const interviewOptions = computed(() => interviews.value.map((item) => ({
  label: `${item.title} · ${item.state}`,
  value: item.id,
})))
const messagePairs = computed(() => {
  const pairs: Array<{ inbound: ChatMessage; reply: ChatMessage; key: string; validTrial: boolean }> = []
  let inbound: ChatMessage | null = null
  for (const message of messages.value) {
    if (message.role === 'user') inbound = message
    else if (message.role === 'assistant' && inbound) {
      const key = `${inbound.id}:${message.id}`
      const usage = message.token_usage ?? {}
      const validTrial = Boolean(
        selectedVersion.value
        && conversation.value?.purpose === 'persona_trial'
        && inbound.persona_version_id === selectedVersion.value.id
        && message.persona_version_id === selectedVersion.value.id
        && message.provider === 'deepseek'
        && message.provider_response_id
        && usage.budget_state === 'settled'
        && usage.turn_state !== 'stale',
      )
      pairs.push({ inbound, reply: message, key, validTrial })
      inbound = null
    }
  }
  return pairs
})
const selectedEvidence = computed(() => messagePairs.value.find((pair) => pair.key === selectedEvidenceKey.value && pair.validTrial) ?? null)

function formatTime(value?: string | null) {
  if (!value) return '未提供'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN')
}

function usageSummary(message: ChatMessage) {
  const usage = message.token_usage
  if (!usage) return '未提供 usage'
  const state = String(usage.budget_state || 'unknown')
  const cost = usage.actual_cost === undefined ? '费用未提供' : `¥${usage.actual_cost}`
  return `${state} · ${cost}`
}

async function loadVersions() {
  try {
    const result = await api.personaVersions()
    versions.value = result.items
    if (!selectedVersionId.value) selectedVersionId.value = result.items[0]?.id ?? ''
  } catch (cause) {
    error.value = errorMessage(cause)
  }
}

async function loadInterviews() {
  try {
    interviews.value = (await api.personaInterviews()).items
    if (!selectedInterviewId.value && interviews.value.length) selectedInterviewId.value = interviews.value[0].id
    else if (selectedInterviewId.value && !interviews.value.some((item) => item.id === selectedInterviewId.value)) selectedInterviewId.value = interviews.value[0]?.id ?? ''
  } catch (cause) {
    error.value = errorMessage(cause)
  }
}

async function loadConfirmedDesignImportInfo() {
  confirmedDesignImportError.value = ''
  if (!isOwner.value) {
    confirmedDesignImportInfo.value = null
    return
  }
  try {
    confirmedDesignImportInfo.value = await api.confirmedDesignImportInfo()
  } catch (cause) {
    confirmedDesignImportInfo.value = null
    confirmedDesignImportError.value = errorMessage(cause)
  }
}

async function loadInterview(id: string) {
  if (!id) {
    interview.value = null
    conversation.value = null
    messages.value = []
    return
  }
  try {
    const result = await api.personaInterview(id)
    interview.value = result.interview
    conversation.value = result.conversation
    messages.value = result.messages
    selectedEvidenceKey.value = ''
    feedbackNotes.value = {}
    feedbackStates.value = {}
  } catch (cause) {
    error.value = errorMessage(cause)
  }
}

async function refresh() {
  loading.value = true
  error.value = ''
  try {
    const currentResult = await api.currentPersona()
    current.value = currentResult.version
    await Promise.all([loadVersions(), loadInterviews(), loadConfirmedDesignImportInfo()])
    if (selectedInterviewId.value) await loadInterview(selectedInterviewId.value)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

async function importConfirmedDesign() {
  if (!canImportConfirmedDesign.value || !confirmedDesignImportInfo.value?.bundle_hash || importingConfirmedDesign.value) return
  importingConfirmedDesign.value = true
  confirmedDesignImportError.value = ''
  confirmedDesignImportResult.value = null
  notice.value = ''
  try {
    const result = await api.importConfirmedDesign({ bundle_hash: confirmedDesignImportInfo.value.bundle_hash })
    confirmedDesignImportResult.value = {
      import_status: result.import_status,
      draft_id: result.version.id,
      draft_version: result.version.version_number,
      draft_state: result.version.state,
      approved_at: result.version.approved_at ?? null,
      source_bundle_hash: result.source_bundle_hash,
      sources: result.sources,
      conversation_id: result.conversation.id,
    }
    selectedVersionId.value = result.version.id
    await Promise.all([loadVersions(), loadInterviews(), loadConfirmedDesignImportInfo()])
    notice.value = result.import_status === 'created'
      ? '已按你的点击导入为未批准草案；当前正式人格未更改，也没有调用模型。'
      : '此来源已导入；服务端返回同一未批准草案，当前正式人格未更改。'
  } catch (cause) {
    confirmedDesignImportError.value = errorMessage(cause)
  } finally {
    importingConfirmedDesign.value = false
  }
}

async function createInterview() {
  if (creatingInterview.value) return
  creatingInterview.value = true
  error.value = ''
  notice.value = ''
  try {
    const result = await api.createPersonaInterview({ title: interviewTitle.value.trim() || undefined })
    interviewTitle.value = ''
    selectedInterviewId.value = result.interview.id
    await loadInterviews()
    await loadInterview(result.interview.id)
    notice.value = '已创建校准区访谈；只有你在实聊台明确发送时才会调用 DeepSeek。'
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    creatingInterview.value = false
  }
}

async function saveDraft() {
  if (!interview.value || savingDraft.value || !versionLabel.value.trim() || !versionContent.value.trim()) return
  savingDraft.value = true
  error.value = ''
  notice.value = ''
  try {
    const result = await api.savePersonaDraft(interview.value.id, {
      version_label: versionLabel.value.trim(),
      content: versionContent.value.trim(),
      based_on_version_id: basedOnVersionId.value || undefined,
    })
    versionLabel.value = ''
    versionContent.value = ''
    selectedVersionId.value = result.version.id
    conversation.value = result.conversation
    interview.value = result.interview
    await loadVersions()
    await loadInterview(result.interview.id)
    notice.value = `已保存不可变草案 v${result.version.version_number}。请在试聊会话里进行真实 DeepSeek 试聊后再反馈和批准。`
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    savingDraft.value = false
  }
}

async function submitFeedback(pair: { inbound: ChatMessage; reply: ChatMessage; key: string; validTrial: boolean }, action: 'keep' | 'change' | 'reject') {
  if (!interview.value || actionId.value) return
  const points = feedbackNotes.value[pair.key]?.trim()
  if (action === 'change' && !points) {
    error.value = '请写下需要调整的具体方向，再提交“修改”。'
    return
  }
  actionId.value = pair.key + ':' + action
  error.value = ''
  try {
    await api.personaFeedback(interview.value.id, {
      inbound_message_id: pair.inbound.id,
      reply_message_id: pair.reply.id,
      action,
      change_points: points || undefined,
    })
    feedbackStates.value = { ...feedbackStates.value, [pair.key]: action }
    if (action === 'keep' && pair.validTrial) selectedEvidenceKey.value = pair.key
    notice.value = action === 'keep' ? '已记录真人保留反馈。批准仍需同一人格版本的结算完成试聊证据。' : '已记录本轮真人反馈。'
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    actionId.value = ''
  }
}

async function approveSelectedEvidence() {
  const version = selectedVersion.value
  const selectedPair = selectedEvidence.value
  const currentInterview = interview.value
  if (!version || !selectedPair || !currentInterview || feedbackStates.value[selectedPair.key] !== 'keep') return
  actionId.value = 'approve'
  error.value = ''
  notice.value = ''
  approvalEvidence.value = null
  try {
    const result = await api.approvePersonaVersion(version.id, {
      interview_session_id: currentInterview.id,
      inbound_message_id: selectedPair.inbound.id,
      reply_message_id: selectedPair.reply.id,
      expected_state_revision: version.state_revision,
      version_hash: version.content_hash,
    })
    approvalEvidence.value = result.evidence
    current.value = result.version
    await loadVersions()
    notice.value = '本机服务已批准此版本，并返回所绑定的同版试聊证据。'
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    actionId.value = ''
  }
}

watch(selectedInterviewId, (id) => void loadInterview(id))
watch(selectedVersionId, () => { selectedEvidenceKey.value = '' })
onMounted(() => void refresh())
</script>

<template>
  <div class="page-stack">
    <header class="page-heading">
      <div><p class="eyebrow">真人校准与不可变版本</p><h1>人格访谈与版本</h1><p class="page-heading__description">真实 DeepSeek 回复由本机聊天服务返回；Codex 和页面不会代演、自动评分或自批人格。</p></div>
      <div class="page-heading__actions"><StatusPill :status="current?.state || 'needs_input'" /><NButton quaternary :loading="loading" @click="refresh">刷新</NButton></div>
    </header>

    <NAlert v-if="error" type="warning" :show-icon="true" title="人格操作未完成">{{ error }}</NAlert>
    <NAlert v-if="notice" type="success" :show-icon="true">{{ notice }}</NAlert>
    <NAlert type="info" :show-icon="true" title="批准证据要求">
      只有同一草案版本、配置指纹一致、费用结算完成的真实 DeepSeek 试聊，并由你对同一问答明确选择“保留”，服务端才会批准。批准会绑定你当前选择的版本 hash 和证据消息 ID。
    </NAlert>

    <NCard v-if="isOwner" class="panel-card" :bordered="false">
      <template #header>
        <div class="card-heading">
          <div><h2>已填角色设定导入</h2><p>草案正文由角色稿全文与确认设计中已选的角色约束原文摘录组成，涵盖关系、权限隔离和互动偏好；完整确认设计保留在私有归档。此处仅显示经本机核验的来源文件名与哈希，不显示源路径或私人正文。</p></div>
          <NButton type="primary" :disabled="!canImportConfirmedDesign" :loading="importingConfirmedDesign" @click="importConfirmedDesign">导入为未批准草案</NButton>
        </div>
      </template>
      <NAlert v-if="confirmedDesignImportError" type="warning" :show-icon="true" title="来源状态或导入结果暂不可用">{{ confirmedDesignImportError }}</NAlert>
      <div v-if="confirmedDesignImportInfo" class="confirmed-design-panel">
        <div class="confirmed-design-panel__status">
          <span>来源状态</span>
          <StatusPill :status="confirmedDesignImportInfo.status === 'available' ? 'ready' : confirmedDesignImportInfo.status === 'not_configured' ? 'not_connected' : 'needs_verification'" />
          <small>{{ confirmedDesignImportInfo.status === 'available' ? '来源已通过核验，可由 owner 显式导入。' : confirmedDesignImportInfo.status === 'not_configured' ? '本机服务尚未配置私有来源；页面不会扫描或寻找文件。' : '来源不可用或未通过核验；导入保持关闭。' }}</small>
        </div>
        <div class="confirmed-design-panel__sources">
          <div><span>Bundle SHA-256</span><strong>{{ confirmedDesignImportInfo.bundle_hash || '未提供' }}</strong></div>
          <div><span>Manifest schema</span><strong>{{ confirmedDesignImportInfo.schema_version ?? '未提供' }}</strong></div>
          <div v-for="source in confirmedDesignImportInfo.sources" :key="source.basename + source.sha256">
            <span>{{ source.basename }} · {{ source.status === 'verified' ? '已核验' : '不可用' }}</span>
            <strong>{{ source.sha256 }}</strong>
          </div>
        </div>
        <NAlert v-if="confirmedDesignImportInfo.context_capacity" type="info" :show-icon="true" title="上下文容量：partial">
          目标是 owner {{ confirmedDesignImportInfo.context_capacity.target_tokens.owner.toLocaleString() }} token、联系人 {{ confirmedDesignImportInfo.context_capacity.target_tokens.contact.toLocaleString() }} token；当前实际限制是 owner {{ confirmedDesignImportInfo.context_capacity.actual_max_utf8_bytes.owner.toLocaleString() }} UTF-8 bytes（{{ (confirmedDesignImportInfo.context_capacity.actual_max_utf8_bytes.owner / 1024).toLocaleString() }} KiB）、联系人 {{ confirmedDesignImportInfo.context_capacity.actual_max_utf8_bytes.contact.toLocaleString() }} UTF-8 bytes（{{ (confirmedDesignImportInfo.context_capacity.actual_max_utf8_bytes.contact / 1024).toLocaleString() }} KiB）。字节限额不等同于 token 容量，目标 token 容量尚未实现并验证。
        </NAlert>
      </div>
      <NEmpty v-else-if="!confirmedDesignImportError" description="正在读取安全来源状态；不会自动导入" />
      <div v-if="confirmedDesignImportResult || existingConfirmedDesignDraft" class="confirmed-design-result">
        <strong>导入结果：{{ confirmedDesignImportResult?.import_status === 'created' ? '已创建' : '已有同一草案' }} · 未批准</strong>
        <span>版本 {{ confirmedDesignImportResult?.draft_version ?? existingConfirmedDesignDraft?.version_number }} · {{ confirmedDesignImportResult?.draft_id || existingConfirmedDesignDraft?.id }} · 状态 {{ confirmedDesignImportResult?.draft_state || existingConfirmedDesignDraft?.state }}</span>
        <span>来源 Bundle SHA-256：{{ confirmedDesignImportResult?.source_bundle_hash || existingConfirmedDesignDraft?.source_bundle_hash }}</span>
        <span>批准时间：{{ confirmedDesignImportResult?.approved_at ?? existingConfirmedDesignDraft?.approved_at ?? '无（未批准）' }}</span>
        <div v-for="source in confirmedDesignImportResult?.sources" :key="source.basename + source.sha256" class="confirmed-design-result__source">
          <span>{{ source.basename }} · {{ source.status === 'verified' ? '已核验' : '不可用' }}</span><strong>{{ source.sha256 }}</strong>
        </div>
        <RouterLink v-if="confirmedDesignConversationId && (confirmedDesignImportResult?.draft_id || existingConfirmedDesignDraft?.id)" class="text-link" :to="{ name: 'chat', query: { conversationId: confirmedDesignConversationId, personaVersionId: confirmedDesignImportResult?.draft_id || existingConfirmedDesignDraft?.id } }">打开绑定的校准会话</RouterLink>
      </div>
      <p v-if="confirmedDesignImportInfo?.available && !canImportConfirmedDesign && !existingConfirmedDesignDraft && !importingConfirmedDesign" class="muted-copy">当前来源不满足 schema、哈希或完整性门控；没有发送导入请求。</p>
    </NCard>

    <section class="persona-grid">
      <NCard class="panel-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>当前批准版本</h2><p>正式对话使用这个不可变版本。</p></div><AppIcon name="shield" /></div></template>
        <div v-if="current" class="persona-version-card">
          <div class="persona-version-card__top"><strong>v{{ current.version_number }} · {{ current.version_label }}</strong><StatusPill :status="current.state" /></div>
          <small>{{ current.id }} · {{ current.content_hash }}</small>
          <p>{{ current.content }}</p>
          <small>批准：{{ formatTime(current.approved_at) }} · 配置指纹：{{ current.config_fingerprint }}</small>
          <RouterLink class="text-link" :to="{ name: 'chat' }">打开正式实聊台</RouterLink>
        </div>
        <NEmpty v-else description="尚无已批准人格版本；普通正式实聊会被服务端阻止" />
      </NCard>

      <NCard class="panel-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>版本历史</h2><p>新版本不会覆盖已批准或历史正文。</p></div><AppIcon name="book" /></div></template>
        <label class="persona-filter"><span>选择版本</span><NSelect v-model:value="selectedVersionId" :options="versionOptions" placeholder="选择人格版本" /></label>
        <div v-if="selectedVersion" class="persona-version-detail">
          <div><span>来源访谈</span><strong>{{ selectedVersion.source_interview_id || '未提供' }}</strong></div>
          <div><span>来源请求</span><strong>{{ selectedVersion.source_request_id || '未提供' }}</strong></div>
          <div><span>基于版本</span><strong>{{ selectedVersion.based_on_version_id || '无' }}</strong></div>
          <div><span>配置指纹</span><strong>{{ selectedVersion.config_fingerprint }}</strong></div>
          <div><span>state revision</span><strong>{{ selectedVersion.state_revision }}</strong></div>
          <div><span>创建时间</span><strong>{{ formatTime(selectedVersion.created_at) }}</strong></div>
          <template v-if="selectedVersion.source_kind === 'confirmed_design'">
            <div><span>正文构成</span><strong>{{ selectedVersion.contains_confirmed_constraint_excerpts ? '角色稿全文 + 已选确认约束原文摘录' : '角色稿全文' }}</strong></div>
            <div v-if="selectedVersion.confirmed_constraint_refs?.length"><span>已选约束引用</span><strong>{{ selectedVersion.confirmed_constraint_refs.join(' · ') }}</strong></div>
            <small>此导入草案的私人正文留在服务端；这里只显示来源状态、哈希和逻辑引用。</small>
          </template>
          <RouterLink v-if="selectedVersion.state === 'draft' && selectedVersion.source_kind === 'confirmed_design' && selectedConfirmedDesignConversationId" class="text-link" :to="{ name: 'chat', query: { conversationId: selectedConfirmedDesignConversationId, personaVersionId: selectedVersion.id } }">在绑定的校准会话中试聊</RouterLink>
          <pre v-if="selectedVersion.source_kind !== 'confirmed_design'">{{ selectedVersion.content }}</pre>
        </div>
        <NEmpty v-else description="还没有人格版本" />
      </NCard>
    </section>

    <section class="persona-grid persona-grid--wide">
      <NCard class="panel-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>真人格访谈</h2><p>访谈会话使用 calibration 隔离范围。</p></div><AppIcon name="chat" /></div></template>
        <div class="interview-create"><NInput v-model:value="interviewTitle" maxlength="256" placeholder="访谈名称（可选）" /><NButton type="primary" :loading="creatingInterview" @click="createInterview">新建访谈</NButton></div>
        <label class="persona-filter"><span>访谈记录</span><NSelect v-model:value="selectedInterviewId" :options="interviewOptions" placeholder="选择访谈" /></label>
        <div v-if="interview && conversation" class="interview-summary">
          <div class="interview-summary__top"><div><strong>{{ interview.title }}</strong><small>{{ interview.id }} · {{ interview.state }} · {{ formatTime(interview.created_at) }}</small></div><StatusPill :status="conversation.state" /></div>
          <small>绑定人格版本：{{ interview.persona_version_id || conversation.persona_version_id || '尚未创建草案' }} · revision {{ conversation.revision }} · {{ conversation.purpose }}</small>
          <RouterLink class="text-link" :to="{ name: 'chat', query: { conversationId: conversation.id } }">在实聊台打开此会话</RouterLink>
        </div>
        <NEmpty v-else description="选择或新建一次访谈" />
      </NCard>

      <NCard v-if="interview && conversation" class="panel-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>创建不可变草案</h2><p>草案正文由你确认；保存后只能创建新版本。</p></div><AppIcon name="edit" /></div></template>
        <div class="draft-form">
          <label class="field-label" for="persona-label">版本名称</label><NInput id="persona-label" v-model:value="versionLabel" maxlength="128" placeholder="例如：亲情妹妹感 v1 草案" />
          <label class="field-label" for="persona-based-on">基于已有版本（可选）</label><NSelect id="persona-based-on" v-model:value="basedOnVersionId" :options="versionOptions" clearable placeholder="选择一个版本作参考" />
          <label class="field-label" for="persona-content">版本正文</label><NInput id="persona-content" v-model:value="versionContent" type="textarea" :autosize="{ minRows: 6, maxRows: 16 }" maxlength="24000" placeholder="填写经过访谈整理并由你确认的人格规则与表达边界。" />
          <NButton type="primary" :disabled="!versionLabel.trim() || !versionContent.trim()" :loading="savingDraft" @click="saveDraft">保存为新版本</NButton>
        </div>
      </NCard>
    </section>

    <NCard v-if="interview && conversation" class="panel-card" :bordered="false">
      <template #header><div class="card-heading"><div><h2>真实访谈与试聊证据</h2><p>每条问答可单独反馈。通过按钮发出的每轮会产生真实调用费用。</p></div><NTag round type="info">{{ messages.length }} 条消息</NTag></div></template>
      <NAlert v-if="conversation.state !== 'active'" type="warning" :show-icon="true" class="runtime-note">当前会话状态为 {{ conversation.state }}；在实聊台查看并按需停止或接管。</NAlert>
      <div v-if="messagePairs.length" class="persona-pairs">
        <article v-for="pair in messagePairs" :key="pair.key" class="persona-pair" :class="{ 'persona-pair--evidence': pair.validTrial }">
          <div class="persona-pair__top"><span>同一轮 · {{ pair.inbound.id }} / {{ pair.reply.id }}</span><NTag v-if="pair.validTrial" round size="small" type="success">可作为同版证据候选</NTag><StatusPill v-else :status="pair.reply.persona_version_id ? 'needs_verification' : 'partial'" /></div>
          <div class="persona-pair__message"><strong>你的真实输入</strong><p>{{ pair.inbound.content }}</p></div>
          <div class="persona-pair__message"><strong>DeepSeek 返回</strong><p>{{ pair.reply.content }}</p><small>{{ pair.reply.provider || 'provider 未提供' }} · {{ pair.reply.model || 'model 未提供' }} · {{ usageSummary(pair.reply) }}</small></div>
          <label class="field-label" :for="`feedback-${pair.reply.id}`">需要修改的地方（选择“修改”时必填）</label>
          <NInput :id="`feedback-${pair.reply.id}`" v-model:value="feedbackNotes[pair.key]" maxlength="4000" placeholder="具体描述应保留或调整的表达" />
          <div class="persona-pair__actions">
            <NButton size="small" :loading="actionId === `${pair.key}:keep`" @click="submitFeedback(pair, 'keep')">保留这轮</NButton>
            <NButton size="small" secondary :loading="actionId === `${pair.key}:change`" @click="submitFeedback(pair, 'change')">记录修改意见</NButton>
            <NButton size="small" quaternary type="error" :loading="actionId === `${pair.key}:reject`" @click="submitFeedback(pair, 'reject')">拒绝这轮</NButton>
            <NButton v-if="pair.validTrial && feedbackStates[pair.key] === 'keep'" size="small" type="primary" :loading="actionId === 'approve'" @click="selectedEvidenceKey = pair.key">选作批准证据</NButton>
          </div>
          <small v-if="feedbackStates[pair.key]">本次页面已记录反馈：{{ feedbackStates[pair.key] }}</small>
        </article>
      </div>
      <NEmpty v-else description="还没有完整的用户输入与 DeepSeek 回复配对" />
      <div class="approve-panel">
        <div><strong>批准所选人格草案</strong><small>{{ selectedEvidence ? `证据对：${selectedEvidence.inbound.id} → ${selectedEvidence.reply.id}` : '先选择一条同版本、费用已结算并获得“保留”反馈的真实试聊。' }}</small></div>
        <NPopconfirm :disabled="!selectedVersion || selectedVersion.state !== 'draft' || !selectedEvidence || !interview || feedbackStates[selectedEvidence.key] !== 'keep'" @positive-click="approveSelectedEvidence">
          <template #trigger><NButton type="primary" :disabled="!selectedVersion || selectedVersion.state !== 'draft' || !selectedEvidence || !interview || feedbackStates[selectedEvidence.key] !== 'keep'" :loading="actionId === 'approve'">人工批准此版本</NButton></template>
          确认批准 {{ selectedVersion?.version_label }}？后端会再次校验同版本的真实 DeepSeek 试聊和真人“保留”反馈。
        </NPopconfirm>
      </div>
      <details v-if="approvalEvidence" class="approval-evidence"><summary>本机批准证据</summary><pre>{{ JSON.stringify(approvalEvidence, null, 2) }}</pre></details>
    </NCard>
  </div>
</template>

<style scoped>
.persona-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 15px; align-items: start; }
.persona-grid--wide { margin-top: 0; }
.persona-version-card { display: grid; gap: 8px; }
.persona-version-card__top { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
.persona-version-card__top strong { color: var(--text); font-size: 14px; }
.persona-version-card small, .interview-summary small { color: var(--text-muted); font-size: 10px; overflow-wrap: anywhere; }
.persona-version-card p { max-height: 240px; overflow: auto; margin: 4px 0; padding: 12px; border-radius: 11px; background: var(--surface-soft); color: var(--text); white-space: pre-wrap; }
.persona-filter { display: grid; gap: 5px; margin: 8px 0 12px; color: var(--text-muted); font-size: 11px; font-weight: 700; }
.persona-version-detail { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 200px), 1fr)); gap: 8px 13px; }
.persona-version-detail > div { display: grid; gap: 2px; }
.persona-version-detail span { color: var(--text-muted); font-size: 10px; }
.persona-version-detail strong { overflow-wrap: anywhere; color: var(--text-soft); font-size: 10px; }
.persona-version-detail pre { grid-column: 1 / -1; max-height: 280px; overflow: auto; margin: 4px 0 0; padding: 12px; border-radius: 10px; background: var(--surface-soft); color: var(--text); font-size: 11px; white-space: pre-wrap; }
.confirmed-design-panel, .confirmed-design-panel__sources, .confirmed-design-result { display: grid; gap: 9px; }
.confirmed-design-panel__status, .confirmed-design-panel__sources > div, .confirmed-design-result, .confirmed-design-result__source { display: grid; gap: 4px; padding: 10px; border-radius: 10px; background: var(--surface-soft); }
.confirmed-design-panel__status > span, .confirmed-design-panel__sources span, .confirmed-design-result > span, .confirmed-design-result__source span { color: var(--text-muted); font-size: 10px; }
.confirmed-design-panel__status small { color: var(--text-muted); font-size: 10px; }
.confirmed-design-panel__sources strong, .confirmed-design-result__source strong, .confirmed-design-result > span { overflow-wrap: anywhere; }
.confirmed-design-result > strong { color: var(--text); font-size: 12px; }
.interview-create { display: flex; gap: 8px; }
.interview-summary { display: grid; gap: 8px; margin-top: 12px; padding: 12px; border-radius: 12px; background: var(--surface-soft); }
.interview-summary__top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.interview-summary__top > div { display: grid; gap: 3px; }
.interview-summary__top strong { color: var(--text); font-size: 12px; }
.draft-form { display: grid; gap: 8px; }
.persona-pairs { display: grid; gap: 12px; }
.persona-pair { display: grid; gap: 8px; padding: 13px; border: 1px solid var(--border); border-radius: 13px; background: var(--surface); }
.persona-pair--evidence { border-color: color-mix(in srgb, var(--success) 45%, var(--border)); }
.persona-pair__top, .persona-pair__actions { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; }
.persona-pair__top { justify-content: space-between; color: var(--text-muted); font-size: 10px; }
.persona-pair__message { padding: 10px; border-radius: 10px; background: var(--surface-soft); }
.persona-pair__message strong { color: var(--text-muted); font-size: 10px; }
.persona-pair__message p { margin: 5px 0 0; color: var(--text); font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
.persona-pair__message small { color: var(--text-muted); font-size: 10px; }
.approve-panel { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; margin-top: 18px; padding: 13px; border-radius: 12px; background: var(--surface-soft); }
.approve-panel > div { display: grid; gap: 4px; }
.approve-panel strong { color: var(--text); font-size: 12px; }
.approve-panel small { color: var(--text-muted); font-size: 10px; overflow-wrap: anywhere; }
.approval-evidence { margin-top: 14px; padding: 12px; border: 1px solid var(--border); border-radius: 11px; }
.approval-evidence summary { color: var(--primary); font-size: 11px; font-weight: 700; cursor: pointer; }
.approval-evidence pre { max-height: 280px; overflow: auto; font-size: 10px; white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 820px) { .persona-grid { grid-template-columns: 1fr; } .interview-create { flex-direction: column; } }
</style>
