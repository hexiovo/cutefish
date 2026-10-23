<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { NAlert, NButton, NCard, NEmpty, NInput, NPopconfirm, NSelect, NTag } from 'naive-ui'
import { api, ApiRequestError } from '../api/client'
import { errorMessage } from '../api/errors'
import type { IntegrationActionPreview, IntegrationCapability, IntegrationSourceReadResult } from '../api/types'
import AppIcon from '../components/AppIcon.vue'
import StatusPill from '../components/StatusPill.vue'

const capabilities = ref<IntegrationCapability[]>([])
const loading = ref(false)
const reading = ref(false)
const previewing = ref(false)
const previewUncertain = ref(false)
const error = ref('')
const notice = ref('')
const sourceRead = ref<IntegrationSourceReadResult | null>(null)
const actionPreview = ref<IntegrationActionPreview | null>(null)
const selectedReadId = ref('')
const selectedActionId = ref('')
const objectId = ref('')
const queryJson = ref('{}')
const accountRef = ref('')
const payloadJson = ref('{}')

const readCapabilities = computed(() => capabilities.value.filter((item) => item.read_registered === true && item.target_configured === true))
const actionCapabilities = computed(() => capabilities.value.filter((item) =>
  item.status === 'ready_local' && item.handler_registered && item.target_configured,
))
const selectedReadCapability = computed(() => readCapabilities.value.find((item) => item.capability_id === selectedReadId.value) ?? null)
const selectedActionCapability = computed(() => actionCapabilities.value.find((item) => item.capability_id === selectedActionId.value) ?? null)
const readOptions = computed(() => readCapabilities.value.map((item) => ({ label: `${item.name} · ${item.site_id}`, value: item.capability_id })))
const actionOptions = computed(() => actionCapabilities.value.map((item) => ({ label: `${item.name} · ${item.site_id}`, value: item.capability_id })))
const readBindingMatches = computed(() => Boolean(
  sourceRead.value
  && selectedActionCapability.value
  && sourceRead.value.evidence.site_id === selectedActionCapability.value.site_id
  && sourceRead.value.evidence.object_id === objectId.value.trim()
))
const canRead = computed(() => Boolean(
  selectedReadCapability.value
  && objectId.value.trim()
  && objectId.value.trim().length <= 512
  && !reading.value
))
const canPreview = computed(() => Boolean(
  selectedActionCapability.value
  && selectedActionCapability.value.status === 'ready_local'
  && selectedActionCapability.value.handler_registered
  && selectedActionCapability.value.target_configured
  && sourceRead.value
  && readBindingMatches.value
  && accountRef.value.trim()
  && accountRef.value.trim().length <= 128
  && payloadObject.value
  && !previewing.value
  && !previewUncertain.value
))
const payloadObject = computed<Record<string, unknown> | null>(() => {
  try {
    const value: unknown = JSON.parse(payloadJson.value)
    return value !== null && typeof value === 'object' && !Array.isArray(value)
      ? value as Record<string, unknown>
      : null
  } catch {
    return null
  }
})
const readContentPreview = computed(() => {
  const result = sourceRead.value
  if (!result || !isTextContent(result.evidence.content_type)) return ''
  try {
    const bytes = Uint8Array.from(atob(result.content_base64), (character) => character.charCodeAt(0))
    const text = new TextDecoder().decode(bytes)
    return text.length > 12_000 ? `${text.slice(0, 12_000)}\n…（内容已截断）` : text
  } catch {
    return '响应内容无法以 UTF-8 文本读取。'
  }
})

function isTextContent(contentType: string) {
  return contentType.startsWith('text/') || contentType === 'application/json' || contentType.endsWith('+json') || contentType.endsWith('+xml')
}

function clearActionPreview() {
  actionPreview.value = null
}

