<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { NAlert, NButton, NCard, NEmpty, NInput, NPopconfirm, NSelect, NTag } from 'naive-ui'
import { ApiRequestError, api } from '../api/client'
import { errorMessage } from '../api/errors'
import type { AsrCapabilities, AsrTranscriptionResult, ChatConversation, ChatMessage, ChatTurnResult, OutboxItem, RoomItem, TtsCapabilities, TrialBatch, VoiceArtifact, VoiceOutboxAuthorizationResult, VoiceProfileData } from '../api/types'
import AppIcon from '../components/AppIcon.vue'
import StatusPill from '../components/StatusPill.vue'

const tts = ref<TtsCapabilities | null>(null)
const asr = ref<AsrCapabilities | null>(null)
const voicesLoading = ref(false)
const voiceProfileLoading = ref(false)
const savingVoiceProfile = ref(false)
const savingVoiceSettings = ref(false)
const approvingVoice = ref(false)
const asrLoading = ref(false)
const previewing = ref(false)
const loadingRooms = ref(false)
const transcribing = ref(false)
const error = ref('')
const notice = ref('')
const sampleText = ref('你好呀，我想用本机语音试听一小段。')
const selectedVoiceId = ref('')
const voiceProfile = ref<VoiceProfileData | null>(null)
const approvalPreview = ref<VoiceArtifact | null>(null)
const approvalPreviewUncertain = ref(false)
const audioUrl = ref('')
const replyArtifact = ref<VoiceArtifact | null>(null)
const replyAudioUrl = ref('')
const replyGenerationUncertain = ref(false)
const replyArtifactBinding = ref<VoiceReplyBinding | null>(null)
const replyAuthorization = ref<VoiceOutboxAuthorizationResult | null>(null)
const replyOutboxItem = ref<OutboxItem | null>(null)
const replyPrepareIdempotencyKey = ref('')
const authorizingVoiceReply = ref(false)
const preparingVoiceReplyOutbox = ref(false)
const voiceAuthorizationUncertain = ref(false)
const voicePreparationUncertain = ref(false)
const authorizationClock = ref(Date.now())
const voiceConversations = ref<ChatConversation[]>([])
const selectedVoiceConversationId = ref('')
const voiceBatches = ref<TrialBatch[]>([])
const selectedVoiceBatchId = ref('')
const voiceMessages = ref<ChatMessage[]>([])
const loadingVoiceConversations = ref(false)
const loadingVoiceMessages = ref(false)
const loadingVoiceBatches = ref(false)
const creatingVoiceBatch = ref(false)
const startingVoiceBatch = ref(false)
const stoppingVoiceBatch = ref(false)
const generatingVoiceReply = ref(false)
const roomItems = ref<RoomItem[]>([])
const selectedRoomItemId = ref('')
const transcription = ref<AsrTranscriptionResult | null>(null)
const conversations = ref<ChatConversation[]>([])
const ownerPersonId = ref('')
const selectedConversationId = ref('')
const loadingConversations = ref(false)
const submittingTranscript = ref(false)
const chatResult = ref<ChatTurnResult | null>(null)
const chatUncertain = ref(false)
const lastChatRequestId = ref('')
let replyAuthorizationExpiryTimer = 0

interface VoiceReplyBinding {
  conversation_id: string
  expected_revision: number
  source_message_id: string
  batch_id: string
  batch_version: number
  account_id: string
  recipient_id: string
}

const voiceOptions = computed(() => (tts.value?.voices ?? []).map((voice) => ({
  label: `${voice.name} · ${voice.culture}`,
  value: voice.voice_id,
})))
const selectedRoomItem = computed(() => roomItems.value.find((item) => item.id === selectedRoomItemId.value) ?? null)
const mediaItemOptions = computed(() => roomItems.value
  .filter((item) => Boolean(item.managed_media_id))
  .map((item) => ({ label: `${item.title} · ${item.id} · v${item.version}`, value: item.id })))
const conversationOptions = computed(() => conversations.value
  .filter((conversation) => conversation.scope === 'production' && conversation.purpose === 'production' && conversation.channel === 'ui')
  .map((conversation) => ({ label: `${conversation.title || conversation.id} · ${conversation.id}`, value: conversation.id })))
const selectedConversation = computed(() => conversations.value.find((conversation) => conversation.id === selectedConversationId.value) ?? null)
const selectedVoiceConversation = computed(() => voiceConversations.value.find((conversation) => conversation.id === selectedVoiceConversationId.value) ?? null)
const currentVoiceBatch = computed(() => voiceBatches.value.find((batch) => batch.id === selectedVoiceBatchId.value) ?? null)
const voiceBatchOptions = computed(() => voiceBatches.value.map((batch) => ({ label: `${batch.label || batch.id} · ${batch.state} · v${batch.version ?? '?'}`, value: batch.id })))
const voiceConversationOptions = computed(() => voiceConversations.value
  .map((conversation) => ({ label: `${conversation.title || conversation.id} · ${conversation.id}`, value: conversation.id })))
const latestVoiceReply = computed(() => {
  const ordered = [...voiceMessages.value].sort((left, right) => Date.parse(left.created_at) - Date.parse(right.created_at))
  const latest = ordered.at(-1)
  return latest?.role === 'assistant' ? latest : null
})
const canSaveVoiceProfile = computed(() => Boolean(
  voiceProfile.value
  && selectedVoiceId.value
  && tts.value?.voices.some((voice) => voice.voice_id === selectedVoiceId.value)
  && voiceProfile.value.profile?.voice_id !== selectedVoiceId.value
  && !savingVoiceProfile.value
))
const canApprovePreview = computed(() => Boolean(
  voiceProfile.value?.profile
  && approvalPreview.value?.purpose === 'approval_preview'
  && approvalPreview.value.profile_version === voiceProfile.value.profile.profile_version
  && voiceProfile.value.approval?.profile_version !== voiceProfile.value.profile.profile_version
  && !approvingVoice.value
))
const canCreateVoiceBatch = computed(() => Boolean(
  voiceProfile.value?.deployment_profile === 'local_trial'
  && voiceProfile.value.runtime_enabled
  && voiceProfile.value.voice_outbox_enabled
  && voiceProfile.value.approval?.profile_version === voiceProfile.value.profile?.profile_version
  && selectedVoiceConversation.value?.person_id === ownerPersonId.value
  && selectedVoiceConversation.value?.account_id
  && selectedVoiceConversation.value?.recipient_id
  && selectedVoiceConversation.value?.state === 'active'
  && !selectedVoiceConversation.value.stop_requested
  && !selectedVoiceConversation.value.human_takeover
  && !voiceBatches.value.some((batch) => batch.state === 'draft' || batch.state === 'running')
  && !creatingVoiceBatch.value
))
const canStartVoiceBatch = computed(() => Boolean(
  currentVoiceBatch.value?.state === 'draft'
  && currentVoiceBatch.value.version
  && selectedVoiceConversation.value?.account_id
  && selectedVoiceConversation.value?.recipient_id
  && voiceProfile.value?.deployment_profile === 'local_trial'
  && !startingVoiceBatch.value
))
const canGenerateVoiceReply = computed(() => Boolean(
  voiceProfile.value?.deployment_profile === 'local_trial'
  && voiceProfile.value.runtime_enabled
  && voiceProfile.value.voice_outbox_enabled
  && voiceProfile.value.approval?.profile_version === voiceProfile.value.profile?.profile_version
  && currentVoiceBatch.value?.state === 'running'
  && currentVoiceBatch.value.target?.module_id === 'qq'
  && currentVoiceBatch.value.target?.capability_id === 'qq.voice.send'
  && currentVoiceBatch.value.target?.object_ids?.length === 1
  && currentVoiceBatch.value.target.object_ids[0] === selectedVoiceConversation.value?.recipient_id
  && selectedVoiceConversation.value?.scope === 'production'
  && selectedVoiceConversation.value?.purpose === 'production'
  && selectedVoiceConversation.value?.channel === 'qq'
  && selectedVoiceConversation.value?.state === 'active'
  && !selectedVoiceConversation.value.stop_requested
  && !selectedVoiceConversation.value.human_takeover
  && latestVoiceReply.value
  && !generatingVoiceReply.value
  && !replyGenerationUncertain.value
  && !replyArtifact.value,
))
const voiceOutboxProfileReady = computed(() => {
  const profile = voiceProfile.value
  return Boolean(profile
    && profile.deployment_profile === 'local_trial'
    && profile.production_enabled === false
    && profile.readiness === 'ready'
    && profile.runtime_enabled
    && profile.voice_outbox_enabled
    && profile.profile
    && profile.approval?.profile_id === profile.profile.profile_id
    && profile.approval.profile_version === profile.profile.profile_version)
})
const canAuthorizeVoiceReply = computed(() => Boolean(
  voiceOutboxProfileReady.value
  && replyArtifact.value?.purpose === 'reply'
  && replyArtifact.value.artifact_state === 'ready'
  && replyArtifactBinding.value
  && selectedVoiceConversationId.value === replyArtifactBinding.value.conversation_id
  && selectedVoiceBatchId.value === replyArtifactBinding.value.batch_id
  && (!replyAuthorization.value
    || replyAuthorization.value.state !== 'active'
    || Date.parse(replyAuthorization.value.expires_at) <= authorizationClock.value)
  && !replyOutboxItem.value
  && !authorizingVoiceReply.value
  && !voiceAuthorizationUncertain.value
  && !voicePreparationUncertain.value
  && !loadingVoiceConversations.value
  && !loadingVoiceMessages.value
  && !loadingVoiceBatches.value
  && !voiceProfileLoading.value
))
const canPrepareVoiceReplyOutbox = computed(() => Boolean(
  voiceOutboxProfileReady.value
  && replyArtifact.value
  && replyArtifactBinding.value
  && replyAuthorization.value?.state === 'active'
  && authorizationMatchesReply(replyAuthorization.value)
  && !replyOutboxItem.value
  && !preparingVoiceReplyOutbox.value
  && !voiceAuthorizationUncertain.value
  && !voicePreparationUncertain.value
  && !loadingVoiceConversations.value
  && !loadingVoiceMessages.value
  && !loadingVoiceBatches.value
  && !voiceProfileLoading.value
))
const canSubmitTranscript = computed(() => Boolean(
  transcription.value?.transcript
  && selectedConversation.value?.state === 'active'
  && !selectedConversation.value.stop_requested
  && !selectedConversation.value.human_takeover
  && !chatUncertain.value
  && !chatResult.value,
))

