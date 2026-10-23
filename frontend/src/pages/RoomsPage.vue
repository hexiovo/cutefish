<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { NAlert, NButton, NCard, NEmpty, NInput, NPopconfirm, NSelect, NTag } from 'naive-ui'
import { api } from '../api/client'
import { errorMessage } from '../api/errors'
import type { ManagedMedia, RoomContentRead, RoomExportResult, RoomItem, RoomItemCreate, RoomSourceScope, RoomStatus, SourceReference, SourceReferenceKind, VisionCapability, VisionObservationResult } from '../api/types'
import AppIcon from '../components/AppIcon.vue'
import StatusPill from '../components/StatusPill.vue'

const status = ref<RoomStatus | null>(null)
const items = ref<RoomItem[]>([])
const selectedId = ref('')
const selected = ref<RoomItem | null>(null)
const loading = ref(false)
const actionId = ref('')
const error = ref('')
const notice = ref('')
const kindFilter = ref('')
const stateFilter = ref('')
const total = ref(0)
const exportResult = ref<RoomExportResult | null>(null)
const roomContent = ref<RoomContentRead | null>(null)
const readingContent = ref(false)
const registeringMedia = ref(false)
const mediaPath = ref('')
const roleUseApproved = ref(false)
const registeredMedia = ref<ManagedMedia | null>(null)
const visionCapability = ref<VisionCapability | null>(null)
const loadingVision = ref(false)
const observingMedia = ref(false)
const visionObservation = ref<VisionObservationResult | null>(null)

const writeAllowed = computed(() => Boolean(status.value?.configured && status.value.authorization_state === 'granted'))
const roots = computed(() => status.value?.configured ? 'room_root 已配置' : 'room_root 未配置')
const kindOptions = [
  { label: '全部类型', value: '' },
  { label: '收藏集合', value: 'collection' },
  { label: '笔记', value: 'note' },
  { label: '兴趣', value: 'interest' },
  { label: '计划', value: 'plan' },
  { label: '作品', value: 'work' },
  { label: '相册索引', value: 'album' },
  { label: '分享候选', value: 'share_candidate' },
]
const stateOptions = [
  { label: '所有状态', value: '' },
  { label: '待处理', value: 'pending' },
  { label: '可用', value: 'ready' },
  { label: '已移入回收站', value: 'trashed' },
  { label: '暂不可用', value: 'unavailable' },
]

const createKind = ref<RoomItemCreate['kind']>('note')
const createTitle = ref('')
const createPath = ref('')
const createContent = ref('')
const managedMediaId = ref('')
type SourceReferenceDraft = { kind: SourceReferenceKind; id: string; person_id: string; scope: RoomSourceScope | '' }
const createSources = ref<SourceReferenceDraft[]>([{ kind: 'owner_supplied', id: '', person_id: '', scope: '' }])
const createScope = ref<RoomSourceScope>('production')
const createTags = ref('')
const createShareAllowed = ref(false)

const editTitle = ref('')
const editPath = ref('')
const editContent = ref('')
const editTags = ref('')
const editShareAllowed = ref(false)
const editing = ref(false)
const itemKinds = [
  { label: '笔记', value: 'note' },
  { label: '兴趣', value: 'interest' },
  { label: '计划', value: 'plan' },
  { label: '作品', value: 'work' },
  { label: '收藏集合', value: 'collection' },
  { label: '相册索引', value: 'album' },
  { label: '分享候选', value: 'share_candidate' },
]
const sourceScopeOptions = [
  { label: '正式范围（production）', value: 'production' },
  { label: '校准范围（calibration）', value: 'calibration' },
]
const optionalSourceScopeOptions = [
  { label: '跟随条目范围', value: '' },
  ...sourceScopeOptions,
]
const sourceKindOptions = [
  { label: '房间条目', value: 'room_item' },
  { label: '已登记媒体', value: 'managed_media' },
  { label: '聊天事件', value: 'chat_event' },
  { label: '本人直接提供', value: 'owner_supplied' },
]
const sourceKindLabels: Record<SourceReferenceKind, string> = {
  room_item: '房间条目',
  managed_media: '已登记媒体',
  chat_event: '聊天事件',
  owner_supplied: '本人直接提供',
}
const sourceRefsValid = computed(() => createSources.value.length > 0 && createSources.value.every((source) =>
  Boolean(source.id.trim())
  && (source.kind !== 'owner_supplied' || !source.person_id.trim())
  && (!source.scope || source.scope === createScope.value),
))