async function loadCapabilities() {
  loading.value = true
  error.value = ''
  notice.value = ''
  try {
    capabilities.value = (await api.integrationCapabilities()).items
    if (!readCapabilities.value.some((item) => item.capability_id === selectedReadId.value)) {
      selectedReadId.value = readCapabilities.value[0]?.capability_id ?? ''
    }
    if (!actionCapabilities.value.some((item) => item.capability_id === selectedActionId.value)) {
      selectedActionId.value = actionCapabilities.value[0]?.capability_id ?? ''
    }
    notice.value = `已读取 ${capabilities.value.length} 条本机能力状态；读取和预览均需你逐项触发。`
  } catch (cause) {
    error.value = errorMessage(cause)
    capabilities.value = []
    sourceRead.value = null
    actionPreview.value = null
  } finally {
    loading.value = false
  }
}

async function readSource() {
  const capability = selectedReadCapability.value
  if (!capability || !canRead.value) return
  error.value = ''
  notice.value = ''
  reading.value = true
  sourceRead.value = null
  clearActionPreview()
  try {
    const query: unknown = JSON.parse(queryJson.value)
    if (query === null || typeof query !== 'object' || Array.isArray(query)
      || Object.values(query).some((value) => typeof value !== 'string')) {
      throw new Error('查询参数必须是字符串键值对象，例如 {"page":"1"}。')
    }
    const result = await api.integrationRead({
      site_id: capability.site_id,
      capability_id: capability.capability_id,
      object_id: objectId.value.trim(),
      query: query as Record<string, string>,
    })
    if (result.evidence.site_id !== capability.site_id
      || result.evidence.capability_id !== capability.capability_id
      || result.evidence.object_id !== objectId.value.trim()) {
      throw new Error('服务端来源证据与本次所选站点/对象不匹配，已丢弃响应。')
    }
    sourceRead.value = result
    notice.value = `已按本机注册的只读连接器读取 ${result.evidence.byte_length} bytes；来源证据 ${result.source_ref_id}。`
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    reading.value = false
  }
}

async function createActionPreview() {
  const capability = selectedActionCapability.value
  const source = sourceRead.value
  const payload = payloadObject.value
  if (!capability || !source || !payload || !canPreview.value) return
  error.value = ''
  notice.value = ''
  previewing.value = true
  clearActionPreview()
  try {
    actionPreview.value = await api.integrationPreview({
      site_id: capability.site_id,
      account_ref: accountRef.value.trim(),
      object_id: objectId.value.trim(),
      action: capability.capability_id,
      payload,
      source_ref_ids: [source.source_ref_id],
      scope: {
        kind: 'single_object',
        site_id: capability.site_id,
        object_id: objectId.value.trim(),
        action: capability.capability_id,
      },
    })
    notice.value = '本机已记录单对象 preview_only 提案；没有创建授权、准备 Outbox 项或执行外部动作。'
  } catch (cause) {
    error.value = errorMessage(cause)
    if (cause instanceof ApiRequestError && (!cause.httpStatus || cause.retryable || cause.httpStatus >= 500)) {
      previewUncertain.value = true
      notice.value = '预览记录结果未知；为避免重复创建，当前页面已锁定再次预览。刷新页面后先核对服务端状态。'
    }
  } finally {
    previewing.value = false
  }
}

onMounted(() => void loadCapabilities())
</script>

