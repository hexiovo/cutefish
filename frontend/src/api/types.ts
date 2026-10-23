export type ResultStatus =
  | 'success'
  | 'failed'
  | 'unknown'
  | 'not_connected'
  | 'needs_login'
  | 'needs_input'
  | 'needs_verification'
  | 'partial'
  | 'unsupported'
  | (string & {})

export interface ApiErrorBody {
  code?: string
  message?: string
  retryable?: boolean
  request_id?: string
  details?: unknown
}

export interface ApiEnvelope<T> {
  status: ResultStatus
  data: T | null
  evidence?: unknown
  error?: ApiErrorBody | null
  usage?: unknown
  request_id?: string
  time?: string
}

export interface BootstrapData {
  setup_required: boolean
  csrf_token: string
}

export interface SessionData {
  authenticated: boolean
  setup_required: boolean
  principal?: { id?: string; role?: string; display_name?: string } | null
  csrf_token?: string | null
}

export interface OverviewData {
  mode?: string
  host_id?: string | null
  active_trial_batch?: TrialBatch | null
  pending_outbox_count?: number | null
  budget_snapshot?: BudgetSnapshot | null
  app_version?: string | null
  services?: Record<string, ServiceStatus | null> | null
  database?: ServiceStatus | null
  [key: string]: unknown
}

export interface ServiceStatus {
  status?: string
  connected?: boolean | null
  available?: boolean | null
  label?: string
  detail?: string | null
}

export interface BudgetSnapshot {
  spent?: number | null
  reserved?: number | null
  unknown?: number | null
  limit?: number | null
  timezone?: string | null
  background_soft_limit?: number | null
  background_borrow_limit?: number | null
  background_hard_limit?: number | null
  currency?: string | null
  [key: string]: unknown
}

export type BudgetEntryState = 'reserved' | 'dispatched' | 'unknown' | 'settled' | 'released'

export interface BudgetEntry {
  reservation_id: string
  idempotency_key?: string
  provider?: string | null
  model?: string | null
  purpose?: string | null
  person?: string | null
  account?: string | null
  local_date?: string | null
  estimated_cost?: number | null
  actual_cost?: number | null
  state: BudgetEntryState | (string & {})
  is_background?: boolean
  currency?: string | null
  price_version?: string | null
  max_input_tokens?: number | null
  max_output_tokens?: number | null
  rates?: Record<string, unknown> | null
}

export interface PersonItem {
  person_id: string
  display_name: string
  role: string
  state: string
  chat_only: boolean
}

export interface QqServiceStatus {
  display_name?: string | null
  status?: string
  connected?: boolean | null
  needs_credentials?: boolean
  needs_connection?: boolean
  owner_configured?: boolean
  enabled?: boolean
  group_chat_enabled?: boolean
  detail?: string | null
}

export interface QqAccountItem {
  account_id: string
  display_name?: string | null
  status: string
  service_url?: string | null
  owner_sender_id?: string | null
  needs_credentials: boolean
  needs_connection: boolean
  owner_configured?: boolean
  enabled?: boolean
  group_chat_enabled?: boolean
}

export interface QqIdentityItem {
  id: string
  account_id: string
  person_id: string
  sender_id: string
  is_owner: boolean
  is_group: boolean
}

export type ConversationScope = 'production' | 'calibration'
export type ConversationPurpose = 'production' | 'persona_interview' | 'persona_trial'

export interface ChatConversation {
  id: string
  person_id: string
  scope: ConversationScope
  channel: string
  purpose: ConversationPurpose
  delivery_mode: string
  revision: number
  state: string
  stop_requested: boolean
  human_takeover: boolean
  account_id?: string | null
  recipient_id?: string | null
  batch_id?: string | null
  persona_version_id?: string | null
  title?: string | null
  created_at: string
  updated_at: string
  [key: string]: unknown
}

export interface ChatMessage {
  id: string
  conversation_id: string
  person_id: string
  scope: ConversationScope
  role: string
  content: string
  channel: string
  provider?: string | null
  model?: string | null
  provider_response_id?: string | null
  persona_version_id?: string | null
  config_fingerprint?: string | null
  token_usage?: Record<string, unknown> | null
  source_event_id?: string | null
  created_at: string
  [key: string]: unknown
}