function clearPreview() {
  if (audioUrl.value) URL.revokeObjectURL(audioUrl.value)
  audioUrl.value = ''
  approvalPreview.value = null
  approvalPreviewUncertain.value = false
}

function clearReplyArtifact() {
  if (replyAuthorizationExpiryTimer) window.clearTimeout(replyAuthorizationExpiryTimer)
  replyAuthorizationExpiryTimer = 0
  if (replyAudioUrl.value) URL.revokeObjectURL(replyAudioUrl.value)
  replyAudioUrl.value = ''
  replyArtifact.value = null
  replyArtifactBinding.value = null
  replyAuthorization.value = null
  replyOutboxItem.value = null
  replyPrepareIdempotencyKey.value = ''
  replyGenerationUncertain.value = false
  voiceAuthorizationUncertain.value = false
  voicePreparationUncertain.value = false
}

function time(value?: string | null) {
  if (!value) return '未提供'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN')
}

watch(replyAuthorization, (authorization) => {
  if (replyAuthorizationExpiryTimer) window.clearTimeout(replyAuthorizationExpiryTimer)
  replyAuthorizationExpiryTimer = 0
  if (!authorization) return
  const delay = Date.parse(authorization.expires_at) - Date.now()
  if (Number.isFinite(delay) && delay > 0) {
    replyAuthorizationExpiryTimer = window.setTimeout(() => {
      authorizationClock.value = Date.now()
      replyAuthorizationExpiryTimer = 0
    }, delay + 25)
  } else authorizationClock.value = Date.now()
})

function isUnknownMutationOutcome(value: unknown) {
  return value instanceof ApiRequestError && (
    value.retryable
    || value.httpStatus !== null && value.httpStatus >= 500
    || ['not_connected', 'unknown', 'invalid_response', 'missing_data'].includes(value.code)
  )
}

function isUnknownChatFailure(value: unknown) {
  return value instanceof ApiRequestError && (value.retryable || ['not_connected', 'unknown', 'provider_timeout', 'internal_error'].includes(value.code))
}

function requestIdFromError(value: unknown) {
  if (!(value instanceof ApiRequestError) || typeof value.details !== 'object' || value.details === null) return ''
  const id = (value.details as Record<string, unknown>).local_request_id
  return typeof id === 'string' ? id : ''
}

async function loadTtsCapabilities() {
  voicesLoading.value = true
  error.value = ''
  notice.value = ''
  try {
    tts.value = await api.ttsCapabilities()
    if (!selectedVoiceId.value || !tts.value.voices.some((voice) => voice.voice_id === selectedVoiceId.value)) {
      selectedVoiceId.value = tts.value.voices[0]?.voice_id ?? ''
    }
    notice.value = '本机语音状态已读取；页面没有播放或批准任何声音。'
  } catch (cause) {
    error.value = errorMessage(cause)
    tts.value = null
  } finally {
    voicesLoading.value = false
  }
}

async function loadVoiceProfile() {
  voiceProfileLoading.value = true
  error.value = ''
  try {
    voiceProfile.value = await api.voiceProfile()
    const configuredVoice = voiceProfile.value.profile?.voice_id
    if (configuredVoice) selectedVoiceId.value = configuredVoice
  } catch (cause) {
    error.value = errorMessage(cause)
    voiceProfile.value = null
  } finally {
    voiceProfileLoading.value = false
  }
}

async function saveVoiceProfile() {
  const current = voiceProfile.value
  if (!current || !selectedVoiceId.value || !tts.value?.voices.some((voice) => voice.voice_id === selectedVoiceId.value) || savingVoiceProfile.value) return
  savingVoiceProfile.value = true
  error.value = ''
  notice.value = ''
  try {
    voiceProfile.value = await api.saveVoiceProfile({ expected_revision: current.revision, voice_id: selectedVoiceId.value })
    clearPreview()
    clearReplyArtifact()
    notice.value = '不可变声音 profile 已更新；旧批准已撤销。请为新版本重新试听并批准。'
  } catch (cause) {
    const message = errorMessage(cause)
    await loadVoiceProfile()
    error.value = message
    if (voiceProfile.value?.profile?.profile_version !== current.profile?.profile_version) {
      clearPreview()
      clearReplyArtifact()
      notice.value = 'profile 状态已刷新；保存结果已核对，没有自动重试。'
    } else if (cause instanceof ApiRequestError && cause.httpStatus === 409) {
      notice.value = 'profile 版本已变化；已刷新当前状态，没有自动覆盖。'
    }
  } finally {
    savingVoiceProfile.value = false
  }
}

async function saveVoiceSettings(next: { runtime_enabled: boolean; voice_outbox_enabled: boolean }) {
  const current = voiceProfile.value
  if (!current || current.deployment_profile !== 'local_trial' || savingVoiceSettings.value) return
  savingVoiceSettings.value = true
  error.value = ''
  notice.value = ''
  try {
    voiceProfile.value = await api.saveVoiceSettings({ expected_revision: current.revision, ...next })
    if (!next.runtime_enabled || !next.voice_outbox_enabled) clearReplyArtifact()
    notice.value = '语音运行与本机语音外发开关已由你更新；这不会批准 profile、创建授权或发送语音。'
  } catch (cause) {
    const message = errorMessage(cause)
    await loadVoiceProfile()
    error.value = message
    if (voiceProfile.value?.runtime_enabled !== next.runtime_enabled || voiceProfile.value?.voice_outbox_enabled !== next.voice_outbox_enabled) {
      notice.value = '设置结果已重新读取；没有自动重试。'
    }
  } finally {
    savingVoiceSettings.value = false
  }
}

async function createApprovalPreview() {
  const current = voiceProfile.value
  if (!current?.profile || !sampleText.value.trim() || sampleText.value.length > 240 || previewing.value || approvalPreviewUncertain.value) return
  previewing.value = true
  error.value = ''
  notice.value = ''
  clearPreview()
  try {
    const artifact = await api.createVoicePreview({ expected_profile_version: current.profile.profile_version, text: sampleText.value.trim() })
    approvalPreview.value = artifact
    await loadApprovalAudio()
    notice.value = '审批试听素材已生成并读回；播放器需要你手动播放。生成素材没有批准声音。'
  } catch (cause) {
    error.value = errorMessage(cause)
    if (cause instanceof ApiRequestError && (!cause.httpStatus || cause.retryable || cause.httpStatus >= 500)) {
      approvalPreviewUncertain.value = true
      notice.value = '试听素材生成结果未知；当前页面已锁定重复生成。请刷新 profile 状态后再决定下一步。'
    }
  } finally {
    previewing.value = false
  }
}

async function loadApprovalAudio() {
  if (!approvalPreview.value) return
  try {
    if (audioUrl.value) URL.revokeObjectURL(audioUrl.value)
    audioUrl.value = URL.createObjectURL(await api.voiceArtifactContent(approvalPreview.value.artifact_id))
  } catch (cause) {
    error.value = errorMessage(cause)
  }
}

