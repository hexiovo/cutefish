import type {
  ApiEnvelope,
  AsrCapabilities,
  AsrTranscriptionRequest,
  AsrTranscriptionResult,
  BudgetEntry,
  BudgetEntryState,
  BootstrapData,
  BudgetSnapshot,
  CandidateItem,
  CandidateState,
  DeepSeekCredentialStatus,
  ChatConversation,
  ChatDraft,
  ChatMessage,
  ChatTurnResult,
  ConversationPurpose,
  ConversationScope,
  ConfirmedDesignImportInfo,
  ConfirmedDesignImportResult,
  FeatureStatusData,
  IntegrationActionPreview,
  IntegrationActionPreviewRequest,
  IntegrationCapability,
  IntegrationSourceReadRequest,
  IntegrationSourceReadResult,
  HostSettings,
  MemoryItem,
  ModuleConfigData,
  ModuleItem,
  MigrationExportResult,
  MigrationImportResult,
  MigrationPreflight,
  MigrationStatus,
  MigrationValidationResult,
  ManagedMedia,
  OverviewData,
  PersonaApproval,
  PersonaFeedback,
  PersonaInterview,
  PersonaVersion,
  PersonItem,
  ProactivityBatchCreate,
  ProactivityBatchResult,
  ProactivityPolicy,
  ProactivityPolicyUpdate,
  QqAccountItem,
  QqIdentityItem,
  QqServiceStatus,
  OutboxItem,
  OutboxListData,
  OutboxPreflightResult,
  SessionData,
  RoomItem,
  RoomItemCreate,
  RoomExportResult,
  RoomContentRead,
  RoomStatus,
  TaskItem,
  TtsCapabilities,
  TtsPreviewResult,
  TrialBatch,
  TrialBatchCreate,
  VisionCapability,
  VisionObservationResult,
  UiSettings,
  VoiceApprovalResult,
  VoiceArtifact,
  VoiceOutboxAuthorizationRequest,
  VoiceOutboxAuthorizationResult,
  VoiceOutboxPrepareRequest,
  VoiceProfileData,
  VoiceReplyRequest,
} from './types'

let csrfToken: string | null = null

export class ApiRequestError extends Error {
  readonly code: string
  readonly retryable: boolean
  readonly requestId: string | null
  readonly httpStatus: number | null
  readonly details: unknown

  constructor(options: {
    message: string
    code?: string
    retryable?: boolean
    requestId?: string | null
    httpStatus?: number | null
    details?: unknown
  }) {
    super(options.message)
    this.name = 'ApiRequestError'
    this.code = options.code ?? 'unknown'
    this.retryable = options.retryable ?? false
    this.requestId = options.requestId ?? null
    this.httpStatus = options.httpStatus ?? null
    this.details = options.details
  }
}