export interface ChatRequestStatus {
  id?: string
  local_request_id?: string
  conversation_id?: string
  inbound_message_id?: string | null
  response_message_id?: string | null
  state?: string
  status?: string
  provider?: string | null
  model?: string | null
  error_code?: string | null
  error_message?: string | null
  request_id?: string | null
  [key: string]: unknown
}

export interface ChatTurnResult {
  conversation: ChatConversation
  message: ChatMessage | null
  inbound_message?: ChatMessage | null
  request: ChatRequestStatus
  usage?: Record<string, unknown> | null
}

export interface MemoryItem {
  id: string
  person_id: string
  scope: ConversationScope
  kind: string
  content: string
  status: string
  version: number
  source_message_id?: string | null
  source_event_id?: string | null
  source_reference?: string | null
  source_locator?: string | null
  valid_from?: string | null
  valid_until?: string | null
  supersedes_id?: string | null
  corrected_by_id?: string | null
  invalidation_reason?: string | null
  created_at?: string
}

export interface OutboxItem {
  id: string
  idempotency_key: string
  request_id?: string | null
  conversation_id: string
  expected_revision: number
  batch_id: string
  recipient_id: string
  account_id: string
  actor_id: string
  person_id: string
  action_id: string
  message_type?: 'text' | 'record' | (string & {})
  record_ref?: VoiceOutboxRecordRef | null
  authorization_id?: string | null
  delivery_mode: string
  batch_version: number
  dispatch_reason: string
  segment_index: number
  segment_count: number
  payload_hash: string
  state: string
  attempt_count: number
  attempt_id?: string | null
  remote_receipt_id?: string | null
  dispatch_evidence_ref?: string | null
  result_code?: string | null
  created_at: string
  sending_at?: string | null
  finished_at?: string | null
  payload?: string | Record<string, unknown> | null
}

export interface VoiceOutboxRecordRef {
  artifact_id: string
  sha256: string
  voice_version: string
  approval_ref: string
}

export interface VoiceOutboxAuthorizationRequest {
  action_id: 'qq.voice.send'
  artifact_id: string
  conversation_id: string
  expected_revision: number
  batch_id: string
  expires_in_seconds: number
}

export interface VoiceOutboxAuthorizationResult {
  id: string
  authorization_id: string
  action_id: 'qq.voice.send'
  person_id: string | null
  conversation_id: string
  conversation_revision: number
  delivery_mode: string
  recipient_id: string
  account_id: string
  batch_id: string
  batch_version: number
  payload_hash: string
  expires_at: string
  state: 'active' | string
  record_ref: VoiceOutboxRecordRef
}

export interface VoiceOutboxPrepareRequest {
  action_id: 'qq.voice.send'
  artifact_id: string
  conversation_id: string
  expected_revision: number
  batch_id: string
  authorization_id: string
  idempotency_key: string
}

export interface OutboxPreflightResult {
  state: 'validated' | string
  conversation_revision: number
  outbox_id: string
}

export interface OutboxListData {
  items: OutboxItem[]
  total: number
  limit: number
  offset: number
}

export interface ChatDraft {
  id: string
  conversation_id: string
  person_id: string
  scope: ConversationScope
  source_revision: number
  revision: number
  source_message_id: string
  content: string
  state: 'review' | 'stale' | 'approved' | 'sent' | 'cancelled' | string
  stale_reason?: string | null
  created_at: string
  updated_at: string
}

export type TaskStatus = 'open' | 'active' | 'paused' | 'done' | 'abandoned'
export type TaskEffectState = 'not_dispatched' | 'pending' | 'sending' | 'sent' | 'failed' | 'unknown' | 'cancelled'

export interface TaskItem {
  id: string
  title: string
  intent: string
  progress?: string | null
  next_step?: string | null
  status: TaskStatus
  source_refs: string[]
  capability_refs: string[]
  budget_refs: string[]
  revision: number
  explicit_pause: boolean
  manual_takeover: boolean
  pause_reason?: string | null
  created_at: string
  updated_at: string
  effect_state: TaskEffectState
}

export type CandidateState = 'proposed' | 'draft' | 'prepared' | 'cancelled' | 'expired'