async function approvePreview() {
  const current = voiceProfile.value
  const artifact = approvalPreview.value
  if (!current?.profile || !artifact || artifact.purpose !== 'approval_preview' || artifact.profile_version !== current.profile.profile_version || approvingVoice.value) return
  approvingVoice.value = true
  error.value = ''
  notice.value = ''
  try {
    await api.approveVoicePreview({ expected_profile_version: current.profile.profile_version, preview_artifact_id: artifact.artifact_id })
    await loadVoiceProfile()
    notice.value = '服务端已记录此 profile 版本的明确批准。批准只绑定当前 profile version，不会创建 QQ 外发授权。'
  } catch (cause) {
    const message = errorMessage(cause)
    if (cause instanceof ApiRequestError && (!cause.httpStatus || cause.retryable || cause.httpStatus >= 500)) {
      await loadVoiceProfile()
      error.value = message
      notice.value = '批准请求结果未知；已查询当前 profile 状态，没有自动重发批准请求。'
    } else if (cause instanceof ApiRequestError && cause.httpStatus === 409) {
      await loadVoiceProfile()
      error.value = message
    } else error.value = message
  } finally {
    approvingVoice.value = false
  }
}

async function loadVoiceConversations() {
  loadingVoiceConversations.value = true
  error.value = ''
  notice.value = ''
  try {
    const [conversationResult, peopleResult] = await Promise.all([api.conversations('production'), api.people()])
    ownerPersonId.value = peopleResult.items.find((person) => person.role === 'owner')?.person_id ?? ''
    voiceConversations.value = conversationResult.items.filter((conversation) => conversation.person_id === ownerPersonId.value && conversation.scope === 'production' && conversation.purpose === 'production' && conversation.channel === 'qq')
    if (!voiceConversations.value.some((conversation) => conversation.id === selectedVoiceConversationId.value)) {
      selectedVoiceConversationId.value = voiceConversations.value[0]?.id ?? ''
    }
    notice.value = `已读取 ${voiceConversations.value.length} 个本人的 production QQ 对话；没有生成或发送语音。`
  } catch (cause) {
    error.value = errorMessage(cause)
    voiceConversations.value = []
  } finally {
    loadingVoiceConversations.value = false
  }
}

function isEligibleVoiceBatch(batch: TrialBatch, conversation: ChatConversation) {
  const expiresAt = batch.expires_at ? Date.parse(batch.expires_at) : 0
  return Boolean(
    batch.target?.module_id === 'qq'
    && batch.target.capability_id === 'qq.voice.send'
    && batch.target.object_ids?.length === 1
    && batch.target.object_ids[0] === conversation.recipient_id
    && batch.stop_condition?.kind === 'after_n'
    && batch.stop_condition.max_actions === 1
    && expiresAt > Date.now()
    && (batch.state === 'draft' || batch.state === 'running'),
  )
}

async function loadVoiceBatches() {
  const conversation = selectedVoiceConversation.value
  if (!conversation?.recipient_id) {
    voiceBatches.value = []
    selectedVoiceBatchId.value = ''
    return
  }
  loadingVoiceBatches.value = true
  error.value = ''
  try {
    const result = await api.batches()
    if (selectedVoiceConversationId.value !== conversation.id) return
    voiceBatches.value = result.items.filter((batch) => isEligibleVoiceBatch(batch, conversation))
    if (!voiceBatches.value.some((batch) => batch.id === selectedVoiceBatchId.value)) {
      selectedVoiceBatchId.value = voiceBatches.value.find((batch) => batch.state === 'running')?.id
        || voiceBatches.value[0]?.id
        || ''
    }
  } catch (cause) {
    error.value = errorMessage(cause)
    voiceBatches.value = []
    selectedVoiceBatchId.value = ''
  } finally {
    loadingVoiceBatches.value = false
  }
}

async function createVoiceBatch() {
  const current = selectedVoiceConversation.value
  if (!current || !canCreateVoiceBatch.value || creatingVoiceBatch.value) return
  creatingVoiceBatch.value = true
  error.value = ''
  notice.value = ''
  try {
    const detail = await api.conversation(current.id)
    const conversation = detail.conversation
    if (conversation.person_id !== ownerPersonId.value
      || conversation.scope !== 'production'
      || conversation.purpose !== 'production'
      || conversation.channel !== 'qq'
      || conversation.state !== 'active'
      || conversation.stop_requested
      || conversation.human_takeover
      || !conversation.account_id
      || !conversation.recipient_id) {
      throw new Error('会话不再满足本人 production QQ 单目标批次要求；请刷新后核对。')
    }
    const recentBatches = await api.batches()
    const existing = recentBatches.items.filter((batch) => isEligibleVoiceBatch(batch, conversation))
    if (existing.length) {
      voiceBatches.value = existing
      selectedVoiceBatchId.value = existing.find((batch) => batch.state === 'running')?.id || existing[0].id
      throw new Error('该收件目标已有未过期的单次语音批次；已载入现有批次，请勿重复创建。')
    }
    const created = await api.createBatch({
      label: `单条 QQ 语音 · ${new Date().toLocaleString('zh-CN')}`,
      target: { module_id: 'qq', capability_id: 'qq.voice.send', object_ids: [conversation.recipient_id] },
      expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
      stop_condition: { kind: 'after_n', max_actions: 1 },
    })
    voiceBatches.value = [created]
    selectedVoiceBatchId.value = created.id
    notice.value = `已创建只绑定此 owner recipient 的一小时单动作语音批次（${created.id}）。它仍是草稿，尚未启动或发送。`
  } catch (cause) {
    error.value = errorMessage(cause)
    await loadVoiceBatches()
    error.value = errorMessage(cause)
  } finally {
    creatingVoiceBatch.value = false
  }
}

async function startVoiceBatch() {
  const batch = currentVoiceBatch.value
  if (!batch?.version || !canStartVoiceBatch.value) return
  startingVoiceBatch.value = true
  error.value = ''
  notice.value = ''
  try {
    const started = await api.startBatch(batch.id)
    voiceBatches.value = voiceBatches.value.map((item) => item.id === started.id ? started : item)
    clearReplyArtifact()
    notice.value = '单次语音批次已启动；启动只记录有限批次状态，不会创建授权或发送。'
  } catch (cause) {
    const message = errorMessage(cause)
    await loadVoiceBatches()
    error.value = message
    notice.value = '批次启动结果已通过刷新查询；没有自动重试。'
  } finally {
    startingVoiceBatch.value = false
  }
}

async function stopVoiceBatch() {
  const batch = currentVoiceBatch.value
  if (!batch || !['draft', 'running'].includes(batch.state) || stoppingVoiceBatch.value) return
  stoppingVoiceBatch.value = true
  error.value = ''
  notice.value = ''
  try {
    const stopped = await api.stopBatch(batch.id)
    voiceBatches.value = voiceBatches.value.map((item) => item.id === stopped.id ? stopped : item)
    clearReplyArtifact()
    notice.value = '语音批次已停止；绑定该批次的未外发语音素材会因批次状态变化而失效。'
  } catch (cause) {
    error.value = errorMessage(cause)
    await loadVoiceBatches()
  } finally {
    stoppingVoiceBatch.value = false
  }
}

async function loadVoiceConversationMessages() {
  const conversationId = selectedVoiceConversationId.value
  if (!conversationId) {
    voiceMessages.value = []
    clearReplyArtifact()
    return
  }
  loadingVoiceMessages.value = true
  error.value = ''
  voiceMessages.value = []
  clearReplyArtifact()
  try {
    const detail = await api.conversation(conversationId)
    if (selectedVoiceConversationId.value !== conversationId) return
    const conversation = detail.conversation
    if (conversation.person_id !== ownerPersonId.value || conversation.scope !== 'production' || conversation.purpose !== 'production' || conversation.channel !== 'qq') {
      throw new Error('仅显示本人的 production QQ 对话。')
    }
    voiceMessages.value = detail.messages
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loadingVoiceMessages.value = false
  }
}