<template>
  <div class="page-stack">
    <header class="page-heading">
      <div><p class="eyebrow">外部平台边界</p><h1>平台集成</h1><p class="page-heading__description">只对本机实际注册的受限读取连接器与已核实 Handler 显示操作。读取和预览都由你逐项触发。</p></div>
      <div class="page-heading__actions"><NTag round type="info">手动 · local_trial</NTag><NButton quaternary :loading="loading" @click="loadCapabilities">刷新能力状态</NButton></div>
    </header>

    <NAlert v-if="error" type="warning" :show-icon="true" title="集成请求未完成">{{ error }}</NAlert>
    <NAlert v-if="notice" type="success" :show-icon="true">{{ notice }}</NAlert>
    <NAlert type="info" :show-icon="true" title="执行边界">没有实际注册的只读 connector 时，来源读取保持关闭；没有同时标记为本机就绪、目标已配置且 Handler 已注册的动作时，预览保持关闭。此页没有批准、Outbox prepare 或 dispatch 控件。</NAlert>

    <section class="integration-grid">
      <NCard class="panel-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>能力清单</h2><p>{{ capabilities.length }} 条由本机服务声明的能力</p></div><AppIcon name="modules" /></div></template>
        <div v-if="capabilities.length" class="capability-list">
          <article v-for="item in capabilities" :key="`${item.site_id}:${item.capability_id}`" class="capability-row">
            <div class="capability-select">
              <span><strong>{{ item.name }}</strong><small>{{ item.site_id }} · {{ item.capability_id }}</small></span>
              <StatusPill :status="item.status" />
            </div>
            <p>{{ item.reason }}</p>
            <div class="capability-flags"><span>目标配置：<strong>{{ item.target_configured ? '是' : '否' }}</strong></span><span>只读连接器：<strong>{{ item.read_registered ? '已登记' : '未登记' }}</strong></span><span>动作 Handler：<strong>{{ item.handler_registered ? '已登记' : '未登记' }}</strong></span></div>
          </article>
        </div>
        <NEmpty v-else-if="!loading" description="尚未读取本机集成能力" />
        <div v-else class="inline-loading">正在读取本机能力清单…</div>
      </NCard>

      <div class="integration-operations">
        <NCard class="panel-card" :bordered="false">
          <template #header><div class="card-heading"><div><h2>只读来源</h2><p>请求仅交给能力清单确认已注册的 connector</p></div><AppIcon name="book" /></div></template>
          <div v-if="readCapabilities.length" class="integration-form">
            <label class="field-label" for="integration-read-capability">已注册的只读能力</label>
            <NSelect id="integration-read-capability" v-model:value="selectedReadId" :options="readOptions" :disabled="reading" />
            <label class="field-label" for="integration-object-id">单一目标对象 ID</label>
            <NInput id="integration-object-id" v-model:value="objectId" maxlength="512" placeholder="输入来源定义支持的对象 ID" :disabled="reading" @update:value="sourceRead = null; clearActionPreview()" />
            <label class="field-label" for="integration-query">受限查询参数 JSON</label>
            <NInput id="integration-query" v-model:value="queryJson" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" :disabled="reading" @update:value="sourceRead = null; clearActionPreview()" />
            <NButton type="primary" secondary :loading="reading" :disabled="!canRead" @click="readSource">读取此来源</NButton>
            <p class="form-hint">URL、路径、方法和请求头由已注册 connector 管理；查询字段仍会由服务端白名单校验。HTML/JSON 只按文本显示，不执行。</p>
            <div v-if="sourceRead" class="evidence-card">
              <div><span>来源证据</span><strong>{{ sourceRead.source_ref_id }}</strong></div>
              <div><span>精确对象 / scope</span><strong>{{ sourceRead.evidence.object_id }} · {{ sourceRead.evidence.read_scope }}</strong></div>
              <div><span>读取时间 / 长度</span><strong>{{ sourceRead.evidence.fetched_at }} · {{ sourceRead.evidence.byte_length }} bytes</strong></div>
              <div><span>内容类型 / SHA-256</span><strong>{{ sourceRead.evidence.content_type }} · {{ sourceRead.evidence.sha256 }}</strong></div>
              <pre v-if="readContentPreview">{{ readContentPreview }}</pre>
              <small v-else>此响应为非文本类型；页面不渲染或执行媒体内容。</small>
            </div>
          </div>
          <NEmpty v-else description="没有实际注册的只读 connector；不会尝试访问来源" />
        </NCard>

        <NCard class="panel-card" :bordered="false">
          <template #header><div class="card-heading"><div><h2>动作预览</h2><p>只创建本地 preview_only 记录</p></div><AppIcon name="shield" /></div></template>
          <div v-if="actionCapabilities.length" class="integration-form">
            <label class="field-label" for="integration-action-capability">已核实动作 Handler</label>
            <NSelect id="integration-action-capability" v-model:value="selectedActionId" :options="actionOptions" :disabled="previewing" @update:value="clearActionPreview" />
            <label class="field-label" for="integration-account-ref">本机账号别名</label>
            <NInput id="integration-account-ref" v-model:value="accountRef" maxlength="128" placeholder="输入服务配置的非敏感账号别名" :disabled="previewing" @update:value="clearActionPreview" />
            <label class="field-label" for="integration-payload">动作 payload JSON 对象</label>
            <NInput id="integration-payload" v-model:value="payloadJson" type="textarea" :autosize="{ minRows: 3, maxRows: 6 }" :disabled="previewing" @update:value="clearActionPreview" />
            <NAlert v-if="!readBindingMatches" type="warning" :show-icon="true">需先读取与该 Handler 站点、当前对象 ID 完全匹配的来源，才能创建动作预览。</NAlert>
            <NPopconfirm :disabled="!canPreview" @positive-click="createActionPreview">
              <template #trigger><NButton type="warning" secondary :loading="previewing" :disabled="!canPreview">生成单对象动作预览</NButton></template>
              只会按当前本机注册的 Handler 记录 preview_only 提案，不会批准、准备或派发外部动作。确认生成？
            </NPopconfirm>
            <p class="form-hint">payload 不能包含凭据。该流程只保留与来源相同的站点和对象；授权、prepare 与 dispatch 不在此页开放。</p>
            <NAlert v-if="previewUncertain" type="warning" :show-icon="true">预览记录结果未知。此页已锁定重复创建；刷新后先核对服务端记录。</NAlert>
            <div v-if="actionPreview" class="evidence-card">
              <div><span>Preview ID / 状态</span><strong>{{ actionPreview.preview_id }} · {{ actionPreview.state }}</strong></div>
              <div><span>单对象 scope</span><strong>{{ actionPreview.scope.site_id }} · {{ actionPreview.scope.object_id }} · {{ actionPreview.scope.action }}</strong></div>
              <div><span>Payload SHA-256</span><strong>{{ actionPreview.payload_hash }}</strong></div>
              <div><span>Preview SHA-256</span><strong>{{ actionPreview.preview_hash }}</strong></div>
              <pre>{{ actionPreview.payload_json }}</pre>
            </div>
          </div>
          <NEmpty v-else description="没有同时满足本机就绪、目标配置和 Handler 注册的动作；预览保持关闭" />
        </NCard>
      </div>
    </section>
  </div>