export function setCsrfToken(token: string | null | undefined) {
  csrfToken = token || null
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

async function postAudioPreview(path: string, payload: { text: string; voice_id: string; task_id?: string; expected_task_revision?: number }): Promise<TtsPreviewResult> {
  if (!csrfToken) {
    throw new ApiRequestError({ message: '尚未取得本机会话保护令牌，请刷新后重试。', code: 'csrf_missing' })
  }

  let response: Response
  try {
    response = await fetch(path, {
      method: 'POST',
      headers: {
        Accept: 'audio/wav, application/json',
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrfToken,
      },
      credentials: 'same-origin',
      body: JSON.stringify(payload),
    })
  } catch {
    throw new ApiRequestError({ message: '无法连接本机服务。请确认本机控制台已启动，再重试。', code: 'not_connected', retryable: true })
  }

  if (!response.ok) {
    let body: unknown
    try {
      body = await response.json()
    } catch {
      body = null
    }
    const envelope = isObject(body) ? (body as unknown as ApiEnvelope<unknown>) : null
    const errorBody = envelope?.error ?? undefined
    throw new ApiRequestError({
      message: errorBody?.message || `本机服务暂未完成此操作（HTTP ${response.status}）。`,
      code: errorBody?.code || envelope?.status || 'preview_failed',
      retryable: errorBody?.retryable,
      requestId: errorBody?.request_id || envelope?.request_id || null,
      httpStatus: response.status,
      details: errorBody?.details,
    })
  }

  const mediaType = response.headers.get('Content-Type')?.split(';', 1)[0].trim().toLowerCase()
  if (mediaType !== 'audio/wav' || response.headers.get('X-Voice-Approved') !== 'false') {
    throw new ApiRequestError({
      message: '试听接口没有返回未批准的 WAV 预览；已丢弃响应。',
      code: 'invalid_voice_preview',
      httpStatus: response.status,
    })
  }

  return {
    audio: await response.blob(),
    engine: response.headers.get('X-Voice-Engine'),
    voiceId: response.headers.get('X-Voice-Id'),
    approved: false,
    capability: response.headers.get('X-Voice-Capability'),
  }
}

async function getVoiceArtifactContent(artifactId: string): Promise<Blob> {
  if (!artifactId.trim()) {
    throw new ApiRequestError({ message: '语音素材引用无效，未读取音频。', code: 'invalid_voice_artifact' })
  }

  let response: Response
  try {
    response = await fetch(`/api/extensions/voice/artifacts/${encodeURIComponent(artifactId)}/content`, {
      method: 'GET',
      headers: { Accept: 'audio/wav, application/json' },
      credentials: 'same-origin',
    })
  } catch {
    throw new ApiRequestError({ message: '无法连接本机服务。请确认本机控制台已启动，再重试。', code: 'not_connected', retryable: true })
  }

  if (!response.ok) {
    let body: unknown
    try {
      body = await response.json()
    } catch {
      body = null
    }
    const envelope = isObject(body) ? (body as unknown as ApiEnvelope<unknown>) : null
    const errorBody = envelope?.error ?? undefined
    throw new ApiRequestError({
      message: errorBody?.message || `本机服务暂未完成此操作（HTTP ${response.status}）。`,
      code: errorBody?.code || envelope?.status || 'voice_artifact_unavailable',
      retryable: errorBody?.retryable,
      requestId: errorBody?.request_id || envelope?.request_id || null,
      httpStatus: response.status,
      details: errorBody?.details,
    })
  }

  const mediaType = response.headers.get('Content-Type')?.split(';', 1)[0].trim().toLowerCase()
  if (mediaType !== 'audio/wav') {
    throw new ApiRequestError({
      message: '语音素材读取接口没有返回 WAV 音频；响应已丢弃。',
      code: 'invalid_voice_artifact',
      httpStatus: response.status,
    })
  }

  return response.blob()
}

async function request<T>(path: string, init: RequestInit = {}): Promise<ApiEnvelope<T>> {
  const method = (init.method ?? 'GET').toUpperCase()
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    if (!csrfToken) {
      throw new ApiRequestError({ message: '尚未取得本机会话保护令牌，请刷新后重试。', code: 'csrf_missing' })
    }
    headers.set('X-CSRF-Token', csrfToken)
  }

  let response: Response
  try {
    response = await fetch(path, { ...init, method, headers, credentials: 'same-origin' })
  } catch {
    throw new ApiRequestError({
      message: '无法连接本机服务。请确认本机控制台已启动，再重试。',
      code: 'not_connected',
      retryable: true,
    })
  }

  let body: unknown
  try {
    body = await response.json()
  } catch {
    throw new ApiRequestError({
      message: `本机服务返回了无法识别的响应（HTTP ${response.status}）。`,
      code: 'invalid_response',
      retryable: response.status >= 500,
      httpStatus: response.status,
    })
  }

  const envelope = isObject(body) ? (body as unknown as ApiEnvelope<T>) : null
  const errorBody = envelope?.error ?? undefined
  if (!response.ok || !envelope || envelope.status !== 'success') {
    throw new ApiRequestError({
      message: errorBody?.message || `本机服务暂未完成此操作（HTTP ${response.status}）。`,
      code: errorBody?.code || envelope?.status || 'invalid_response',
      retryable: errorBody?.retryable,
      requestId: errorBody?.request_id || envelope?.request_id || null,
      httpStatus: response.status,
      details: errorBody?.details,
    })
  }
  return envelope
}

function data<T>(envelope: ApiEnvelope<T>): T {
  if (envelope.data === null || envelope.data === undefined) {
    throw new ApiRequestError({
      message: '本机服务没有提供页面需要的数据。',
      code: 'missing_data',
      requestId: envelope.request_id,
    })
  }
  return envelope.data
}

const get = <T>(path: string) => request<T>(path).then(data)
const post = <T>(path: string, payload?: unknown) =>
  request<T>(path, { method: 'POST', body: payload === undefined ? undefined : JSON.stringify(payload) }).then(data)