async function createVoiceReply() {
  const conversation = selectedVoiceConversation.value
  const message = latestVoiceReply.value
  const batch = currentVoiceBatch.value
  if (!conversation || !message || !batch?.version || !canGenerateVoiceReply.value) return
  generatingVoiceReply.value = true
  error.value = ''
  notice.value = ''
  try {
    const latestDetail = await api.conversation(conversation.id)
    const orderedMessages = [...latestDetail.messages].sort((left, right) => Date.parse(left.created_at) - Date.parse(right.created_at))
    const currentLatestMessage = orderedMessages.at(-1)
    const currentBatch = (await api.batches()).items.find((item) => item.id === batch.id)
    if (latestDetail.conversation.person_id !== ownerPersonId.value
      || latestDetail.conversation.scope !== 'production'
      || latestDetail.conversation.purpose !== 'production'
      || latestDetail.conversation.channel !== 'qq'
      || latestDetail.conversation.revision !== conversation.revision
      || latestDetail.conversation.state !== 'active'
      || latestDetail.conversation.stop_requested
      || latestDetail.conversation.human_takeover
      || currentLatestMessage?.role !== 'assistant'
      || currentLatestMessage.id !== message.id
      || !currentBatch
      || currentBatch.state !== 'running'
      || currentBatch.version !== batch.version
      || !isEligibleVoiceBatch(currentBatch, latestDetail.conversation)) {
      voiceMessages.value = latestDetail.messages
      voiceConversations.value = voiceConversations.value.map((item) => item.id === latestDetail.conversation.id ? latestDetail.conversation : item)
      if (currentBatch) {
        voiceBatches.value = voiceBatches.value.map((item) => item.id === currentBatch.id ? currentBatch : item)
      } else {
        await loadVoiceBatches()
      }
      throw new Error('会话或最新助手消息已变化；请核对后再发起本次语音生成。')
    }
    const artifact = await api.generateVoiceReply({
      conversation_id: latestDetail.conversation.id,
      expected_conversation_revision: latestDetail.conversation.revision,
      source_message_id: currentLatestMessage.id,
      batch_id: currentBatch.id,
      expected_batch_version: currentBatch.version,
    })
    replyArtifactBinding.value = {
      conversation_id: latestDetail.conversation.id,
      expected_revision: latestDetail.conversation.revision,
      source_message_id: currentLatestMessage.id,
      batch_id: currentBatch.id,
      batch_version: currentBatch.version,
      account_id: latestDetail.conversation.account_id || '',
      recipient_id: latestDetail.conversation.recipient_id || '',
    }
    replyArtifact.value = artifact
    await loadReplyAudio()
    notice.value = '本次语音素材已生成并读回。此动作没有创建授权、Outbox 项或发送请求。'
  } catch (cause) {
    const message = errorMessage(cause)
    if (cause instanceof ApiRequestError && (!cause.httpStatus || cause.retryable || cause.httpStatus >= 500)) {
      await Promise.all([loadVoiceProfile(), loadVoiceConversationMessages()])
      replyGenerationUncertain.value = true
      error.value = message
      notice.value = '语音素材生成结果未知；为避免重复生成，当前操作已锁定。请刷新会话和 profile 状态后再决定下一步。'
    } else if (cause instanceof ApiRequestError && cause.httpStatus === 409) {
      await loadVoiceProfile()
      await loadVoiceConversationMessages()
      error.value = message
      notice.value = '会话或 profile 状态已变化；本次没有自动重试，请核对最新回复。'
    } else error.value = message
  } finally {
    generatingVoiceReply.value = false
  }
}

function authorizationMatchesReply(authorization: VoiceOutboxAuthorizationResult) {
  const artifact = replyArtifact.value
  const binding = replyArtifactBinding.value
  const conversation = selectedVoiceConversation.value
  const batch = voiceBatches.value.find((item) => item.id === binding?.batch_id)
  const profile = voiceProfile.value
  const currentApproval = profile?.approval
  return Boolean(
    artifact
    && binding
    && conversation
    && batch
    && profile?.profile
    && currentApproval
    && authorization.state === 'active'
    && authorization.authorization_id === authorization.id
    && Date.parse(authorization.expires_at) > authorizationClock.value
    && authorization.action_id === 'qq.voice.send'
    && authorization.conversation_id === binding.conversation_id
    && authorization.conversation_revision === binding.expected_revision
    && authorization.batch_id === binding.batch_id
    && authorization.batch_version === binding.batch_version
    && authorization.person_id === conversation.person_id
    && authorization.account_id === binding.account_id
    && authorization.recipient_id === binding.recipient_id
    && authorization.record_ref.artifact_id === artifact.artifact_id
    && authorization.record_ref.sha256 === artifact.sha256
    && authorization.record_ref.voice_version === artifact.voice_version
    && authorization.record_ref.approval_ref === currentApproval.approval_id
    && profile.deployment_profile === 'local_trial'
    && profile.production_enabled === false
    && profile.readiness === 'ready'
    && profile.runtime_enabled
    && profile.voice_outbox_enabled
    && conversation.id === binding.conversation_id
    && conversation.revision === binding.expected_revision
    && conversation.account_id === binding.account_id
    && conversation.recipient_id === binding.recipient_id
    && batch.version === binding.batch_version
    && profile.profile.profile_id === artifact.profile_id
    && profile.profile.profile_version === artifact.profile_version
    && currentApproval.profile_id === artifact.profile_id
    && currentApproval.profile_version === artifact.profile_version
  )
}

async function readFreshVoiceReplyContext() {
  const artifact = replyArtifact.value
  const binding = replyArtifactBinding.value
  if (!artifact || !binding) throw new Error('当前没有可授权的语音回复素材。')
  if (selectedVoiceConversationId.value !== binding.conversation_id || selectedVoiceBatchId.value !== binding.batch_id) {
    throw new Error('当前选择已离开语音素材绑定的会话或批次；请重新读取并核对素材。')
  }
  const [detail, batchResult, profile] = await Promise.all([
    api.conversation(binding.conversation_id),
    api.batches(),
    api.voiceProfile(),
  ])
  if (selectedVoiceConversationId.value !== binding.conversation_id
    || selectedVoiceBatchId.value !== binding.batch_id
    || replyArtifact.value?.artifact_id !== artifact.artifact_id) {
    throw new Error('读取期间所选会话、批次或语音素材已变化；没有继续下一步。')
  }
  const conversation = detail.conversation
  const orderedMessages = [...detail.messages].sort((left, right) => Date.parse(left.created_at) - Date.parse(right.created_at))
  const latestMessage = orderedMessages.at(-1)
  const batch = batchResult.items.find((item) => item.id === binding.batch_id)
  voiceMessages.value = detail.messages
  voiceConversations.value = voiceConversations.value.some((item) => item.id === conversation.id)
    ? voiceConversations.value.map((item) => item.id === conversation.id ? conversation : item)
    : [...voiceConversations.value, conversation]
  voiceProfile.value = profile
  voiceBatches.value = batchResult.items.filter((item) => isEligibleVoiceBatch(item, conversation))
  if (!conversation.account_id || !conversation.recipient_id || !batch || !profile.profile || !profile.approval
    || conversation.id !== binding.conversation_id
    || conversation.person_id !== ownerPersonId.value
    || conversation.scope !== 'production'
    || conversation.purpose !== 'production'
    || conversation.channel !== 'qq'
    || conversation.state !== 'active'
    || conversation.stop_requested
    || conversation.human_takeover
    || conversation.revision !== binding.expected_revision
    || conversation.account_id !== binding.account_id
    || conversation.recipient_id !== binding.recipient_id
    || latestMessage?.role !== 'assistant'
    || latestMessage.id !== binding.source_message_id
    || batch.state !== 'running'
    || batch.version !== binding.batch_version
    || !isEligibleVoiceBatch(batch, conversation)
    || profile.deployment_profile !== 'local_trial'
    || !profile.runtime_enabled
    || !profile.voice_outbox_enabled
    || profile.approval.profile_id !== profile.profile.profile_id
    || profile.approval.profile_version !== profile.profile.profile_version
    || artifact.purpose !== 'reply'
    || artifact.artifact_state !== 'ready'
    || artifact.profile_id !== profile.profile.profile_id
    || artifact.profile_version !== profile.profile.profile_version) {
    clearReplyArtifact()
    throw new Error('会话、最新回复、语音批次或批准 profile 已变化或未就绪；旧素材状态已清除，没有创建授权或 Outbox 草稿。请核对后重新生成。')
  }
  return { artifact, binding, conversation, latestMessage, batch, profile }
}

async function authorizeVoiceReply() {
  if (!canAuthorizeVoiceReply.value || authorizingVoiceReply.value) return
  authorizingVoiceReply.value = true
  error.value = ''
  notice.value = ''
  let mutationStarted = false
  try {
    const context = await readFreshVoiceReplyContext()
    mutationStarted = true
    const authorization = await api.authorizeVoiceOutbox({
      action_id: 'qq.voice.send',
      artifact_id: context.artifact.artifact_id,
      conversation_id: context.conversation.id,
      expected_revision: context.conversation.revision,
      batch_id: context.batch.id,
      expires_in_seconds: 600,
    })
    replyAuthorization.value = authorization
    if (!authorizationMatchesReply(authorization)) {
      voiceAuthorizationUncertain.value = true
      error.value = '服务端返回的授权绑定与当前素材不一致；已锁定后续操作，请先核对服务端状态。'
      return
    }
    notice.value = `一次性授权已创建，有效至 ${new Date(authorization.expires_at).toLocaleString('zh-CN')}。它尚未创建 Outbox 草稿，也没有发送语音。`
  } catch (cause) {
    error.value = errorMessage(cause)
    if (mutationStarted && isUnknownMutationOutcome(cause)) {
      voiceAuthorizationUncertain.value = true
      notice.value = '授权请求结果未知；当前页面已锁定重复授权和后续 prepare。请刷新并核对服务端状态，不要重试。'
    }
  } finally {
    authorizingVoiceReply.value = false
  }
}