function splitLines(value: string) {
  return value.split(/[\r\n,，]+/).map((part) => part.trim()).filter(Boolean)
}

function addSourceReference() {
  createSources.value.push({ kind: 'owner_supplied', id: '', person_id: '', scope: '' })
}

function removeSourceReference(index: number) {
  createSources.value.splice(index, 1)
}

function setSourceReferenceKind(index: number, kind: SourceReferenceKind) {
  const source = createSources.value[index]
  if (!source) return
  source.kind = kind
  if (kind === 'owner_supplied') source.person_id = ''
}

function sourceReferencesPayload(): SourceReference[] {
  return createSources.value.map((source) => ({
    kind: source.kind,
    id: source.id.trim(),
    ...(source.person_id.trim() ? { person_id: source.person_id.trim() } : {}),
    ...(source.scope ? { scope: source.scope } : {}),
  }))
}

function formatSourceReference(source: SourceReference) {
  const details = [
    sourceKindLabels[source.kind],
    source.id,
    source.person_id ? `person_id: ${source.person_id}` : '',
    source.scope ? `scope: ${source.scope}` : '',
  ].filter(Boolean)
  return details.join(' · ')
}

function formatTime(value?: string | null) {
  if (!value) return '未提供'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN')
}

function syncEditForm(item: RoomItem | null) {
  roomContent.value = null
  exportResult.value = null
  editTitle.value = item?.title ?? ''
  editPath.value = item?.relative_path ?? ''
  editContent.value = item?.content_text ?? ''
  editTags.value = item?.tags?.join(', ') ?? ''
  editShareAllowed.value = Boolean(item?.share_allowed)
  editing.value = false
  visionObservation.value = null
}

async function loadStatus() {
  try {
    status.value = await api.roomStatus()
  } catch (cause) {
    error.value = errorMessage(cause)
    status.value = null
  }
}