export interface CandidateItem {
  id: string
  task_id?: string | null
  title: string
  target_ref?: string | null
  source_event_refs: string[]
  experience_ref?: string | null
  reason: string
  context: Record<string, unknown>
  not_before?: string | null
  expires_at?: string | null
  source_revision: number
  permission_revision: number
  budget_estimate?: number | null
  defer_reason?: string | null
  state: CandidateState
  revision: number
  outbox_id?: string | null
}

export interface RoomStatus {
  root_key: 'room_root' | string
  configured: boolean
  disk_state: 'unknown' | 'available' | 'unavailable' | string
  authorization_state: 'needs_authorization' | 'granted' | 'revoked' | string
  revision: number
}

export type RoomSourceScope = 'production' | 'calibration'
export type SourceReferenceKind = 'room_item' | 'managed_media' | 'chat_event' | 'owner_supplied'

export interface SourceReference {
  kind: SourceReferenceKind
  id: string
  person_id?: string | null
  scope?: RoomSourceScope
}

export interface DeepSeekCredentialStatus {
  provider: 'deepseek'
  configured: boolean
}

export interface RoomItem {
  id: string
  kind: string
  title: string
  root_key: 'room_root' | string
  relative_path?: string | null
  source_refs: SourceReference[]
  source_scope: RoomSourceScope
  content_hash?: string | null
  share_allowed: boolean
  tags: string[]
  state: 'pending' | 'ready' | 'trashed' | 'unavailable' | string
  version: number
  created_at: string
  updated_at: string
  content_text?: string | null
  managed_media_id?: string | null
}

export interface RoomExportResult {
  item: RoomItem
  manifest: {
    format_version: string | number
    exported_at: string
    filename: string
    sha256: string
    byte_length: number
    source_scope: RoomSourceScope
  }
  content: {
    encoding: 'utf-8' | 'base64'
    media_type: string
    data: string
  }
}

export interface RoomContentRead {
  item_id: string
  encoding: 'utf-8' | 'base64'
  media_type: string
  content_hash?: string | null
  data: string
}

export interface TtsVoice {
  voice_id: string
  name: string
  culture: string
}

export interface TtsCapabilities {
  status: 'partial' | 'needs_resource' | 'unsupported' | (string & {})
  engine_id: 'windows.system_speech' | (string & {})
  runtime_available: boolean
  voices: TtsVoice[]
  approved_voice_id: string | null
  supports_custom_voice: false
  supports_instruction_expression: false
  supports_semantic_emphasis: false
  supports_pause_control: false
  default_reply_mode: 'text' | (string & {})
  automatic_voice_reply: false
  audio_played: false
  model_downloaded: false
  detail: string
}

export interface TtsPreviewResult {
  audio: Blob
  engine: string | null
  voiceId: string | null
  approved: false
  capability: string | null
}

export interface VoiceProfile {
  profile_id: string
  profile_version: number
  voice_version: string
  engine_id: string
  voice_id: string
  voice_name: string
  culture: string
  settings_hash: string
  capability: 'plain_reading' | (string & {})
}

export interface VoiceApproval {
  approval_id: string
  preview_artifact_id: string
  profile_id: string
  profile_version: number
  approved_at: string
}

export interface VoiceProfileData {
  revision: number
  deployment_profile: 'local_trial' | 'target_staging' | (string & {})
  source_mode: string
  runtime_enabled: boolean
  voice_outbox_enabled: boolean
  production_enabled: false
  runtime_available?: boolean | null
  readiness: 'needs_profile' | 'needs_approval' | 'runtime_disabled' | 'voice_outbox_disabled' | 'ready' | (string & {})
  profile: VoiceProfile | null
  approval: VoiceApproval | null
  capabilities: {
    supports_custom_voice: boolean
    supports_instruction_expression: boolean
    supports_semantic_emphasis: boolean
    supports_pause_control: boolean
  }
}

export interface VoiceArtifact {
  artifact_id: string
  profile_id: string
  profile_version: number
  voice_version: string
  sha256: string
  media_format: 'wav' | (string & {})
  duration_ms: number
  purpose: 'approval_preview' | 'reply' | (string & {})
  artifact_state?: 'ready' | 'cancelled' | (string & {})
}

export interface VoiceApprovalResult {
  approval_id: string
  profile_id: string
  profile_version: number
  preview_artifact_id: string
  approved_at: string
}