async function prepareVoiceReplyOutbox() {
  if (!canPrepareVoiceReplyOutbox.value || preparingVoiceReplyOutbox.value || voiceAuthorizationUncertain.value) return
  preparingVoiceReplyOutbox.value = true
  error.value = ''
  notice.value = ''
  let mutationStarted = false
  try {
    const context = await readFreshVoiceReplyContext()
    const authorization = replyAuthorization.value
    if (!authorization || !authorizationMatchesReply(authorization)) {
      throw new Error('一次性授权已失效或不再匹配当前语音素材；请先核对最新状态。')
    }
    if (!replyPrepareIdempotencyKey.value) replyPrepareIdempotencyKey.value = crypto.randomUUID()
    mutationStarted = true
    const item = await api.prepareVoiceOutbox({
      action_id: 'qq.voice.send',
      artifact_id: context.artifact.artifact_id,
      conversation_id: context.conversation.id,
      expected_revision: context.conversation.revision,
      batch_id: context.batch.id,
      authorization_id: authorization.authorization_id,
      idempotency_key: replyPrepareIdempotencyKey.value,
    })
    replyOutboxItem.value = item
    if (item.action_id !== 'qq.voice.send'
      || item.state !== 'prepared'
      || item.conversation_id !== context.binding.conversation_id
      || item.expected_revision !== context.binding.expected_revision
      || item.batch_id !== context.binding.batch_id
      || item.account_id !== context.binding.account_id
      || item.recipient_id !== context.binding.recipient_id
      || item.authorization_id !== authorization.authorization_id
      || item.record_ref?.artifact_id !== context.artifact.artifact_id
      || item.record_ref.sha256 !== context.artifact.sha256
      || item.record_ref.voice_version !== context.artifact.voice_version
      || item.record_ref.approval_ref !== authorization.record_ref.approval_ref) {
      voicePreparationUncertain.value = true
      error.value = '服务端返回的 Outbox 项与本次精确授权绑定不一致；已锁定重复 prepare，请核对 Outbox。'
      return
    }
    notice.value = `语音 Outbox 草稿已准备（${item.id}）。它仍是 prepared 状态；发送需要到 Outbox 页重新预检并再次确认。`
  } catch (cause) {
    error.value = errorMessage(cause)
    if (mutationStarted && isUnknownMutationOutcome(cause)) {
      voicePreparationUncertain.value = true
      notice.value = 'Prepare 请求结果未知；当前页面已锁定重复操作。请到 Outbox 刷新核对状态，不要重试。'
    }
  } finally {
    preparingVoiceReplyOutbox.value = false
  }
}

async function loadReplyAudio() {
  if (!replyArtifact.value) return
  try {
    if (replyAudioUrl.value) URL.revokeObjectURL(replyAudioUrl.value)
    replyAudioUrl.value = URL.createObjectURL(await api.voiceArtifactContent(replyArtifact.value.artifact_id))
  } catch (cause) {
    error.value = errorMessage(cause)
  }
}

async function loadAsrCapabilities() {
  asrLoading.value = true
  error.value = ''
  notice.value = ''
  try {
    asr.value = await api.asrCapabilities()
    notice.value = '本地语音识别状态已读取；没有下载模型或打开媒体文件。'
  } catch (cause) {
    error.value = errorMessage(cause)
    asr.value = null
  } finally {
    asrLoading.value = false
  }
}

async function loadRoomMediaIndex() {
  loadingRooms.value = true
  error.value = ''
  notice.value = ''
  transcription.value = null
  try {
    const result = await api.rooms({ state: 'ready', limit: 200, offset: 0 })
    roomItems.value = result.items.filter((item) => Boolean(item.managed_media_id))
    if (!roomItems.value.some((item) => item.id === selectedRoomItemId.value)) {
      selectedRoomItemId.value = roomItems.value[0]?.id ?? ''
    }
    notice.value = `已从房间数据库索引读取 ${roomItems.value.length} 条带托管媒体的条目；没有扫描磁盘。`
  } catch (cause) {
    error.value = errorMessage(cause)
    roomItems.value = []
  } finally {
    loadingRooms.value = false
  }
}

async function transcribe() {
  const item = selectedRoomItem.value
  if (!item || !asr.value || asr.value.status !== 'ready' || transcribing.value) return
  transcribing.value = true
  error.value = ''
  notice.value = ''
  transcription.value = null
  chatResult.value = null
  chatUncertain.value = false
  lastChatRequestId.value = ''
  try {
    transcription.value = await api.transcribeRoomAudio({ room_item_id: item.id, expected_room_version: item.version })
    notice.value = transcription.value.status === 'success' || transcription.value.transcript
      ? '本机语音识别已返回文字；是否继续对话仍由你决定。'
      : `识别接口返回状态：${transcription.value.status}。后续对话和 QQ 投递尚未启动。`
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    transcribing.value = false
  }
}

async function loadProductionConversations() {
  loadingConversations.value = true
  error.value = ''
  notice.value = ''
  try {
    const [conversationResult, peopleResult] = await Promise.all([api.conversations('production'), api.people()])
    ownerPersonId.value = peopleResult.items.find((person) => person.role === 'owner')?.person_id ?? ''
    conversations.value = conversationResult.items.filter((conversation) => conversation.purpose === 'production' && conversation.channel === 'ui' && conversation.person_id === ownerPersonId.value)
    if (!conversations.value.some((conversation) => conversation.id === selectedConversationId.value)) {
      selectedConversationId.value = conversations.value[0]?.id ?? ''
    }
    notice.value = `已读取 ${conversations.value.length} 个 production UI 对话；转写内容不会自动发送。`
  } catch (cause) {
    error.value = errorMessage(cause)
    conversations.value = []
  } finally {
    loadingConversations.value = false
  }
}

async function submitTranscriptToChat() {
  const transcriptText = transcription.value?.transcript?.trim()
  const conversationId = selectedConversationId.value
  if (!transcriptText || !conversationId || submittingTranscript.value) return
  submittingTranscript.value = true
  error.value = ''
  notice.value = ''
  chatResult.value = null
  try {
    const detail = await api.conversation(conversationId)
    const conversation = detail.conversation
    if (conversation.scope !== 'production' || conversation.purpose !== 'production' || conversation.channel !== 'ui' || conversation.person_id !== ownerPersonId.value) throw new Error('只允许发送到本人 production UI 对话。')
    if (conversation.state !== 'active' || conversation.stop_requested || conversation.human_takeover) {
      throw new Error('对话已暂停、停止或人工接管；转写文本未发送。')
    }
    chatResult.value = await api.sendChatTurn(conversationId, {
      content: transcriptText,
      idempotency_key: crypto.randomUUID(),
    })
    lastChatRequestId.value = String(chatResult.value.request?.local_request_id || '')
    const returnedState = String(chatResult.value.request?.state || chatResult.value.request?.status || '').toLowerCase()
    chatUncertain.value = ['unknown', 'failed_unknown', 'completed_stale'].includes(returnedState)
    notice.value = chatUncertain.value
      ? '请求已返回但结果仍需核对；为避免重复计费或回复，当前转写不能再次提交。'
      : '已由你明确提交转写文本到现有 ChatService；没有触发语音回复或 QQ Outbox。'
  } catch (cause) {
    error.value = errorMessage(cause)
    if (isUnknownChatFailure(cause)) {
      lastChatRequestId.value = requestIdFromError(cause)
      chatUncertain.value = true
      notice.value = lastChatRequestId.value
        ? '请求结果未知；为避免重复计费或回复，当前转写不能再次提交。请查询最近请求。'
        : '请求结果未知；为避免重复计费或回复，当前转写不能再次提交。请刷新对话确认服务端状态。'
    } else if (cause instanceof ApiRequestError && (cause.httpStatus === 409 || cause.code === 'conversation_revision_conflict')) {
      await loadProductionConversations()
      notice.value = '对话版本已变化，请核对最新状态；本次没有自动重试。'
    }
  } finally {
    submittingTranscript.value = false
  }
}

async function checkChatRequest() {
  if (!lastChatRequestId.value) return
  error.value = ''
  try {
    chatResult.value = await api.chatRequest(lastChatRequestId.value)
    const state = String(chatResult.value.request?.state || chatResult.value.request?.status || 'unknown').toLowerCase()
    chatUncertain.value = state === 'unknown' || state === 'completed_stale'
    notice.value = chatUncertain.value
      ? '请求结果仍未知；转写保持锁定，避免重复提交。'
      : `最近请求状态：${state}。此转写已经提交，不会再次发送。`
  } catch (cause) {
    error.value = errorMessage(cause)
  }
}

watch(selectedVoiceConversationId, () => { void loadVoiceConversationMessages(); void loadVoiceBatches() })
watch(selectedVoiceBatchId, (batchId) => {
  if (replyArtifactBinding.value && batchId !== replyArtifactBinding.value.batch_id) clearReplyArtifact()
})
onMounted(() => { void loadVoiceProfile() })
onUnmounted(() => {
  if (replyAuthorizationExpiryTimer) window.clearTimeout(replyAuthorizationExpiryTimer)
  clearPreview()
  clearReplyArtifact()
})
</script>