</template>

<style scoped>
.integration-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.25fr); gap: 15px; align-items: start; }
.integration-operations, .capability-list, .integration-form { display: grid; gap: 12px; min-width: 0; }
.capability-row { padding: 11px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface); }
.capability-select { display: flex; width: 100%; justify-content: space-between; align-items: flex-start; gap: 9px; padding: 0; color: var(--text); text-align: left; }
.capability-select > span { display: grid; gap: 4px; min-width: 0; }
.capability-select strong { overflow-wrap: anywhere; font-size: 11px; }
.capability-select small, .capability-row p, .capability-flags, .form-hint { color: var(--text-muted); font-size: 10px; line-height: 1.45; overflow-wrap: anywhere; }
.capability-row p { margin: 8px 0; }
.capability-flags { display: flex; flex-wrap: wrap; gap: 6px 12px; }
.capability-flags strong { color: var(--text-soft); }
.field-label { color: var(--text-muted); font-size: 11px; font-weight: 700; }
.form-hint { margin: 0; }
.evidence-card { display: grid; gap: 8px; min-width: 0; padding: 11px; border: 1px solid var(--border); border-radius: 11px; background: var(--surface-soft); }
.evidence-card > div { display: grid; gap: 3px; min-width: 0; }
.evidence-card span, .evidence-card small { color: var(--text-muted); font-size: 10px; }
.evidence-card strong { overflow-wrap: anywhere; color: var(--text-soft); font-size: 10px; }
.evidence-card pre { max-height: 260px; overflow: auto; margin: 0; color: var(--text-soft); font-size: 10px; white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 900px) { .integration-grid { grid-template-columns: 1fr; } }
</style>