export interface AsrCapabilities {
  status: 'ready' | 'needs_resource' | 'unsupported' | (string & {})
  model_candidates: string[]
  compute_type: string
  max_duration_seconds: number
  max_bytes: number
  accepted_formats: string[]
  detail?: string | null
}

export interface AsrTranscriptionRequest {
  room_item_id: string
  expected_room_version: number
  task_id?: string
  expected_task_revision?: number
  submit_to_chat?: boolean
  conversation_id?: string
  expected_conversation_revision?: number
  chat_idempotency_key?: string
}

export interface AsrTranscriptionResult {
  status: 'needs_resource' | 'transcribed' | (string & {})
  transcript: string | null
  language?: string | null
  source?: {
    room_item_id: string
    room_version: number
    media_type: string
    duration_seconds: number | null
    byte_length: number
    content_hash: string
  }
  downstream: {
    chat_service: string
    voice_reply: string
    qq_outbox: string
  }
  chat_request?: ChatRequestStatus | null
  detail?: string
  [key: string]: unknown
}

export interface ManagedMedia {
  id: string
  root_key: 'room_root' | 'gallery_root' | string
  relative_path: string
  sha256: string
  mime_type: string
  byte_length: number
  source_refs: SourceReference[]
  source_scope: RoomSourceScope
  source_person_id?: string | null
  role_use_approved: boolean
  share_allowed: boolean
  version: number
  created_at?: string
}

export interface VisionCapability {
  state: 'needs_model_verification' | 'needs_resource' | (string & {})
  visual_model_status: string
  pricing_status: string
  decoder_available: boolean
  supported_mime_types: string[]
  max_bytes: number
  max_pixels: number
}

export interface VisionObservationResult {
  state: string
  media: {
    room_item_id: string
    room_item_version: number
    managed_media_id: string
    sha256: string
    mime_type: string
    width: number
    height: number
    pixel_count: number
    byte_length: number
    root_key: string
    relative_path: string
    media_version: number
    pixel_digest: string
  }
  observation: null
  source_refs: SourceReference[]
}

export interface MigrationRestorePolicy {
  external_actions_enabled: false
  outbox_dispatch_enabled: false
  paid_provider_calls_enabled: false
  scheduling_enabled: false
  auto_start_enabled: false
  task_auto_resume: false
}

export interface MigrationLogicalRoot {
  root_key: string
  configured: boolean
  available: boolean | null
}

export interface MigrationStatus {
  deployment_profile: 'local_trial' | 'target_staging'
  target_staging: boolean
  restore_policy: MigrationRestorePolicy
  logical_roots: MigrationLogicalRoot[]
}

export interface MigrationPreflight {
  available: boolean
  schema_version: number | null
  schema_supported: boolean
  table_count: number
  registered_media_count: number
  active_batch_count: number
  pending_outbox_count: number
  sending_outbox_count: number
  errors: string[]
  warnings: string[]
}

export interface MigrationExportResult {
  export_id: string
  bundle_root_key: 'backup_root'
  bundle_relative_path: string
  database_sha256: string
  manifest_sha256: string
  media_count: number
  sanitized: true
}

export interface MigrationValidationResult {
  valid: boolean
  export_id: string | null
  database_sha256: string | null
  media_count: number
  errors: string[]
  warnings: string[]
}

export interface MigrationImportResult {
  imported: true
  deployment_profile: 'target_staging'
  target_relative_path: string
  bundle_export_id: string
  requires_restart: true
  restore_policy: MigrationRestorePolicy
}

export interface ProactivityPolicy {
  revision: number
  enabled: boolean
  mode: 'manual'
  explicit_pause: boolean
  dnd_enabled: boolean
  manual_takeover: boolean
  updated_by: string | null
  updated_at: string | null
}

export interface ProactivityPolicyUpdate {
  expected_revision: number
  enabled: boolean
  explicit_pause: boolean
  dnd_enabled: boolean
  manual_takeover: boolean
}

export interface ProactivityBatchCreate {
  label: string
  conversation_id: string
  source_event_ids: string[]
  expires_at: string
  max_candidates: 1 | 2 | 3
  task_id?: string
}