<template>
  <div class="page-stack">
    <header class="page-heading">
      <div>
        <p class="eyebrow">本机语音能力</p>
        <h1>试听与语音输入</h1>
        <p class="page-heading__description">先核对本机语音 profile 和批准状态。试听、批准、素材、授权和草稿准备都由你分别发起；派发还需要到 Outbox 重新预检并再次确认。</p>
      </div>
      <div class="page-heading__actions"><NTag round type="info">默认文字</NTag></div>
    </header>

    <NAlert v-if="error" type="error" :show-icon="true" title="语音请求未完成">{{ error }}</NAlert>
    <NAlert v-if="notice" type="success" :show-icon="true">{{ notice }}</NAlert>
    <NAlert type="info" :show-icon="true" title="语音安全边界">
      本机 SAPI 当前只提供普通朗读，不能作为已完成 C07 表达控制的证据。批准只绑定当前 profile 版本；语音授权与 Outbox 草稿分两步确认。本页面不会自动批准、授权或发送；派发只能在 Outbox 通过服务端最新预检后再次确认。语音输入只读取已登记的托管 WAV，不会下载模型。
    </NAlert>

    <section class="content-grid">
      <NCard class="panel-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>本机朗读试听</h2><p>先手动读取能力，再生成短 WAV 预览。</p></div><AppIcon name="play" /></div></template>
        <div class="voice-panel">
          <NButton secondary :loading="voiceProfileLoading" @click="loadVoiceProfile">读取 profile、批准和运行状态</NButton>
          <div v-if="voiceProfile" class="voice-status voice-status--profile">
            <span>部署 profile</span><strong>{{ voiceProfile.deployment_profile }}</strong>
            <span>声音来源</span><strong>{{ voiceProfile.source_mode }}</strong>
            <span>运行时</span><StatusPill :status="voiceProfile.runtime_enabled ? 'enabled' : 'disabled'" kind="plain" />
            <span>语音外发开关</span><StatusPill :status="voiceProfile.voice_outbox_enabled ? 'enabled' : 'disabled'" kind="plain" />
            <span>正式启用</span><StatusPill :status="voiceProfile.production_enabled ? 'enabled' : 'disabled'" kind="plain" />
            <span>就绪门禁</span><StatusPill :status="voiceProfile.readiness" />
          </div>
          <div v-if="voiceProfile" class="voice-form">
            <NButton secondary :loading="voicesLoading" @click="loadTtsCapabilities">读取本机语音能力</NButton>
            <label class="field-label" for="tts-voice">选择本机已安装语音</label>
            <NSelect id="tts-voice" v-model:value="selectedVoiceId" :options="voiceOptions" :disabled="!tts?.voices.length || voicesLoading || previewing || savingVoiceProfile" placeholder="先读取本机语音" />
            <NPopconfirm v-if="canSaveVoiceProfile" @positive-click="saveVoiceProfile">
              <template #trigger><NButton secondary :loading="savingVoiceProfile" :disabled="!canSaveVoiceProfile">保存为新 profile 版本</NButton></template>
              保存会创建不可变 profile 并撤销旧批准。下一步仍须单独生成试听并由你明确批准，确认继续？
            </NPopconfirm>
            <small class="capability-detail">当前 profile：{{ voiceProfile.profile ? `${voiceProfile.profile.voice_name} · ${voiceProfile.profile.voice_id} · v${voiceProfile.profile.profile_version} · ${voiceProfile.profile.capability}` : '尚未设置' }}。settings hash：{{ voiceProfile.profile?.settings_hash || '未提供' }}</small>
          </div>
          <div v-if="voiceProfile" class="voice-form">
            <div class="voice-status"><span>批准版本</span><strong>{{ voiceProfile.approval ? `v${voiceProfile.approval.profile_version} · ${voiceProfile.approval.approved_at}` : '无有效批准' }}</strong><span>支持表达控制</span><strong>{{ voiceProfile.capabilities.supports_instruction_expression ? '有' : '无' }} · 重音 {{ voiceProfile.capabilities.supports_semantic_emphasis ? '有' : '无' }} · 停顿 {{ voiceProfile.capabilities.supports_pause_control ? '有' : '无' }}</strong></div>
            <div class="voice-actions">
              <NPopconfirm v-if="!voiceProfile.runtime_enabled" :disabled="!voiceProfile.profile || voiceProfile.approval?.profile_version !== voiceProfile.profile.profile_version || voiceProfile.deployment_profile !== 'local_trial' || savingVoiceSettings" @positive-click="saveVoiceSettings({ runtime_enabled: true, voice_outbox_enabled: false })">
                <template #trigger><NButton size="small" secondary :loading="savingVoiceSettings" :disabled="!voiceProfile.profile || voiceProfile.approval?.profile_version !== voiceProfile.profile.profile_version || voiceProfile.deployment_profile !== 'local_trial'">启用本机语音运行时</NButton></template>
                只允许 local_trial 的手动语音流程。启用运行时不会自动合成、授权或发送。确认启用？
              </NPopconfirm>
              <NPopconfirm v-else :disabled="savingVoiceSettings" @positive-click="saveVoiceSettings({ runtime_enabled: false, voice_outbox_enabled: false })">
                <template #trigger><NButton size="small" quaternary :loading="savingVoiceSettings">关闭本机语音运行时</NButton></template>
                关闭会同时撤销本机语音外发开关，不会删除 profile、批准或素材。确认关闭？
              </NPopconfirm>
              <NPopconfirm v-if="!voiceProfile.voice_outbox_enabled" :disabled="!voiceProfile.runtime_enabled || voiceProfile.approval?.profile_version !== voiceProfile.profile?.profile_version || voiceProfile.deployment_profile !== 'local_trial' || savingVoiceSettings" @positive-click="saveVoiceSettings({ runtime_enabled: true, voice_outbox_enabled: true })">
                <template #trigger><NButton size="small" type="warning" secondary :loading="savingVoiceSettings" :disabled="!voiceProfile.runtime_enabled || voiceProfile.approval?.profile_version !== voiceProfile.profile?.profile_version || voiceProfile.deployment_profile !== 'local_trial'">允许后续单次语音外发</NButton></template>
                这只打开本机语音 Outbox 功能开关。每条语音仍需有限批次、精确授权和独立发送确认；确认继续？
              </NPopconfirm>
              <NPopconfirm v-else :disabled="savingVoiceSettings" @positive-click="saveVoiceSettings({ runtime_enabled: true, voice_outbox_enabled: false })">
                <template #trigger><NButton size="small" quaternary :loading="savingVoiceSettings">关闭语音外发开关</NButton></template>
                这会阻止后续语音 Outbox 准备，不会撤销已创建的服务端授权。确认关闭？
              </NPopconfirm>
            </div>
            <NAlert v-if="voiceProfile.deployment_profile !== 'local_trial'" type="warning" :show-icon="true">当前是迁移暂存 profile；语音运行、授权和发送保持禁用。</NAlert>
          </div>
          <div class="voice-status">
            <span>状态</span><StatusPill :status="tts?.status || 'unknown'" />
            <span>引擎</span><strong>{{ tts?.engine_id || '尚未读取' }}</strong>
          </div>
          <p v-if="tts?.detail" class="capability-detail">{{ tts.detail }}</p>
          <NEmpty v-if="tts && !tts.voices.length" description="当前接口未返回可用语音" />
          <div class="voice-form">
            <label class="field-label" for="tts-text">审批试听文字（最多 240 字）</label>
            <NInput id="tts-text" v-model:value="sampleText" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }" maxlength="240" :disabled="previewing" />
            <NButton type="primary" :loading="previewing" :disabled="!voiceProfile?.profile || !sampleText.trim() || sampleText.length > 240 || approvalPreviewUncertain" @click="createApprovalPreview">生成 profile 试听素材</NButton>
          </div>
          <div v-if="approvalPreview" class="audio-preview">
            <strong>未批准的 profile 试听素材</strong>
            <small>{{ approvalPreview.artifact_id }} · profile v{{ approvalPreview.profile_version }} · {{ approvalPreview.voice_version }} · SHA-256 {{ approvalPreview.sha256 }} · {{ approvalPreview.duration_ms }}ms</small>
            <audio v-if="audioUrl" :src="audioUrl" controls preload="none">当前浏览器不支持 WAV 播放。</audio>
            <NButton v-else size="small" secondary @click="loadApprovalAudio">读取已生成素材供手动试听</NButton>
            <NPopconfirm v-if="canApprovePreview" @positive-click="approvePreview">
              <template #trigger><NButton type="warning" secondary :loading="approvingVoice" :disabled="!canApprovePreview">我已试听，批准此 profile</NButton></template>
              批准将绑定此 artifact 的精确 profile 版本。它不会启用自动语音、不会创建 Outbox 授权，也不会发送。请确认已听过并要批准这个声音。
            </NPopconfirm>
            <StatusPill v-else-if="voiceProfile?.approval?.profile_version === approvalPreview.profile_version" status="approved" />
          </div>
          <div v-if="tts" class="capability-flags">
            <div><span>自定义音色</span><strong>{{ tts.supports_custom_voice ? '支持' : '不支持' }}</strong></div>
            <div><span>指令情绪表达</span><strong>{{ tts.supports_instruction_expression ? '支持' : '不支持' }}</strong></div>
            <div><span>语义重音</span><strong>{{ tts.supports_semantic_emphasis ? '支持' : '不支持' }}</strong></div>
            <div><span>暂停控制</span><strong>{{ tts.supports_pause_control ? '支持' : '不支持' }}</strong></div>
            <div><span>自动语音回复</span><strong>{{ tts.automatic_voice_reply ? '开启' : '关闭' }}</strong></div>
          </div>
        </div>
      </NCard>

      <NCard class="panel-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>房间语音输入</h2><p>从已登记的托管媒体索引选择 WAV，不浏览目录。</p></div><AppIcon name="book" /></div></template>
        <div class="voice-panel">
          <div class="voice-status"><span>ASR 状态</span><StatusPill :status="asr?.status || 'unknown'" /><span>执行配置</span><strong>{{ asr ? `${asr.compute_type} · ${asr.max_duration_seconds}s 上限` : '尚未读取' }}</strong></div>
          <p v-if="asr?.detail" class="capability-detail">{{ asr.detail }}</p>
          <NButton secondary :loading="asrLoading" @click="loadAsrCapabilities">检查本地 ASR 资源</NButton>
          <NButton secondary :loading="loadingRooms" @click="loadRoomMediaIndex">读取房间媒体索引</NButton>
          <NEmpty v-if="!roomItems.length" description="尚未载入可用的房间媒体索引" />
          <template v-else>
            <label class="field-label" for="asr-room-item">房间媒体条目</label>
            <NSelect id="asr-room-item" v-model:value="selectedRoomItemId" :options="mediaItemOptions" :disabled="loadingRooms || transcribing" />
            <small class="capability-detail">允许类型：{{ asr?.accepted_formats.join('、') || '读取 ASR 状态后显示' }}；文件大小上限：{{ asr ? `${(asr.max_bytes / 1024 / 1024).toFixed(0)} MiB` : '未知' }}。服务端会再次校验媒体类型、时长、来源和房间授权。</small>
            <NButton type="primary" :loading="transcribing" :disabled="!selectedRoomItem || !asr || asr.status !== 'ready'" @click="transcribe">在本机转写</NButton>
          </template>
          <NAlert v-if="asr?.status === 'needs_resource'" type="warning" :show-icon="true">本机缺少已安装 ASR 资源；不会自动下载或转用付费云服务。</NAlert>
          <NAlert v-if="transcription" :type="transcription.transcript ? 'success' : 'info'" :show-icon="true">
            <div>状态：{{ transcription.status }} · 聊天：{{ transcription.downstream.chat_service }} · 语音回复：{{ transcription.downstream.voice_reply }} · QQ Outbox：{{ transcription.downstream.qq_outbox }}</div>
            <p v-if="transcription.transcript" class="transcript-text">{{ transcription.transcript }}</p>
          </NAlert>
          <div v-if="transcription?.transcript" class="transcript-submit">
            <NAlert type="warning" :show-icon="true">把转写文字提交到 ChatService 可能产生服务费用。这是单独的一次用户确认；不会自动播放、合成语音或发送 QQ。</NAlert>
            <NButton size="small" secondary :loading="loadingConversations" @click="loadProductionConversations">读取 production UI 对话</NButton>
            <label class="field-label" for="asr-chat-conversation">发送目标</label>
            <NSelect id="asr-chat-conversation" v-model:value="selectedConversationId" :options="conversationOptions" :disabled="loadingConversations || submittingTranscript" placeholder="选择普通 production UI 对话" />
            <div v-if="selectedConversation" class="conversation-gate"><span>状态</span><StatusPill :status="selectedConversation.state" /><span>停止请求</span><strong>{{ selectedConversation.stop_requested ? '是' : '否' }}</strong><span>人工接管</span><strong>{{ selectedConversation.human_takeover ? '是' : '否' }}</strong></div>
            <NPopconfirm :disabled="!canSubmitTranscript" @positive-click="submitTranscriptToChat">
              <template #trigger><NButton type="primary" :disabled="!canSubmitTranscript" :loading="submittingTranscript">明确提交转写文本</NButton></template>
              只把已显示的转写文本提交到这个 production UI 对话。ChatService 可能计费；不会自动重试、转为语音或发送 QQ。确认继续？
            </NPopconfirm>
            <NButton v-if="lastChatRequestId" size="small" quaternary @click="checkChatRequest">查询最近 ChatService 请求</NButton>
            <NAlert v-if="chatUncertain" type="warning" :show-icon="true">请求结果未明；请先查询或刷新服务端状态，不要重复提交。</NAlert>
            <div v-if="chatResult" class="chat-result"><strong>ChatService 请求：{{ chatResult.request?.state || chatResult.request?.status || '状态未提供' }}</strong><small>request id：{{ chatResult.request?.local_request_id || chatResult.request?.request_id || '未提供' }}</small><p v-if="chatResult.message">{{ chatResult.message.content }}</p></div>
          </div>
          <div v-if="asr" class="capability-flags">
            <div><span>本地模型候选</span><strong>{{ asr.model_candidates.join('、') || '无' }}</strong></div>
            <div><span>自动下载</span><strong>关闭</strong></div>
            <div><span>QQ 发送</span><strong>需要独立授权</strong></div>
          </div>
        </div>
      </NCard>
    </section>

    <NCard class="panel-card" :bordered="false">
      <template #header><div class="card-heading"><div><h2>本人 QQ 回复语音</h2><p>从最新 assistant 回复生成素材，再逐步创建精确授权和 Outbox 草稿。</p></div><AppIcon name="play" /></div></template>
      <div class="voice-panel">
        <NAlert type="warning" :show-icon="true">
          需要已批准的当前 profile、local_trial、本机语音运行时和语音 Outbox 开关。素材、一次性授权和 prepared Outbox 草稿分别确认；此页不派发。正式启用状态独立保持关闭。
        </NAlert>
        <NButton secondary :loading="loadingVoiceConversations" @click="loadVoiceConversations">读取本人 production QQ 对话</NButton>
        <NEmpty v-if="!voiceConversations.length" description="尚未载入本人的 production QQ 对话" />
        <template v-else>
          <label class="field-label" for="voice-qq-conversation">QQ 对话</label>
          <NSelect id="voice-qq-conversation" v-model:value="selectedVoiceConversationId" :options="voiceConversationOptions" :disabled="loadingVoiceConversations || loadingVoiceMessages || generatingVoiceReply" />
          <div v-if="selectedVoiceConversation" class="conversation-gate">
            <span>状态</span><StatusPill :status="selectedVoiceConversation.state" />
            <span>revision</span><strong>{{ selectedVoiceConversation.revision }}</strong>
            <span>QQ 账号 / 收件人</span><strong>{{ selectedVoiceConversation.account_id || '未绑定' }} / {{ selectedVoiceConversation.recipient_id || '未绑定' }}</strong>
            <span>停止请求</span><strong>{{ selectedVoiceConversation.stop_requested ? '是' : '否' }}</strong>
            <span>人工接管</span><strong>{{ selectedVoiceConversation.human_takeover ? '是' : '否' }}</strong>
          </div>
          <NButton size="small" secondary :loading="loadingVoiceMessages" :disabled="!selectedVoiceConversationId || generatingVoiceReply" @click="loadVoiceConversationMessages">刷新会话最新消息</NButton>
          <NButton size="small" secondary :loading="loadingVoiceBatches" :disabled="!selectedVoiceConversationId" @click="loadVoiceBatches">读取此收件人的语音批次</NButton>
          <div v-if="voiceBatches.length" class="voice-form">
            <label class="field-label" for="voice-batch">单收件人、单动作语音批次</label>
            <NSelect id="voice-batch" v-model:value="selectedVoiceBatchId" :options="voiceBatchOptions" :disabled="loadingVoiceBatches || generatingVoiceReply" />
            <div v-if="currentVoiceBatch" class="conversation-gate">
              <span>状态 / 版本</span><strong>{{ currentVoiceBatch.state }} / v{{ currentVoiceBatch.version ?? '未知' }}</strong>
              <span>动作范围</span><strong>{{ currentVoiceBatch.target?.module_id }} · {{ currentVoiceBatch.target?.capability_id }} · {{ currentVoiceBatch.target?.object_ids?.[0] }}</strong>
              <span>有效期至</span><strong>{{ currentVoiceBatch.expires_at || '未提供' }}</strong>
              <span>上限</span><strong>{{ currentVoiceBatch.stop_condition?.kind }} · {{ currentVoiceBatch.stop_condition?.max_actions ?? '未知' }}</strong>
            </div>
            <NPopconfirm v-if="currentVoiceBatch?.state === 'draft'" :disabled="!canStartVoiceBatch" @positive-click="startVoiceBatch">
              <template #trigger><NButton size="small" type="warning" secondary :loading="startingVoiceBatch" :disabled="!canStartVoiceBatch">启动这条语音批次</NButton></template>
              启动只更新这一条有限批次的本机状态，不会授权或发送。确认启动？
            </NPopconfirm>
            <NPopconfirm v-if="currentVoiceBatch && ['draft', 'running'].includes(currentVoiceBatch.state)" :disabled="stoppingVoiceBatch" @positive-click="stopVoiceBatch">
              <template #trigger><NButton size="small" quaternary :loading="stoppingVoiceBatch">停止这条语音批次</NButton></template>
              停止后此批次下的语音 artifact 将不能继续外发。确认停止？
            </NPopconfirm>
          </div>
          <NEmpty v-else-if="!loadingVoiceBatches && selectedVoiceConversation" description="此收件人没有未过期的单动作语音批次" />
          <NPopconfirm :disabled="!canCreateVoiceBatch" @positive-click="createVoiceBatch">
            <template #trigger><NButton size="small" secondary :loading="creatingVoiceBatch" :disabled="!canCreateVoiceBatch">创建一小时、单收件人、单动作批次</NButton></template>
            将为账号 {{ selectedVoiceConversation?.account_id || '未知' }} / owner 收件人 {{ selectedVoiceConversation?.recipient_id || '未知' }} 创建一条 `qq.voice.send` 批次，最多一个动作。创建后还须你单独启动；不发送任何内容。确认创建？
          </NPopconfirm>
          <NEmpty v-if="!latestVoiceReply && !loadingVoiceMessages" description="最新消息不是 assistant 回复，或会话还没有可用回复" />
          <div v-if="latestVoiceReply" class="reply-source">
            <div><strong>拟生成的最新回复</strong><small>{{ latestVoiceReply.id }} · {{ latestVoiceReply.created_at }}</small></div>
            <p>{{ latestVoiceReply.content }}</p>
          </div>
          <NPopconfirm :disabled="!canGenerateVoiceReply" @positive-click="createVoiceReply">
            <template #trigger><NButton type="primary" :loading="generatingVoiceReply" :disabled="!canGenerateVoiceReply">为这条回复生成语音素材</NButton></template>
            只读取本人的当前 production QQ assistant 回复，并按当前已批准的 profile 生成一份本地音频 artifact。不会授权、准备或派发 Outbox。确认生成？
          </NPopconfirm>
          <NAlert v-if="voiceProfile && voiceProfile.readiness !== 'ready'" type="info" :show-icon="true">当前 voice readiness：{{ voiceProfile.readiness }}。先按上方步骤补足 profile、批准和运行开关。</NAlert>
          <NAlert v-if="replyGenerationUncertain" type="warning" :show-icon="true">语音 artifact 生成状态未知；本页面已锁定重复生成，避免产生重复素材。刷新后重新核对状态。</NAlert>
          <div v-if="replyArtifact" class="audio-preview">
            <strong>本次语音回复素材 · 未发送</strong>
            <small>{{ replyArtifact.artifact_id }} · {{ replyArtifact.purpose }} · profile v{{ replyArtifact.profile_version }} · {{ replyArtifact.voice_version }} · SHA-256 {{ replyArtifact.sha256 }} · {{ replyArtifact.duration_ms }}ms</small>
            <audio v-if="replyAudioUrl" :src="replyAudioUrl" controls preload="none">当前浏览器不支持 WAV 播放。</audio>
            <NButton v-else size="small" secondary @click="loadReplyAudio">读取已生成素材供手动试听</NButton>
            <div v-if="!replyOutboxItem" class="voice-actions">
              <NPopconfirm :disabled="!canAuthorizeVoiceReply" @positive-click="authorizeVoiceReply">
                <template #trigger><NButton size="small" type="warning" secondary :loading="authorizingVoiceReply" :disabled="!canAuthorizeVoiceReply">为此素材创建一次性语音授权</NButton></template>
                将重新读取当前会话、最新回复、语音批次和批准 profile，并创建一条 10 分钟有效、绑定此素材的授权。此步骤不会创建草稿或发送语音。确认创建？
              </NPopconfirm>
              <NPopconfirm v-if="replyAuthorization && canPrepareVoiceReplyOutbox" :disabled="!canPrepareVoiceReplyOutbox" @positive-click="prepareVoiceReplyOutbox">
                <template #trigger><NButton size="small" type="primary" :loading="preparingVoiceReplyOutbox" :disabled="!canPrepareVoiceReplyOutbox">准备语音 Outbox 草稿</NButton></template>
                将重新核对素材来源并用这条一次性授权创建 prepared 草稿。此步骤不会派发或发送。确认准备？
              </NPopconfirm>
              <span v-if="replyAuthorization && !canPrepareVoiceReplyOutbox" class="muted-copy">授权状态：{{ replyAuthorization.state }} · 有效至 {{ time(replyAuthorization.expires_at) }}。过期后可重新创建授权。</span>
            </div>
            <NAlert v-if="voiceAuthorizationUncertain" type="warning" :show-icon="true">授权结果未知；重复授权与后续 prepare 已锁定。请核对服务端状态，不要重试。</NAlert>
            <NAlert v-if="voicePreparationUncertain" type="warning" :show-icon="true">Outbox prepare 结果未知；重复 prepare 已锁定。请到消息草稿页刷新并核对，不要重试。</NAlert>
            <div v-if="replyOutboxItem" class="voice-outbox-item">
              <strong>Outbox 草稿 · <StatusPill :status="replyOutboxItem.state" /></strong>
              <small>{{ replyOutboxItem.id }} · {{ replyOutboxItem.action_id }} · 会话 revision {{ replyOutboxItem.expected_revision }}</small>
              <span class="muted-copy">草稿仍需在消息草稿页通过服务端预检并再次确认派发。</span>
            </div>
          </div>
        </template>
      </div>
    </NCard>
  </div>
