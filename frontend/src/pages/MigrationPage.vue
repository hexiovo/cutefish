<script setup lang="ts">
import { computed, ref } from 'vue'
import { NAlert, NButton, NCard, NInput, NPopconfirm, NTag } from 'naive-ui'
import { api } from '../api/client'
import { errorMessage } from '../api/errors'
import type { MigrationExportResult, MigrationImportResult, MigrationPreflight, MigrationStatus, MigrationValidationResult } from '../api/types'
import AppIcon from '../components/AppIcon.vue'
import StatusPill from '../components/StatusPill.vue'

const status = ref<MigrationStatus | null>(null)
const preflight = ref<MigrationPreflight | null>(null)
const exported = ref<MigrationExportResult | null>(null)
const validation = ref<MigrationValidationResult | null>(null)
const imported = ref<MigrationImportResult | null>(null)
const destinationRelativePath = ref('')
const mediaIdsText = ref('')
const bundleRelativePath = ref('')
const targetRelativePath = ref('')
const loadingStatus = ref(false)
const inspecting = ref(false)
const exporting = ref(false)
const validating = ref(false)
const importing = ref(false)
const error = ref('')
const notice = ref('')
const validatedPath = ref('')

const restorePolicy = computed(() => status.value?.restore_policy ?? imported.value?.restore_policy ?? null)
const localSourceProfile = computed(() => status.value?.deployment_profile === 'local_trial' && status.value?.target_staging === false)
const rootSummary = computed(() => (status.value?.logical_roots ?? []).map((root) => ({
  ...root,
  label: root.root_key,
})))
const canExport = computed(() => Boolean(
  localSourceProfile.value
  && preflight.value?.available
  && preflight.value.schema_supported
  && destinationRelativePath.value.trim()
  && !exporting.value,
))
const canValidate = computed(() => Boolean(bundleRelativePath.value.trim()) && !validating.value)
const canImport = computed(() => Boolean(
  validation.value?.valid
  && validatedPath.value === bundleRelativePath.value.trim()
  && targetRelativePath.value.trim()
  && !importing.value,
))

function lines(value: string) {
  return value.split(/[\r\n,，]+/).map((line) => line.trim()).filter(Boolean)
}

function clearValidation() {
  validation.value = null
  validatedPath.value = ''
  imported.value = null
}

async function loadStatus() {
  loadingStatus.value = true
  error.value = ''
  notice.value = ''
  try {
    status.value = await api.migrationStatus()
    notice.value = '迁移保护状态已读取；没有检查数据库或访问任何文件。'
  } catch (cause) {
    error.value = errorMessage(cause)
    status.value = null
  } finally {
    loadingStatus.value = false
  }
}

async function inspectSource() {
  inspecting.value = true
  error.value = ''
  notice.value = ''
  preflight.value = null
  try {
    preflight.value = await api.inspectMigrationSource()
    notice.value = '源数据预检已完成；没有生成包或修改源数据库。'
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    inspecting.value = false
  }
}

async function exportBundle() {
  if (!canExport.value) return
  exporting.value = true
  error.value = ''
  notice.value = ''
  exported.value = null
  clearValidation()
  try {
    exported.value = await api.exportMigrationBundle({
      destination_relative_path: destinationRelativePath.value.trim(),
      media_ids: lines(mediaIdsText.value),
    })
    bundleRelativePath.value = exported.value.bundle_relative_path
    notice.value = '隔离迁移包已生成，包含 manifest/hash 且已脱敏；导入前仍须由你显式执行校验。'
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    exporting.value = false
  }
}

async function validateBundle() {
  if (!canValidate.value) return
  validating.value = true
  error.value = ''
  notice.value = ''
  clearValidation()
  try {
    validation.value = await api.validateMigrationBundle({ bundle_relative_path: bundleRelativePath.value.trim() })
    validatedPath.value = validation.value.valid ? bundleRelativePath.value.trim() : ''
    notice.value = validation.value.valid ? '迁移包校验通过。只有该路径的最新校验结果有效时才可导入 staging。' : '迁移包校验未通过；不能导入。'
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    validating.value = false
  }
}

async function importStaging() {
  if (!canImport.value) return
  importing.value = true
  error.value = ''
  notice.value = ''
  try {
    imported.value = await api.importMigrationStaging({
      bundle_relative_path: bundleRelativePath.value.trim(),
      target_relative_path: targetRelativePath.value.trim(),
    })
    notice.value = '已导入隔离的 target_staging；外部动作、Outbox、付费调用、调度、自启与任务自动恢复均保持关闭。需要按提示重启。'
    if (status.value) {
      status.value = {
        ...status.value,
        deployment_profile: imported.value.deployment_profile,
        target_staging: true,
        restore_policy: imported.value.restore_policy,
      }
    }
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    importing.value = false
  }
}
</script>