async function loadList() {
  loading.value = true
  error.value = ''
  try {
    const result = await api.rooms({ kind: kindFilter.value || undefined, state: stateFilter.value || undefined, limit: 100, offset: 0 })
    items.value = result.items
    total.value = result.total
    if (selectedId.value && result.items.some((item) => item.id === selectedId.value)) await loadItem(selectedId.value)
    else {
      selectedId.value = result.items[0]?.id ?? ''
      if (selectedId.value) await loadItem(selectedId.value)
      else {
        selected.value = null
        syncEditForm(null)
      }
    }
  } catch (cause) {
    items.value = []
    selected.value = null
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

async function loadItem(id: string) {
  if (!id) return
  try {
    selected.value = await api.room(id)
    syncEditForm(selected.value)
  } catch (cause) {
    error.value = errorMessage(cause)
    selected.value = null
  }
}

async function refresh() {
  await loadStatus()
  await loadList()
}

async function readContent(item: RoomItem) {
  if (!writeAllowed.value || readingContent.value) return
  readingContent.value = true
  error.value = ''
  notice.value = ''
  roomContent.value = null
  try {
    roomContent.value = await api.roomContent(item.id)
    if (roomContent.value.encoding === 'utf-8') editContent.value = roomContent.value.data
    notice.value = '已通过单项内容读取接口读取；该结果不代表获得其他路径授权。'
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    readingContent.value = false
  }
}

async function registerMedia() {
  if (!writeAllowed.value || !mediaPath.value.trim() || !sourceRefsValid.value || registeringMedia.value) return
  registeringMedia.value = true
  error.value = ''
  notice.value = ''
  registeredMedia.value = null
  try {
    registeredMedia.value = await api.registerManagedMedia({
      root_key: 'room_root',
      relative_path: mediaPath.value.trim(),
      source_refs: sourceReferencesPayload(),
      source_scope: createScope.value,
      role_use_approved: roleUseApproved.value,
      share_allowed: false,
    })
    managedMediaId.value = registeredMedia.value.id
    const approvedForRole = roleUseApproved.value
    roleUseApproved.value = false
    mediaPath.value = ''
    notice.value = approvedForRole
      ? '媒体已登记并按你的显式选择批准角色使用；它仍不会外发。请另行保存房间条目。'
      : '媒体已登记但未批准角色使用；ID 已填入新建表单，请核对来源后另行保存房间条目。'
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    registeringMedia.value = false
  }
}

async function loadVisionCapability() {
  loadingVision.value = true
  error.value = ''
  notice.value = ''
  try {
    visionCapability.value = await api.visionCapability()
    notice.value = '本地视觉解码能力已读取；没有调用视觉模型或读取媒体内容。'
  } catch (cause) {
    error.value = errorMessage(cause)
    visionCapability.value = null
  } finally {
    loadingVision.value = false
  }
}

async function observeSelectedMedia(item: RoomItem) {
  if (!item.managed_media_id || observingMedia.value) return
  observingMedia.value = true
  error.value = ''
  notice.value = ''
  visionObservation.value = null
  try {
    visionObservation.value = await api.observeRoomMedia({
      room_item_id: item.id,
      expected_room_version: item.version,
      detail_level: 'low',
    })
    notice.value = '静态图片已由本地解码并建立 provenance；视觉模型仍未核验，没有生成观察文字或调用 DeepSeek。'
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    observingMedia.value = false
  }
}

function contentBytes(encoding: RoomContentRead['encoding'] | RoomExportResult['content']['encoding'], value: string) {
  if (encoding === 'utf-8') return new TextEncoder().encode(value)
  const binary = atob(value)
  const bytes = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index)
  return bytes
}

function saveBlob(bytes: Uint8Array, filename: string, mediaType: string) {
  const buffer = new ArrayBuffer(bytes.byteLength)
  new Uint8Array(buffer).set(bytes)
  const blob = new Blob([buffer], { type: mediaType || 'application/octet-stream' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

function downloadReadContent() {
  const content = roomContent.value
  const item = selected.value
  if (!content || !item) return
  try {
    const bytes = contentBytes(content.encoding, content.data)
    saveBlob(bytes, `${item.id}${content.encoding === 'utf-8' ? '.txt' : '.bin'}`, content.media_type)
  } catch {
    error.value = '正文数据无法解码，已取消下载。'
  }
}

async function createItem() {
  if (!writeAllowed.value || actionId.value || !sourceRefsValid.value || (Boolean(createContent.value.trim()) === Boolean(managedMediaId.value.trim()))) return
  actionId.value = 'create'
  error.value = ''
  notice.value = ''
  try {
    const payload: RoomItemCreate = {
      kind: createKind.value,
      title: createTitle.value.trim(),
      relative_path: createPath.value.trim() || undefined,
      content_text: createContent.value.trim() || undefined,
      managed_media_id: managedMediaId.value.trim() || undefined,
      source_refs: sourceReferencesPayload(),
      source_scope: createScope.value,
      tags: splitLines(createTags.value),
      share_allowed: createShareAllowed.value,
    }
    const item = await api.createRoomItem(payload)
    selectedId.value = item.id
    createTitle.value = ''
    createPath.value = ''
    createContent.value = ''
    managedMediaId.value = ''
    createSources.value = [{ kind: 'owner_supplied', id: '', person_id: '', scope: '' }]
    createTags.value = ''
    createShareAllowed.value = false
    notice.value = '已保存一条房间索引记录。share_allowed 只记录本地意向，不授权外发。'
    await loadList()
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    actionId.value = ''
  }
}

async function saveItem() {
  const item = selected.value
  if (!writeAllowed.value || !item || actionId.value) return
  actionId.value = item.id
  error.value = ''
  notice.value = ''
  try {
    const updated = await api.updateRoomItem(item.id, {
      expected_version: item.version,
      title: editTitle.value.trim() || undefined,
      new_relative_path: editPath.value.trim() || undefined,
      content_text: editContent.value.trim() || undefined,
      tags: splitLines(editTags.value),
      share_allowed: editShareAllowed.value,
    })
    selected.value = updated
    notice.value = '索引记录已更新为新版本。'
    await loadList()
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    actionId.value = ''
  }
}

async function itemAction(item: RoomItem, action: 'soft-delete' | 'restore' | 'export') {
  if (!writeAllowed.value) return
  actionId.value = item.id + ':' + action
  error.value = ''
  notice.value = ''
  exportResult.value = null
  try {
    if (action === 'export') {
      exportResult.value = await api.exportRoomItem(item.id, { expected_version: item.version })
      notice.value = '单项导出数据与 manifest 已准备好；检查内容后点击下载。'
    } else {
      await api.roomItemAction(item.id, action, { expected_version: item.version })
      notice.value = action === 'soft-delete' ? '项目已移入回收站，可恢复。' : '项目已恢复。'
      await loadList()
    }
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    actionId.value = ''
  }
}

function downloadExport() {
  const result = exportResult.value
  if (!result) return
  try {
    const bytes = contentBytes(result.content.encoding, result.content.data)
    if (bytes.byteLength !== result.manifest.byte_length) {
      error.value = '导出内容长度与 manifest 不一致，已取消下载。'
      return
    }
    saveBlob(bytes, result.manifest.filename, result.content.media_type)
  } catch {
    error.value = '导出内容无法解码，已取消下载。'
  }
}

onMounted(() => void refresh())
</script>

<template>
  <div class="page-stack">
    <header class="page-heading">
      <div><p class="eyebrow">本机房间索引</p><h1>小鱼的房间</h1><p class="page-heading__description">列表来自数据库索引，不遍历目录。任何文件读写都需同时通过 room_root 映射与 role_workspace 授权。</p></div>
      <div class="page-heading__actions"><NTag round type="info">{{ roots }}</NTag><StatusPill :status="status?.authorization_state" /><NButton quaternary :loading="loading" @click="refresh">刷新</NButton></div>
    </header>

    <NAlert v-if="error" type="warning" :show-icon="true" title="房间请求未完成">{{ error }}</NAlert>
    <NAlert v-if="status?.authorization_state !== 'granted'" type="warning" :show-icon="true" title="房间操作待授权">
      当前授权状态：{{ status?.authorization_state || '未知' }}。本机目录映射不会自动授予角色工作区权限；本页不扫描文件，也不会写入 I 盘。
    </NAlert>
    <NAlert v-else-if="status?.disk_state !== 'available'" type="info" :show-icon="true" title="磁盘状态来自缓存">本页不会主动探测磁盘。操作由已配置的 room_root、显式业务授权及本机路径校验决定；不会切换到其他目录。</NAlert>
    <NAlert v-else type="info" :show-icon="true" title="分享标记边界">`share_allowed` 只是本地条目标志，不是发送许可或账号授权。</NAlert>
    <NAlert v-if="notice" type="success" :show-icon="true">{{ notice }}</NAlert>

    <section class="room-status-grid">
      <NCard class="panel-card" :bordered="false"><div class="room-status"><span>根目录键</span><strong>{{ status?.root_key || 'room_root' }}</strong><small>路径来自本机 host 映射，不由页面拼接。</small></div></NCard>
      <NCard class="panel-card" :bordered="false"><div class="room-status"><span>磁盘状态</span><StatusPill :status="status?.disk_state" /><small>只读状态接口，不扫描目录。</small></div></NCard>
      <NCard class="panel-card" :bordered="false"><div class="room-status"><span>业务授权</span><StatusPill :status="status?.authorization_state" /><small>revision {{ status?.revision ?? '未知' }}</small></div></NCard>
    </section>

    <section class="room-layout">
      <NCard class="panel-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>房间条目</h2><p>来自数据库的 {{ total }} 条索引记录。</p></div><AppIcon name="book" /></div></template>
        <div class="room-filters"><NSelect v-model:value="kindFilter" :options="kindOptions" size="small" /><NSelect v-model:value="stateFilter" :options="stateOptions" size="small" /><NButton size="small" quaternary :loading="loading" @click="loadList">筛选</NButton></div>
        <div v-if="items.length" class="room-list">
          <button v-for="item in items" :key="item.id" class="room-item" :class="{ 'room-item--active': selectedId === item.id }" @click="selectedId = item.id; void loadItem(item.id)">
            <span class="room-item__top"><strong>{{ item.title }}</strong><StatusPill :status="item.state" /></span>
            <small>{{ item.kind }} · v{{ item.version }} · {{ item.id }}</small>
            <small>{{ item.relative_path || '没有登记相对路径' }}</small>
          </button>
        </div>
        <NEmpty v-else-if="!loading" description="房间数据库索引中没有符合条件的条目" />
        <div v-else class="inline-loading">正在读取房间索引…</div>
      </NCard>

      <NCard class="panel-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>{{ selected?.title || '新建一条房间内容' }}</h2><p>{{ selected ? `${selected.kind} · ${selected.id} · v${selected.version}` : '只保存到当前 room_root 的索引空间' }}</p></div><AppIcon name="server" /></div></template>
        <details v-if="!selected" class="room-create" :open="true">
          <summary>新建房间条目</summary>
          <div class="room-form">
            <label class="field-label" for="room-kind">条目类型</label><NSelect id="room-kind" v-model:value="createKind" :options="itemKinds" :disabled="!writeAllowed" />
            <label class="field-label" for="room-title">标题</label><NInput id="room-title" v-model:value="createTitle" maxlength="200" :disabled="!writeAllowed" />
            <label class="field-label" for="room-path">相对路径（可选）</label><NInput id="room-path" v-model:value="createPath" placeholder="例如：notes/想法.md；不能填绝对路径" :disabled="!writeAllowed" />
            <label class="field-label" for="room-content">文字内容</label><NInput id="room-content" v-model:value="createContent" type="textarea" :autosize="{ minRows: 3, maxRows: 8 }" maxlength="12000" :disabled="!writeAllowed || Boolean(managedMediaId.trim())" />
            <label class="field-label" for="room-media">已登记的媒体 ID（可选）</label><NInput id="room-media" v-model:value="managedMediaId" placeholder="填写媒体 ID 时不能同时填文字内容" :disabled="!writeAllowed || Boolean(createContent.trim())" />
            <div class="managed-media-form">
              <strong>登记已有图片或音频</strong>
              <small>手动填写 room_root 内的相对路径。服务端仅登记并校验该单一文件，不扫描目录；角色使用批准默认关闭。</small>
              <label class="field-label" for="room-managed-media-path">已有文件相对路径</label>
              <NInput id="room-managed-media-path" v-model:value="mediaPath" placeholder="例如：media/photo.png" :disabled="!writeAllowed || registeringMedia" />
              <label class="room-checkbox"><input v-model="roleUseApproved" type="checkbox" :disabled="!writeAllowed || registeringMedia"><span>批准角色读取此媒体（外发仍关闭）</span></label>
              <NButton size="small" secondary :disabled="!writeAllowed || !mediaPath.trim() || !sourceRefsValid || Boolean(createContent.trim())" :loading="registeringMedia" @click="registerMedia">登记单个媒体文件</NButton>
              <div v-if="registeredMedia" class="managed-media-result"><span>{{ registeredMedia.id }} · {{ registeredMedia.mime_type }} · {{ registeredMedia.byte_length }} bytes</span><small>SHA-256：{{ registeredMedia.sha256 }} · role_use_approved={{ String(registeredMedia.role_use_approved) }} · share_allowed={{ String(registeredMedia.share_allowed) }}</small></div>
            </div>
            <label class="field-label" for="room-source-scope">来源范围</label><NSelect id="room-source-scope" v-model:value="createScope" :options="sourceScopeOptions" :disabled="!writeAllowed" />
            <span class="field-label">结构化来源引用</span>
            <div class="source-ref-list">
              <div v-for="(source, index) in createSources" :key="index" class="source-ref-row">
                <NSelect :value="source.kind" :options="sourceKindOptions" :disabled="!writeAllowed" aria-label="来源类型" @update:value="(kind) => setSourceReferenceKind(index, kind as SourceReferenceKind)" />
                <NInput v-model:value="source.id" placeholder="来源 ID（必填）" :disabled="!writeAllowed" aria-label="来源 ID" />
                <NInput v-model:value="source.person_id" placeholder="person_id（可选）" :disabled="!writeAllowed || source.kind === 'owner_supplied'" aria-label="来源 person_id" />
                <NSelect v-model:value="source.scope" :options="optionalSourceScopeOptions" :disabled="!writeAllowed" aria-label="来源范围" />
                <NButton size="small" quaternary :disabled="!writeAllowed" @click="removeSourceReference(index)">移除</NButton>
              </div>
              <NButton size="small" quaternary :disabled="!writeAllowed" @click="addSourceReference">添加来源引用</NButton>
              <small>来源类型、ID、person_id 和 scope 会按结构化 DTO 提交；person_id 可选，owner_supplied 不能声明 person_id。单项 scope 留空时跟随条目范围。</small>
            </div>
            <label class="field-label" for="room-tags">标签（逗号分隔）</label><NInput id="room-tags" v-model:value="createTags" :disabled="!writeAllowed" />
            <label class="room-checkbox"><input v-model="createShareAllowed" type="checkbox" :disabled="!writeAllowed"><span>本地标为可分享（不授权外发）</span></label>
            <NButton type="primary" :disabled="!writeAllowed || !createTitle.trim() || !sourceRefsValid || Boolean(createContent.trim()) === Boolean(managedMediaId.trim())" :loading="actionId === 'create'" @click="createItem">保存条目</NButton>
          </div>
        </details>

        <div v-if="selected" class="room-detail">
          <NTag round size="small">{{ selected.source_scope }} · 用途范围</NTag>
          <div class="room-detail__meta"><div><span>路径</span><strong>{{ selected.relative_path || '未登记' }}</strong></div><div><span>来源</span><strong>{{ selected.source_refs.map(formatSourceReference).join('；') || '未提供' }}</strong></div><div><span>内容 hash</span><strong>{{ selected.content_hash || '未提供' }}</strong></div><div><span>标签</span><strong>{{ selected.tags.join(' · ') || '无' }}</strong></div><div><span>创建</span><strong>{{ formatTime(selected.created_at) }}</strong></div><div><span>更新</span><strong>{{ formatTime(selected.updated_at) }}</strong></div></div>
          <section v-if="selected.managed_media_id" class="vision-panel" aria-label="本地图片解码">
            <div class="vision-panel__heading"><strong>本地静态图片解码</strong><NButton size="small" quaternary :loading="loadingVision" @click="loadVisionCapability">读取能力状态</NButton></div>
            <div class="vision-panel__meta"><span>托管媒体 ID</span><strong>{{ selected.managed_media_id }}</strong><span>能力状态</span><StatusPill :status="visionCapability?.state || 'not_checked'" /></div>
            <div v-if="visionCapability" class="vision-panel__meta"><span>视觉模型</span><StatusPill :status="visionCapability.visual_model_status" /><span>计价状态</span><StatusPill :status="visionCapability.pricing_status" /><span>格式 / 上限</span><strong>{{ visionCapability.supported_mime_types.join('、') }} · {{ (visionCapability.max_bytes / 1024 / 1024).toFixed(0) }} MiB · {{ (visionCapability.max_pixels / 1000000).toFixed(0) }} MP</strong></div>
            <small>仅 PNG/JPEG；此操作在你点击后读取一个已登记条目并本地解码，不调用视觉模型或 DeepSeek。</small>
            <NButton size="small" secondary :disabled="!visionCapability?.decoder_available || observingMedia" :loading="observingMedia" @click="observeSelectedMedia(selected)">解码这张图片并记录来源</NButton>
            <NAlert v-if="visionCapability && visionCapability.state === 'needs_model_verification'" type="warning" :show-icon="true">目前只有本机静态图片解码和来源登记。P07 仍缺视觉模型 image request/response adapter 代码，且视觉模型与价格尚未核验；不会生成图像理解结果或调用模型。</NAlert>
            <div v-if="visionObservation" class="vision-result">
              <strong>{{ visionObservation.state }} · observation={{ visionObservation.observation === null ? 'null（未调用模型）' : '已返回' }}</strong>
              <span>{{ visionObservation.media.mime_type }} · {{ visionObservation.media.width }} × {{ visionObservation.media.height }} · {{ visionObservation.media.pixel_count }} pixels</span>
              <span>SHA-256：{{ visionObservation.media.sha256 }} · pixel digest：{{ visionObservation.media.pixel_digest }}</span>
              <span>来源：{{ visionObservation.source_refs.map(formatSourceReference).join('；') || '未提供' }}</span>
            </div>
          </section>
          <div v-if="editing" class="room-form room-edit">
            <label class="field-label" for="room-edit-title">标题</label><NInput id="room-edit-title" v-model:value="editTitle" maxlength="200" :disabled="!writeAllowed" />
            <label class="field-label" for="room-edit-path">新相对路径（同一授权 root 内改名）</label><NInput id="room-edit-path" v-model:value="editPath" :disabled="!writeAllowed" />
            <label class="field-label" for="room-edit-content">文字内容</label><NInput id="room-edit-content" v-model:value="editContent" type="textarea" :autosize="{ minRows: 3, maxRows: 8 }" maxlength="12000" placeholder="先用“读取正文”载入后编辑；未读取时留空表示不改正文" :disabled="!writeAllowed" />
            <label class="field-label" for="room-edit-tags">标签</label><NInput id="room-edit-tags" v-model:value="editTags" :disabled="!writeAllowed" />
            <label class="room-checkbox"><input v-model="editShareAllowed" type="checkbox" :disabled="!writeAllowed"><span>本地标为可分享（不授权外发）</span></label>
          </div>
          <div class="room-actions">
            <NButton size="small" secondary :disabled="!writeAllowed" :loading="readingContent" @click="readContent(selected)">读取正文</NButton>
            <NButton v-if="!editing && selected.state !== 'trashed'" size="small" secondary :disabled="!writeAllowed" @click="editing = true">编辑 / 改名</NButton>
            <NButton v-if="editing" size="small" type="primary" :disabled="!writeAllowed" :loading="actionId === selected.id" @click="saveItem">保存新版本</NButton>
            <NButton v-if="editing" size="small" quaternary @click="syncEditForm(selected)">取消编辑</NButton>
            <NButton size="small" quaternary :disabled="!writeAllowed" :loading="actionId === `${selected.id}:export`" @click="itemAction(selected, 'export')">导出此单项</NButton>
            <NPopconfirm v-if="selected.state !== 'trashed'" @positive-click="itemAction(selected, 'soft-delete')">
              <template #trigger><NButton size="small" type="error" quaternary :disabled="!writeAllowed" :loading="actionId === `${selected.id}:soft-delete`">移入回收站</NButton></template>
              这是可恢复的软删除，不会永久删除文件。确认继续？
            </NPopconfirm>
            <NButton v-else size="small" secondary :disabled="!writeAllowed" :loading="actionId === `${selected.id}:restore`" @click="itemAction(selected, 'restore')">恢复</NButton>
          </div>
          <div v-if="roomContent" class="room-content">
            <div class="room-content__header"><span>通过显式读取接口载入的正文 · {{ roomContent.media_type }} · {{ roomContent.encoding }}</span><NButton v-if="roomContent.encoding === 'base64'" size="small" quaternary @click="downloadReadContent">下载媒体副本</NButton></div>
            <pre v-if="roomContent.encoding === 'utf-8'">{{ roomContent.data }}</pre>
            <small v-else>二进制媒体已读取并按 base64 处理；页面不会将其作为文字显示。SHA-256：{{ roomContent.content_hash || '未提供' }}</small>
          </div>
          <section v-if="exportResult" class="room-export" aria-label="单项导出预览">
            <div class="room-export__header"><strong>单项导出预览</strong><NButton size="small" type="primary" @click="downloadExport">下载文件</NButton></div>
            <dl class="room-export__manifest">
              <div><dt>文件</dt><dd>{{ exportResult.manifest.filename }}</dd></div>
              <div><dt>来源范围</dt><dd>{{ exportResult.manifest.source_scope }}（用途标记）</dd></div>
              <div><dt>大小</dt><dd>{{ exportResult.manifest.byte_length }} bytes</dd></div>
              <div><dt>SHA-256</dt><dd>{{ exportResult.manifest.sha256 }}</dd></div>
              <div><dt>媒体类型</dt><dd>{{ exportResult.content.media_type }}</dd></div>
              <div><dt>编码</dt><dd>{{ exportResult.content.encoding }}</dd></div>
              <div><dt>导出时间</dt><dd>{{ formatTime(exportResult.manifest.exported_at) }}</dd></div>
            </dl>
            <small>仅此条目；下载由你明确点击触发。默认上限 8 MiB。</small>
          </section>
        </div>
        <NEmpty v-else description="选择一个索引条目查看详情；也可以在已有授权时新建内容" />
      </NCard>
    </section>
  </div>
</template>

<style scoped>
.room-status-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.room-status { display: grid; gap: 6px; }
.room-status > span { color: var(--text-muted); font-size: 11px; font-weight: 700; }
.room-status strong { color: var(--text); font-size: 15px; }
.room-status small { color: var(--text-muted); font-size: 10px; }
.room-layout { display: grid; min-width: 0; grid-template-columns: minmax(280px, 0.8fr) minmax(0, 1.2fr); gap: 15px; }
.room-filters { display: grid; grid-template-columns: 1fr 1fr auto; gap: 7px; margin-bottom: 10px; }
.room-list { display: grid; max-height: 66vh; gap: 8px; overflow-y: auto; }
.room-item { display: grid; gap: 5px; width: 100%; padding: 11px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface); color: var(--text); text-align: left; cursor: pointer; }
.room-item--active { border-color: var(--primary); background: var(--primary-soft); }
.room-item__top { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
.room-item__top strong { overflow-wrap: anywhere; font-size: 12px; }
.room-item small { overflow-wrap: anywhere; color: var(--text-muted); font-size: 10px; }
.room-create { padding: 13px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-soft); }
.room-create summary { color: var(--primary); font-size: 12px; font-weight: 700; cursor: pointer; }
.room-form { display: grid; gap: 8px; margin-top: 12px; }
.managed-media-form, .vision-panel { display: grid; gap: 8px; padding: 12px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-soft); }
.managed-media-form > strong, .vision-panel__heading strong, .vision-result strong { color: var(--text); font-size: 11px; }
.managed-media-form > small, .vision-panel > small, .managed-media-result small, .vision-result span { color: var(--text-muted); font-size: 10px; line-height: 1.5; overflow-wrap: anywhere; }
.managed-media-result, .vision-result { display: grid; gap: 4px; overflow-wrap: anywhere; color: var(--text-soft); font-size: 10px; }
.vision-panel__heading { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; }
.vision-panel__meta { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 6px 10px; align-items: center; color: var(--text-muted); font-size: 10px; }
.vision-panel__meta strong { overflow-wrap: anywhere; color: var(--text-soft); }
.source-ref-list { display: grid; gap: 8px; }
.source-ref-list > small { color: var(--text-muted); font-size: 10px; }
.source-ref-row { display: grid; grid-template-columns: minmax(130px, 0.8fr) minmax(130px, 1fr) minmax(130px, 1fr) minmax(130px, 0.8fr) auto; gap: 6px; align-items: center; }
.room-checkbox { display: flex; align-items: center; gap: 8px; color: var(--text-muted); font-size: 11px; }
.room-detail__meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 210px), 1fr)); gap: 9px 14px; margin: 13px 0; }
.room-detail__meta > div { display: grid; min-width: 0; gap: 3px; }
.room-detail__meta span, .room-content > span { color: var(--text-muted); font-size: 10px; }
.room-detail__meta strong { overflow-wrap: anywhere; color: var(--text-soft); font-size: 11px; }
.room-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 7px; margin-top: 14px; }
.room-content, .room-export { margin-top: 14px; padding: 12px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-soft); }
.room-content pre { max-height: 300px; overflow: auto; color: var(--text-soft); font-size: 11px; white-space: pre-wrap; overflow-wrap: anywhere; }
.room-content__header { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; color: var(--text-muted); font-size: 10px; }
.room-content > small { color: var(--text-muted); font-size: 10px; overflow-wrap: anywhere; }
.room-export__header { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; color: var(--text); font-size: 12px; font-weight: 700; }
.room-export__manifest { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 210px), 1fr)); gap: 9px 14px; margin: 12px 0; }
.room-export__manifest > div { display: grid; min-width: 0; gap: 3px; }
.room-export__manifest dt { color: var(--text-muted); font-size: 10px; }
.room-export__manifest dd { margin: 0; overflow-wrap: anywhere; color: var(--text-soft); font-size: 11px; }
.room-export > small { color: var(--text-muted); font-size: 10px; }
.room-edit { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--border); }
@media (max-width: 850px) { .room-status-grid { grid-template-columns: 1fr; } .room-layout { grid-template-columns: 1fr; } .room-list { max-height: 34vh; } }
@media (max-width: 1050px) { .source-ref-row { grid-template-columns: 1fr 1fr; } }
@media (max-width: 560px) { .room-filters { grid-template-columns: 1fr; } }
</style>