export interface ProactivityBatchResult {
  state: 'created' | 'disabled' | 'paused' | 'dnd' | 'manual_takeover' | 'stopped' | 'expired' | 'policy_changed' | 'source_changed' | 'budget_unavailable' | 'needs_credentials' | 'duplicate' | 'unknown' | 'failed' | (string & {})
  checks: {
    policy: string
    conversation: string
    batch: string
    budget: string
  }
  usage: Record<string, unknown> | null
  ledger: {
    available: number
    background_available: number
    background_spent: number
    background_reserved: number
    background_unknown: number
    currency: 'CNY' | string
  } | null
  candidates: CandidateItem[]
  chat_result: { conversation_id: string; request_id: string; state: string } | null
}

export type ImplementedFeatureStatus = 'complete' | 'not_implemented' | 'partial' | 'unknown'
export type VerifiedFeatureStatus = 'passed' | 'partial' | 'failed' | 'not_run' | 'unknown'
export type ProductionFeatureStatus = 'enabled' | 'disabled' | 'unknown'

export interface FeatureStatusValue<TStatus extends string = string> {
  status: TStatus
  evidence_refs?: string[]
  note?: string | null
}

export interface FeatureEvidenceStatus {
  implemented?: FeatureStatusValue<ImplementedFeatureStatus>
  local_verified?: FeatureStatusValue<VerifiedFeatureStatus>
  target_verified?: FeatureStatusValue<VerifiedFeatureStatus>
  production_enabled?: FeatureStatusValue<ProductionFeatureStatus>
  status_updated_at?: string | null
  evidence_revision?: string | number | null
}

export interface IntegrationsSnapshot {
  [key: string]: unknown
}

export interface IntegrationCapability {
  site_id: string
  capability_id: string
  name: string
  status: 'needs_configuration' | 'needs_adapter_verification' | 'unsupported_action' | 'disabled' | 'ready_local' | (string & {})
  reason: string
  target_configured: boolean
  handler_registered: boolean
  read_registered?: boolean
}

export interface IntegrationSourceReadRequest {
  site_id: string
  capability_id: string
  object_id: string
  query?: Record<string, string>
}

export interface IntegrationSourceEvidence {
  site_id: string
  capability_id: string
  source_url: string
  object_id: string
  content_type: string
  fetched_at: string
  sha256: string
  byte_length: number
  read_scope: 'metadata' | 'content' | 'subtitle' | 'frame' | 'range'
  time_range?: { start_ms: number; end_ms: number } | null
}

export interface IntegrationSourceReadResult {
  source_ref_id: string
  evidence: IntegrationSourceEvidence
  content_base64: string
}

export interface IntegrationActionScope {
  kind: 'single_object'
  site_id: string
  object_id: string
  action: string
}

export interface IntegrationActionPreview {
  preview_id: string
  site_id: string
  account_ref?: string | null
  object_id: string
  action: string
  payload_json: string
  payload_hash: string
  source_refs: IntegrationSourceEvidence[]
  scope: IntegrationActionScope
  created_at: string
  preview_hash: string
  state: 'preview_only'
  outbox_preview?: Record<string, unknown>
}

export interface IntegrationActionPreviewRequest {
  site_id: string
  account_ref?: string
  object_id: string
  action: string
  payload: Record<string, unknown>
  source_ref_ids: string[]
  scope: IntegrationActionScope
  expected_revision?: number
}

export interface RoomItemCreate {
  kind: 'collection' | 'note' | 'interest' | 'plan' | 'work' | 'album' | 'share_candidate'
  title: string
  relative_path?: string
  content_text?: string
  managed_media_id?: string
  source_refs: SourceReference[]
  source_scope: RoomSourceScope
  tags?: string[]
  share_allowed?: boolean
}

export interface PersonaInterview {
  id: string
  person_id: string
  persona_version_id?: string | null
  conversation_id: string
  state: 'active' | 'complete' | string
  title: string
  created_at: string
  completed_at?: string | null
}

export interface PersonaVersion {
  id: string
  persona_key: string
  version_number: number
  version_label: string
  content: string
  content_hash: string
  state: 'draft' | 'approved' | 'superseded' | string
  source_kind: string
  source_request_id?: string | null
  source_interview_id?: string | null
  based_on_version_id?: string | null
  source_bundle_hash?: string | null
  source_hashes?: Record<string, string>
  contains_confirmed_constraint_excerpts?: boolean
  confirmed_constraint_refs?: string[]
  import_schema_version?: number | null
  config_fingerprint: string
  created_by_person_id: string
  created_at: string
  state_revision: number
  approved_at?: string | null
}