<template>
  <div class="page-stack">
    <header class="page-heading">
      <div><p class="eyebrow">本机隔离迁移</p><h1>快照与目标 staging</h1><p class="page-heading__description">来源只会复制和脱敏；导入始终写到新的 target_staging 目录，不会启用外发或自动任务。</p></div>
      <div class="page-heading__actions"><NTag round type="info">目标部署保持停用</NTag><NButton quaternary :loading="loadingStatus" @click="loadStatus">读取保护状态</NButton></div>
    </header>

    <NAlert v-if="error" type="error" :show-icon="true" title="迁移操作未完成">{{ error }}</NAlert>
    <NAlert v-if="notice" type="success" :show-icon="true">{{ notice }}</NAlert>
    <NAlert type="warning" :show-icon="true" title="边界">
      页面不会自动扫描目录。路径只接受逻辑根目录内的相对路径；不会覆盖非空目标，不会导入任意源文件，也不会对目标主机启用服务。
    </NAlert>

    <section class="migration-grid">
      <NCard class="panel-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>部署保护状态</h2><p>只读取本机服务当前返回的状态。</p></div><AppIcon name="shield" /></div></template>
        <div v-if="status" class="migration-panel">
          <div class="migration-status-row"><span>Profile</span><StatusPill :status="status.deployment_profile" /><strong>{{ status.target_staging ? '目标暂存模式' : '本机开发/试用模式' }}</strong></div>
          <div class="root-list">
            <div v-for="root in rootSummary" :key="root.root_key" class="root-row"><div><strong>{{ root.label }}</strong><small>{{ root.configured ? root.available === false ? '已配置，当前不可用' : root.available === true ? '已配置且可用' : '已配置，可用性未知' : '尚未配置' }}</small></div><StatusPill :status="root.available === true ? 'ready' : root.configured ? 'unknown' : 'needs_configuration'" kind="runtime" /></div>
          </div>
          <div v-if="restorePolicy" class="restore-policy">
            <strong>恢复后保持关闭</strong>
            <div><span>外部动作</span><StatusPill :status="restorePolicy.external_actions_enabled ? 'enabled' : 'disabled'" /></div>
            <div><span>Outbox 派发</span><StatusPill :status="restorePolicy.outbox_dispatch_enabled ? 'enabled' : 'disabled'" /></div>
            <div><span>付费模型调用</span><StatusPill :status="restorePolicy.paid_provider_calls_enabled ? 'enabled' : 'disabled'" /></div>
            <div><span>调度</span><StatusPill :status="restorePolicy.scheduling_enabled ? 'enabled' : 'disabled'" /></div>
            <div><span>自启</span><StatusPill :status="restorePolicy.auto_start_enabled ? 'enabled' : 'disabled'" /></div>
            <div><span>任务自动恢复</span><StatusPill :status="restorePolicy.task_auto_resume ? 'enabled' : 'disabled'" /></div>
          </div>
        </div>
        <NEmpty v-else description="尚未手动读取部署保护状态" />
      </NCard>

      <NCard class="panel-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>源数据预检</h2><p>检查 schema 与待处理状态；不会导出。</p></div><AppIcon name="info" /></div></template>
        <div class="migration-panel">
          <NButton secondary :loading="inspecting" @click="inspectSource">检查本机源数据</NButton>
          <template v-if="preflight">
            <div class="migration-status-row"><span>源数据库</span><StatusPill :status="preflight.available ? preflight.schema_supported ? 'ready' : 'needs_verification' : 'not_connected'" /><strong>schema {{ preflight.schema_version ?? '未知' }}</strong></div>
            <dl class="migration-stats">
              <div><dt>表数量</dt><dd>{{ preflight.table_count }}</dd></div><div><dt>媒体登记</dt><dd>{{ preflight.registered_media_count }}</dd></div><div><dt>活动批次</dt><dd>{{ preflight.active_batch_count }}</dd></div><div><dt>待处理 Outbox</dt><dd>{{ preflight.pending_outbox_count }}</dd></div><div><dt>发送中 Outbox</dt><dd>{{ preflight.sending_outbox_count }}</dd></div>
            </dl>
            <ul v-if="preflight.errors.length" class="migration-messages migration-messages--error"><li v-for="item in preflight.errors" :key="item">{{ item }}</li></ul>
            <ul v-if="preflight.warnings.length" class="migration-messages"><li v-for="item in preflight.warnings" :key="item">{{ item }}</li></ul>
          </template>
          <NEmpty v-else description="预检结果尚未由你请求" />
        </div>
      </NCard>

      <NCard class="panel-card">
        <template #header><div class="card-heading"><div><h2>生成脱敏迁移包</h2><p>输出固定写入 backup_root 下的相对路径。</p></div><AppIcon name="outbox" /></div></template>
        <div class="migration-panel">
          <label class="field-label" for="migration-export-path">备份根目录下的相对路径</label>
          <NInput id="migration-export-path" v-model:value="destinationRelativePath" placeholder="例如：exports/cutefish-snapshot" :disabled="exporting" />
          <label class="field-label" for="migration-media-ids">需纳入的托管媒体 ID（可选，每行一个）</label>
          <NInput id="migration-media-ids" v-model:value="mediaIdsText" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" :disabled="exporting" />
          <NButton type="primary" :loading="exporting" :disabled="!canExport" @click="exportBundle">生成迁移包</NButton>
          <p v-if="!status" class="form-hint">先读取部署保护状态和源数据预检；导出只在 local_trial 且当前 schema 受支持时开放。</p>
          <p v-else-if="!localSourceProfile" class="form-hint">当前 profile 不是 local_trial；不会从 target_staging 导出迁移源。</p>
          <p v-else-if="!preflight?.schema_supported" class="form-hint">请先执行源数据预检，且 schema 必须受当前版本支持，才可导出。</p>
          <div v-if="exported" class="export-result">
            <StatusPill :status="exported.sanitized ? 'ready' : 'needs_verification'" />
            <div><span>位置</span><strong>{{ exported.bundle_root_key }}/{{ exported.bundle_relative_path }}</strong></div>
            <div><span>Export ID</span><strong>{{ exported.export_id }}</strong></div>
            <div><span>数据库 SHA-256</span><strong>{{ exported.database_sha256 }}</strong></div>
            <div><span>Manifest SHA-256</span><strong>{{ exported.manifest_sha256 }}</strong></div>
            <div><span>媒体项目</span><strong>{{ exported.media_count }}</strong></div>
          </div>
        </div>
      </NCard>

      <NCard class="panel-card">
        <template #header><div class="card-heading"><div><h2>校验并导入 staging</h2><p>必须先对同一路径成功校验，目标必须是空的新目录。</p></div><AppIcon name="server" /></div></template>
        <div class="migration-panel">
          <label class="field-label" for="migration-bundle-path">backup_root 下的 bundle 相对路径</label>
          <NInput id="migration-bundle-path" v-model:value="bundleRelativePath" placeholder="粘贴 bundle 相对路径" :disabled="validating || importing" @update:value="clearValidation" />
          <NButton secondary :loading="validating" :disabled="!canValidate" @click="validateBundle">校验迁移包</NButton>
          <template v-if="validation">
            <div class="migration-status-row"><span>校验</span><StatusPill :status="validation.valid ? 'ready' : 'failed'" /><strong>{{ validation.export_id || '没有可用导出 ID' }}</strong></div>
            <div v-if="validation.database_sha256" class="hash-value">数据库 SHA-256：{{ validation.database_sha256 }} · {{ validation.media_count }} 项媒体</div>
            <ul v-if="validation.errors.length" class="migration-messages migration-messages--error"><li v-for="item in validation.errors" :key="item">{{ item }}</li></ul>
            <ul v-if="validation.warnings.length" class="migration-messages"><li v-for="item in validation.warnings" :key="item">{{ item }}</li></ul>
          </template>
          <label class="field-label" for="migration-target-path">data_root 下的新空目标相对路径</label>
          <NInput id="migration-target-path" v-model:value="targetRelativePath" placeholder="例如：target_staging/migration-01" :disabled="importing" />
          <NPopconfirm :disabled="!canImport" @positive-click="importStaging">
            <template #trigger><NButton type="primary" :disabled="!canImport" :loading="importing">导入至 target_staging</NButton></template>
            只导入已校验的 bundle 到 data_root 下的新空目录，运行 Profile 变为 target_staging，外发与自动行为保持关闭。确认继续？
          </NPopconfirm>
          <NAlert v-if="imported" type="warning" :show-icon="true">导入已完成，需要重启本机服务。当前仍是 target_staging，不会启动外部动作。</NAlert>
        </div>
      </NCard>
    </section>
  </div>