const patch = <T>(path: string, payload: unknown) =>
  request<T>(path, { method: 'PATCH', body: JSON.stringify(payload) }).then(data)
const put = <T>(path: string, payload: unknown) =>
  request<T>(path, { method: 'PUT', body: JSON.stringify(payload) }).then(data)

export const api = {
  bootstrap: () => get<BootstrapData>('/api/auth/bootstrap'),
  session: () => get<SessionData>('/api/auth/session'),
  setup: (password: string) => post<SessionData>('/api/auth/setup', { password }),
  login: (password: string) => post<SessionData>('/api/auth/login', { password }),
  logout: () => post<{ authenticated: boolean }>('/api/auth/logout'),
  overview: () => get<OverviewData>('/api/overview'),
  modules: () => get<{ items: ModuleItem[] }>('/api/modules'),
  features: () => get<FeatureStatusData>('/api/features'),
  budgets: () => get<BudgetSnapshot>('/api/budgets'),
  budgetEntries: (options: { state?: BudgetEntryState; limit?: number } = {}) => {
    const query = new URLSearchParams()
    if (options.state) query.set('state', options.state)
    if (options.limit) query.set('limit', String(options.limit))
    const suffix = query.size ? `?${query.toString()}` : ''
    return get<{ items: BudgetEntry[]; total: number }>(`/api/budgets/entries${suffix}`)
  },
  people: () => get<{ items: PersonItem[] }>('/api/people'),
  createPerson: (payload: { display_name: string }) => post<PersonItem>('/api/people', payload),
  qqStatus: () => get<QqServiceStatus>('/api/qq/status'),
  qqAccounts: () => get<{ items: QqAccountItem[] }>('/api/qq/accounts'),
  createQqAccount: (payload: { account_id: string; display_name: string; service_url?: string }) =>
    post<QqAccountItem>('/api/qq/accounts', payload),
  configureQqCredentials: (accountId: string, payload: { napcat_access_token: string; webhook_secret: string }) =>
    put<{ account_id: string; credentials_configured: boolean; enabled: boolean }>(`/api/qq/accounts/${encodeURIComponent(accountId)}/credentials`, payload),
  setQqAccountEnabled: (accountId: string, enabled: boolean) =>
    put<QqAccountItem>(`/api/qq/accounts/${encodeURIComponent(accountId)}/enabled`, { enabled }),
  qqIdentities: (accountId: string) =>
    get<{ items: QqIdentityItem[]; account_id: string }>(`/api/qq/accounts/${encodeURIComponent(accountId)}/identities`),
  bindQqOwner: (accountId: string, payload: { sender_id: string }) =>
    put<QqAccountItem>(`/api/qq/accounts/${encodeURIComponent(accountId)}/owner-identity`, payload),
  bindQqContact: (accountId: string, payload: { person_id: string; sender_id: string }) =>
    put<QqIdentityItem>(`/api/qq/accounts/${encodeURIComponent(accountId)}/contact-identities`, payload),
  conversations: (scope: ConversationScope) =>
    get<{ items: ChatConversation[] }>(`/api/chat/conversations?scope=${encodeURIComponent(scope)}`),
  createConversation: (payload: {
    scope: ConversationScope
    purpose: ConversationPurpose
    title?: string
    persona_version_id?: string
  }) => post<ChatConversation>('/api/chat/conversations', payload),
  conversation: (id: string) =>
    get<{ conversation: ChatConversation; messages: ChatMessage[] }>(`/api/chat/conversations/${encodeURIComponent(id)}`),
  sendChatTurn: (id: string, payload: { content: string; idempotency_key: string }) =>
    post<ChatTurnResult>(`/api/chat/conversations/${encodeURIComponent(id)}/turns`, payload),
  stopConversation: (id: string) => post<ChatConversation>(`/api/chat/conversations/${encodeURIComponent(id)}/stop`),
  takeoverConversation: (id: string) => post<ChatConversation>(`/api/chat/conversations/${encodeURIComponent(id)}/takeover`),
  chatRequest: (id: string) => get<ChatTurnResult>(`/api/chat/requests/${encodeURIComponent(id)}`),
  chatDrafts: (options: { state?: 'review' | 'stale' | 'approved' | 'sent' | 'cancelled'; limit?: number } = {}) => {
    const query = new URLSearchParams()
    if (options.state) query.set('state', options.state)
    if (options.limit) query.set('limit', String(options.limit))
    const suffix = query.size ? `?${query.toString()}` : ''
    return get<{ items: ChatDraft[] }>(`/api/chat/drafts${suffix}`)
  },
  currentPersona: () => get<{ version: PersonaVersion | null }>('/api/persona/current'),
  personaInterviews: () => get<{ items: PersonaInterview[] }>('/api/persona/interviews'),
  createPersonaInterview: (payload: { title?: string }) =>
    post<{ interview: PersonaInterview; conversation: ChatConversation }>('/api/persona/interviews', payload),
  personaInterview: (id: string) => get<{ interview: PersonaInterview; conversation: ChatConversation; messages: ChatMessage[] }>(`/api/persona/interviews/${encodeURIComponent(id)}`),
  savePersonaDraft: (id: string, payload: { version_label: string; content: string; based_on_version_id?: string }) =>
    post<{ version: PersonaVersion; interview: PersonaInterview; conversation: ChatConversation }>(`/api/persona/interviews/${encodeURIComponent(id)}/drafts`, payload),
  personaFeedback: (id: string, payload: {
    inbound_message_id: string
    reply_message_id: string
    action: 'keep' | 'change' | 'reject'
    change_points?: string
  }) => post<PersonaFeedback>(`/api/persona/interviews/${encodeURIComponent(id)}/feedback`, payload),
  personaVersions: () => get<{ items: PersonaVersion[] }>('/api/persona/versions'),
  personaVersion: (id: string) => get<PersonaVersion>(`/api/persona/versions/${encodeURIComponent(id)}`),
  approvePersonaVersion: (id: string, payload: {
    interview_session_id: string
    inbound_message_id: string
    reply_message_id: string
    expected_state_revision: number
    version_hash: string
  }) => post<{ version: PersonaVersion; approval: PersonaApproval; evidence: Record<string, unknown> }>(`/api/persona/versions/${encodeURIComponent(id)}/approve`, payload),
  confirmedDesignImportInfo: () => get<ConfirmedDesignImportInfo>('/api/persona/confirmed-design-import'),
  importConfirmedDesign: (payload: { bundle_hash: string }) =>
    post<ConfirmedDesignImportResult>('/api/persona/confirmed-design-import', payload),
  memoryItems: (personId: string, options: { scope: ConversationScope; includeHistory?: boolean; limit?: number }) => {
    const query = new URLSearchParams({ scope: options.scope })
    if (options.includeHistory !== undefined) query.set('include_history', String(options.includeHistory))
    if (options.limit) query.set('limit', String(options.limit))
    return get<{ items: MemoryItem[]; person_id: string; scope: ConversationScope }>(
      `/api/memory/people/${encodeURIComponent(personId)}/items?${query.toString()}`,
    )
  },
  correctMemory: (id: string, payload: {
    corrected_content: string
    expected_version: number
    source_event_id: string
    source_message_id?: string
  }) => post<MemoryItem>(`/api/memory/items/${encodeURIComponent(id)}/correct`, payload),
  deleteMemory: (id: string, payload: { expected_version: number }) =>
    post<MemoryItem>(`/api/memory/items/${encodeURIComponent(id)}/delete`, payload),
  outbox: (options: { state?: string; limit?: number; offset?: number } = {}) => {
    const query = new URLSearchParams()
    if (options.state) query.set('state', options.state)
    query.set('limit', String(options.limit ?? 50))
    query.set('offset', String(options.offset ?? 0))
    return get<OutboxListData>(`/api/outbox?${query.toString()}`)
  },
  outboxItem: (id: string) => get<OutboxItem>(`/api/outbox/${encodeURIComponent(id)}`),
  cancelOutbox: (id: string) => post<OutboxItem>(`/api/outbox/${encodeURIComponent(id)}/cancel`),
  authorizeVoiceOutbox: (payload: VoiceOutboxAuthorizationRequest) =>
    post<VoiceOutboxAuthorizationResult>('/api/outbox/authorizations', payload),
  prepareVoiceOutbox: (payload: VoiceOutboxPrepareRequest) =>
    post<OutboxItem>('/api/outbox/prepare', payload),
  preflightOutbox: (id: string) => post<OutboxPreflightResult>(`/api/outbox/${encodeURIComponent(id)}/preflight`),
  dispatchOutbox: (id: string) => post<OutboxItem>(`/api/outbox/${encodeURIComponent(id)}/dispatch`),
  tasks: () => get<{ items: TaskItem[] }>('/api/tasks'),
  task: (id: string) => get<TaskItem>(`/api/tasks/${encodeURIComponent(id)}`),
  createTask: (payload: Pick<TaskItem, 'title' | 'intent'> & Partial<Pick<TaskItem, 'progress' | 'next_step' | 'source_refs' | 'capability_refs' | 'budget_refs'>>) =>
    post<TaskItem>('/api/tasks', payload),
  taskAction: (id: string, action: 'pause' | 'resume' | 'cancel' | 'handover', payload: { expected_revision: number; reason?: string; release_handover?: boolean }) =>
    post<TaskItem>(`/api/tasks/${encodeURIComponent(id)}/${action}`, payload),
  candidates: (options: { state?: CandidateState; limit?: number; offset?: number } = {}) => {
    const query = new URLSearchParams()
    if (options.state) query.set('state', options.state)
    if (options.limit) query.set('limit', String(options.limit))
    if (options.offset) query.set('offset', String(options.offset))
    const suffix = query.size ? `?${query.toString()}` : ''
    return get<{ items: CandidateItem[]; total: number }>(`/api/candidates${suffix}`)
  },
  candidate: (id: string) => get<CandidateItem>(`/api/candidates/${encodeURIComponent(id)}`),
  candidatePreview: (id: string) => get<Record<string, unknown>>(`/api/candidates/${encodeURIComponent(id)}/preview`),
  candidateAction: (id: string, action: 'cancel' | 'reassess', payload: { expected_revision: number; reason?: string }) =>
    post<Record<string, unknown>>(`/api/candidates/${encodeURIComponent(id)}/${action}`, payload),
  roomStatus: () => get<RoomStatus>('/api/rooms/status'),
  rooms: (options: { kind?: string; state?: string; limit?: number; offset?: number } = {}) => {
    const query = new URLSearchParams()
    if (options.kind) query.set('kind', options.kind)
    if (options.state) query.set('state', options.state)
    if (options.limit) query.set('limit', String(options.limit))
    if (options.offset) query.set('offset', String(options.offset))
    const suffix = query.size ? `?${query.toString()}` : ''
    return get<{ items: RoomItem[]; total: number }>(`/api/rooms/items${suffix}`)
  },
  room: (id: string) => get<RoomItem>(`/api/rooms/items/${encodeURIComponent(id)}`),
  roomContent: (id: string) => get<RoomContentRead>(`/api/rooms/items/${encodeURIComponent(id)}/content`),
  createRoomItem: (payload: RoomItemCreate) => post<RoomItem>('/api/rooms/items', payload),
  updateRoomItem: (id: string, payload: {
    expected_version: number
    title?: string
    tags?: string[]
    share_allowed?: boolean
    content_text?: string
    new_relative_path?: string
  }) => patch<RoomItem>(`/api/rooms/items/${encodeURIComponent(id)}`, payload),
  roomItemAction: (id: string, action: 'soft-delete' | 'restore', payload: { expected_version: number }) =>
    post<Record<string, unknown>>(`/api/rooms/items/${encodeURIComponent(id)}/${action}`, payload),
  exportRoomItem: (id: string, payload: { expected_version: number }) =>
    post<RoomExportResult>(`/api/rooms/items/${encodeURIComponent(id)}/export`, payload),
  registerManagedMedia: (payload: {
    root_key: 'room_root' | 'gallery_root'
    relative_path: string
    source_refs: RoomItemCreate['source_refs']
    source_scope: RoomItemCreate['source_scope']
    source_person_id?: string
    mime_type?: string
    role_use_approved?: boolean
    share_allowed?: boolean
  }) => post<ManagedMedia>('/api/rooms/managed-media', payload),
  ttsCapabilities: () => get<TtsCapabilities>('/api/extensions/tts'),
  previewTts: (payload: { text: string; voice_id: string; task_id?: string; expected_task_revision?: number }) =>
    postAudioPreview('/api/extensions/tts/preview', payload),
  voiceProfile: () => get<VoiceProfileData>('/api/extensions/voice/profile'),
  saveVoiceProfile: (payload: { expected_revision: number; voice_id: string }) =>
    put<VoiceProfileData>('/api/extensions/voice/profile', payload),
  saveVoiceSettings: (payload: { expected_revision: number; runtime_enabled: boolean; voice_outbox_enabled: boolean }) =>
    put<VoiceProfileData>('/api/extensions/voice/settings', payload),
  createVoicePreview: (payload: { expected_profile_version: number; text: string }) =>
    post<VoiceArtifact>('/api/extensions/voice/previews', payload),
  approveVoicePreview: (payload: { expected_profile_version: number; preview_artifact_id: string }) =>
    post<VoiceApprovalResult>('/api/extensions/voice/approve', payload),
  generateVoiceReply: (payload: VoiceReplyRequest) =>
    post<VoiceArtifact>('/api/extensions/voice/replies', payload),
  voiceArtifactContent: (artifactId: string) => getVoiceArtifactContent(artifactId),
  asrCapabilities: () => get<AsrCapabilities>('/api/extensions/asr'),
  transcribeRoomAudio: (payload: AsrTranscriptionRequest) =>
    post<AsrTranscriptionResult>('/api/extensions/asr/transcriptions', payload),
  visionCapability: () => get<VisionCapability>('/api/vision/capability'),
  observeRoomMedia: (payload: { room_item_id: string; expected_room_version: number; detail_level?: 'low' | 'high' }) =>
    post<VisionObservationResult>('/api/vision/observe', payload),
  migrationStatus: () => get<MigrationStatus>('/api/migration/status'),
  inspectMigrationSource: () => post<MigrationPreflight>('/api/migration/inspect'),
  exportMigrationBundle: (payload: { destination_relative_path: string; media_ids: string[] }) =>
    post<MigrationExportResult>('/api/migration/export', payload),
  validateMigrationBundle: (payload: { bundle_relative_path: string }) =>
    post<MigrationValidationResult>('/api/migration/validate', payload),
  importMigrationStaging: (payload: { bundle_relative_path: string; target_relative_path: string }) =>
    post<MigrationImportResult>('/api/migration/import', payload),
  proactivityPolicy: () => get<ProactivityPolicy>('/api/proactivity/policy'),
  saveProactivityPolicy: (payload: ProactivityPolicyUpdate) => put<ProactivityPolicy>('/api/proactivity/policy', payload),
  createProactivityBatch: (payload: ProactivityBatchCreate) => post<TrialBatch>('/api/proactivity/batches', payload),
  generateProactivityBatch: (id: string) => post<ProactivityBatchResult>(`/api/proactivity/batches/${encodeURIComponent(id)}/generate`),
  integrationCapabilities: () => get<{ items: IntegrationCapability[] }>('/api/integrations/capabilities'),
  integrationRead: (payload: IntegrationSourceReadRequest) =>
    post<IntegrationSourceReadResult>('/api/integrations/read', payload),
  integrationPreview: (payload: IntegrationActionPreviewRequest) => post<IntegrationActionPreview>('/api/integrations/preview', payload),
  batches: () => get<{ items: TrialBatch[] }>('/api/trial-batches'),
  createBatch: (payload: TrialBatchCreate) => post<TrialBatch>('/api/trial-batches', payload),
  startBatch: (id: string) => post<TrialBatch>(`/api/trial-batches/${encodeURIComponent(id)}/start`),
  stopBatch: (id: string) => post<TrialBatch>(`/api/trial-batches/${encodeURIComponent(id)}/stop`),
  uiSettings: () => get<UiSettings>('/api/settings/ui'),
  saveUiSettings: (payload: Pick<UiSettings, 'theme' | 'locale'> & { expected_version: number }) =>
    patch<UiSettings>('/api/settings/ui', payload),
  deepSeekCredentialStatus: () => get<DeepSeekCredentialStatus>('/api/providers/deepseek/credential'),
  saveDeepSeekCredential: (apiKey: string) =>
    put<DeepSeekCredentialStatus>('/api/providers/deepseek/credential', { api_key: apiKey }),
  hostSettings: () => get<HostSettings>('/api/settings/host'),
  moduleConfig: (id: string) => get<ModuleConfigData>(`/api/modules/${encodeURIComponent(id)}/config`),
  saveModuleConfig: (id: string, payload: { values: Record<string, unknown>; expected_version: number }) =>
    put<ModuleConfigData>(`/api/modules/${encodeURIComponent(id)}/config`, payload),
}