</template>

<style scoped>
.voice-panel { display: grid; gap: 12px; }
.voice-status { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 8px 12px; color: var(--text-muted); font-size: 11px; }
.voice-status strong { overflow-wrap: anywhere; color: var(--text); }
.capability-detail { margin: 0; overflow-wrap: anywhere; color: var(--text-muted); font-size: 11px; line-height: 1.55; }
.voice-form { display: grid; gap: 8px; }
.audio-preview { display: grid; gap: 7px; padding: 12px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-soft); }
.audio-preview strong { color: var(--text); font-size: 12px; }
.audio-preview small { overflow-wrap: anywhere; color: var(--text-muted); font-size: 10px; }
.audio-preview audio { width: 100%; }
.voice-status--profile { grid-template-columns: auto minmax(0, 1fr); padding: 10px; border-radius: 10px; background: var(--surface-soft); }
.voice-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.capability-flags { display: grid; gap: 5px; padding: 10px; border-radius: 10px; background: var(--surface-soft); }
.capability-flags > div { display: flex; justify-content: space-between; gap: 12px; color: var(--text-muted); font-size: 10px; }
.capability-flags strong { color: var(--text-soft); text-align: right; }
.transcript-text { margin: 8px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.transcript-submit { display: grid; gap: 9px; padding: 12px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-soft); }
.conversation-gate { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 6px 10px; color: var(--text-muted); font-size: 10px; }
.conversation-gate strong, .chat-result strong { color: var(--text-soft); }
.chat-result { display: grid; gap: 4px; padding: 10px; border-radius: 10px; background: var(--surface); }
.chat-result small { color: var(--text-muted); font-size: 10px; overflow-wrap: anywhere; }
.chat-result p { margin: 5px 0 0; color: var(--text-soft); font-size: 11px; white-space: pre-wrap; overflow-wrap: anywhere; }
.reply-source { display: grid; gap: 8px; padding: 12px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-soft); }
.reply-source > div { display: grid; gap: 3px; }
.reply-source strong { color: var(--text); font-size: 12px; }
.reply-source small { color: var(--text-muted); font-size: 10px; overflow-wrap: anywhere; }
.reply-source p { max-height: 240px; overflow: auto; margin: 0; color: var(--text-soft); font-size: 12px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
.voice-outbox-item { display: grid; gap: 6px; padding: 11px; border: 1px solid var(--border); border-radius: 11px; background: var(--surface); }
.voice-outbox-item strong { display: flex; align-items: center; gap: 8px; color: var(--text); font-size: 12px; }
.voice-outbox-item small { overflow-wrap: anywhere; color: var(--text-muted); font-size: 10px; }
</style>