</template>

<style scoped>
.migration-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 430px), 1fr)); gap: 16px; align-items: start; }
.migration-panel { display: grid; gap: 10px; }
.migration-status-row { display: flex; flex-wrap: wrap; align-items: center; gap: 9px; color: var(--text-muted); font-size: 11px; }
.migration-status-row strong { color: var(--text); }
.restore-policy { display: grid; gap: 7px; padding: 11px; border-radius: 12px; background: var(--surface-soft); }
.restore-policy > strong { color: var(--text); font-size: 11px; }
.restore-policy > div { display: flex; align-items: center; justify-content: space-between; gap: 10px; color: var(--text-muted); font-size: 10px; }
.migration-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; margin: 0; }
.migration-stats > div { padding: 10px; border-radius: 10px; background: var(--surface-soft); }
.migration-stats dt { color: var(--text-muted); font-size: 10px; }
.migration-stats dd { margin: 4px 0 0; color: var(--text); font-size: 13px; font-weight: 700; }
.migration-messages { display: grid; gap: 5px; margin: 0; padding: 10px 10px 10px 28px; border-radius: 10px; background: var(--surface-soft); color: var(--text-soft); font-size: 10px; overflow-wrap: anywhere; }
.migration-messages--error { background: var(--danger-soft); color: var(--danger); }
.export-result { display: grid; gap: 8px; padding: 11px; border: 1px solid var(--border); border-radius: 11px; background: var(--surface-soft); }
.export-result > div { display: grid; gap: 3px; }
.export-result span { color: var(--text-muted); font-size: 10px; }
.export-result strong, .hash-value { overflow-wrap: anywhere; color: var(--text-soft); font-size: 10px; }
</style>