export interface ConfirmedDesignImportSource {
  basename: string
  sha256: string
  source_ref: string
  status: 'verified'
}

export interface ConfirmedDesignImportInfo {
  status: 'available' | 'not_configured' | 'unavailable'
  available: boolean
  reason_code: string | null
  bundle_hash: string | null
  schema_version: number | null
  sources: ConfirmedDesignImportSource[]
  context_capacity: {
    target_tokens: { owner: 131072; contact: 32768 }
    actual_max_utf8_bytes: { owner: number; contact: number }
    token_capacity_status: 'partial'
  }
}

export interface ConfirmedDesignImportedVersion extends PersonaVersion {
  source_bundle_hash: string
  source_hashes: Record<string, string>
  contains_confirmed_constraint_excerpts: boolean
  confirmed_constraint_refs: string[]
  import_schema_version: number
}

export interface ConfirmedDesignImportResult {
  import_status: 'created' | 'already_imported'
  version: ConfirmedDesignImportedVersion
  interview: PersonaInterview
  conversation: ChatConversation
  source_bundle_hash: string
  sources: ConfirmedDesignImportSource[]
}

export interface PersonaFeedback {
  id: string
  persona_version_id: string
  interview_session_id: string
  inbound_message_id: string
  reply_message_id: string
  action: 'keep' | 'change' | 'reject' | string
  change_points?: string | null
  created_at: string
}

export interface PersonaApproval {
  id: string
  persona_version_id: string
  interview_session_id: string
  inbound_message_id: string
  reply_message_id: string
  actor_person_id: string
  version_hash: string
  config_fingerprint: string
  approved_at: string
}

export interface ModuleItem {
  id: string
  name?: string
  version?: string | null
  enabled?: boolean | null
  ready?: boolean | null
  runtime_state?: string | null
  lifecycle?: string | null
  ui_route?: string | null
  dependencies?: string[]
  capabilities?: Array<{ id: string; name?: string; side_effect?: string }>
  config_schema?: Record<string, unknown> | null
  [key: string]: unknown
}

export interface FeatureItem {
  feature_id?: string
  id?: string
  name?: string
  requirement_status?: string
  delivery_status?: string
  module_id?: string | null
  evidence_refs?: string[]
  implemented?: FeatureStatusValue<ImplementedFeatureStatus>
  local_verified?: FeatureStatusValue<VerifiedFeatureStatus>
  target_verified?: FeatureStatusValue<VerifiedFeatureStatus>
  production_enabled?: FeatureStatusValue<ProductionFeatureStatus>
  status_updated_at?: string | null
  evidence_revision?: string | number | null
  [key: string]: unknown
}

export interface FeatureStatusData {
  source_status: 'available' | 'unknown'
  items: FeatureItem[]
}

export interface HostSettings {
  host_id?: string | null
  mode?: string | null
  timezone?: string | null
  roots?: Array<{
    root_key: string
    configured: boolean
    available?: boolean | null
    display_path?: string | null
  }>
}

export interface UiSettings {
  theme: 'light' | 'dark' | 'system'
  locale: 'zh-CN'
  version: number
  updated_at?: string
}

export interface TrialBatch {
  id: string
  label?: string
  state: string
  mode?: string
  target?: {
    module_id?: string
    capability_id?: string
    object_ids?: string[]
  }
  expires_at?: string | null
  stop_condition?: { kind?: string; max_actions?: number } | null
  version?: number
  created_at?: string
  [key: string]: unknown
}

export interface TrialBatchCreate {
  label: string
  target: {
    module_id: string
    capability_id?: string
    object_ids: string[]
  }
  expires_at: string
  stop_condition: { kind: 'manual' } | { kind: 'after_n'; max_actions: number }
}

export interface VoiceReplyRequest {
  conversation_id: string
  expected_conversation_revision: number
  source_message_id: string
  batch_id: string
  expected_batch_version: number
}

export interface ModuleConfigData {
  schema: Record<string, unknown>
  values: Record<string, unknown>
  version: number
}
