<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { NAlert, NButton, NCard, NEmpty, NGrid, NGridItem, NSpin, NTag } from 'naive-ui'
import AppIcon from '../components/AppIcon.vue'
import StatusPill from '../components/StatusPill.vue'
import { appState } from '../stores/app'

const overview = computed(() => appState.overview.data)
const budgets = computed(() => appState.budgets.data ?? overview.value?.budget_snapshot ?? null)
const services = computed(() => {
  const value = overview.value?.services
  if (!value || typeof value !== 'object') return []
  return Object.entries(value).map(([key, state]) => ({ key, state }))
})

function showAmount(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return '未知'
  return '¥' + value.toFixed(2)
}

function serviceLabel(key: string) {
  const labels: Record<string, string> = { qq: 'QQ桥接', model: '模型服务', database: '数据库', ui: '本地界面' }
  return labels[key] ?? key
}

function serviceStatus(state: unknown) {
  if (!state || typeof state !== 'object') return 'unknown'
  const service = state as { status?: string; connected?: boolean | null; available?: boolean | null }
  if (service.status) return service.status
  if (service.connected === true || service.available === true) return 'ready'
  if (service.connected === false || service.available === false) return 'not_connected'
  return 'unknown'
}

function serviceDetail(state: unknown, key: string) {
  if (!state || typeof state !== 'object') return key
  const detail = (state as { detail?: unknown }).detail
  return typeof detail === 'string' && detail ? detail : key
}

const todaySpent = computed(() => showAmount(budgets.value?.spent))
const pendingCount = computed(() => overview.value?.pending_outbox_count == null ? '未知' : String(overview.value.pending_outbox_count))
const currentVersion = computed(() => overview.value?.app_version || '后端未提供')
</script>

<template>
  <div class="page-stack">
    <header class="page-heading">
      <div>
        <p class="eyebrow">本机状态</p>
        <h1>总览</h1>
        <p class="page-heading__description">状态直接来自本机服务；没有返回的数据会明确标成未知。</p>
      </div>
      <div class="page-heading__actions">
        <NTag round type="info">{{ overview?.mode === 'local_trial' ? '本机小批量试用' : overview?.mode || '模式未提供' }}</NTag>
      </div>
    </header>

    <NAlert v-if="appState.overview.error" type="warning" :show-icon="true" title="总览数据暂不可用">
      {{ appState.overview.error.message }}
    </NAlert>

    <NSpin :show="appState.overview.loading && !overview">
      <NGrid cols="1 s:2 l:4" :x-gap="14" :y-gap="14" responsive="screen">
        <NGridItem>
          <NCard class="metric-card" :bordered="false">
            <span class="metric-card__label">主机标识</span>
            <strong class="metric-card__value metric-card__value--text">{{ overview?.host_id || appState.hostSettings.data?.host_id || '未提供' }}</strong>
            <span class="metric-card__hint">当前设备的本机实例</span>
          </NCard>
        </NGridItem>
        <NGridItem>
          <NCard class="metric-card" :bordered="false">
            <span class="metric-card__label">今日已用</span>
            <strong class="metric-card__value">{{ todaySpent }}</strong>
            <span class="metric-card__hint">预算未知部分仍单独保留</span>
          </NCard>
        </NGridItem>
        <NGridItem>
          <NCard class="metric-card" :bordered="false">
            <span class="metric-card__label">待处理副作用</span>
            <strong class="metric-card__value">{{ pendingCount }}</strong>
            <span class="metric-card__hint">以本机 Outbox 实际状态为准</span>
          </NCard>
        </NGridItem>
        <NGridItem>
          <NCard class="metric-card" :bordered="false">
            <span class="metric-card__label">当前运行版本</span>
            <strong class="metric-card__value metric-card__value--text">{{ currentVersion }}</strong>
            <span class="metric-card__hint">仅展示后端返回的版本</span>
          </NCard>
        </NGridItem>
      </NGrid>
    </NSpin>

    <section class="content-grid">
      <NCard class="panel-card" :bordered="false">
        <template #header>
          <div class="card-heading">
            <div><h2>服务与连接</h2><p>查看真实连接状态，不会因页面打开而调用模型。</p></div>
            <AppIcon name="server" />
          </div>
        </template>
        <div v-if="services.length" class="service-list">
          <div v-for="service in services" :key="service.key" class="service-row">
            <div><strong>{{ serviceLabel(service.key) }}</strong><small>{{ serviceDetail(service.state, service.key) }}</small></div>
            <StatusPill :status="serviceStatus(service.state)" kind="runtime" />
          </div>
        </div>
        <NEmpty v-else description="服务详情尚未由后端提供" />
      </NCard>

      <NCard class="panel-card" :bordered="false">
        <template #header>
          <div class="card-heading">
            <div><h2>预算账本</h2><p>已用、预留、未知费用分开显示。</p></div>
            <AppIcon name="wallet" />
          </div>
        </template>
        <div v-if="budgets" class="budget-list">
          <div><span>已用</span><strong>{{ showAmount(budgets.spent) }}</strong></div>
          <div><span>预留</span><strong>{{ showAmount(budgets.reserved) }}</strong></div>
          <div><span>未知</span><strong class="value-warn">{{ showAmount(budgets.unknown) }}</strong></div>
          <div><span>上限</span><strong>{{ showAmount(budgets.limit) }}</strong></div>
          <div class="budget-list__foot">时区：{{ budgets.timezone || appState.hostSettings.data?.timezone || '未提供' }}</div>
        </div>
        <NEmpty v-else description="预算接口暂未返回数据" />
        <template #footer><RouterLink class="text-link" to="/budget">查看预算账本 <AppIcon name="arrow" :size="15" /></RouterLink></template>
      </NCard>
    </section>

    <NCard class="panel-card" :bordered="false">
      <template #header>
        <div class="card-heading">
          <div><h2>模块状态</h2><p>模块可按需待机；生命周期与实际运行状态分别呈现。</p></div>
          <RouterLink class="text-link" to="/modules">查看全部 <AppIcon name="arrow" :size="15" /></RouterLink>
        </div>
      </template>
      <div v-if="appState.modules.data?.length" class="module-summary-grid">
        <div v-for="module in appState.modules.data.slice(0, 6)" :key="module.id" class="module-summary">
          <div class="module-summary__top"><strong>{{ module.name || module.id }}</strong><StatusPill :status="module.runtime_state || 'unknown'" kind="runtime" /></div>
          <small>{{ module.id }}<span v-if="module.lifecycle"> · {{ module.lifecycle }}</span></small>
        </div>
      </div>
      <NEmpty v-else-if="!appState.modules.loading" description="模块状态尚未由后端提供" />
      <div v-else class="inline-loading">正在读取模块清单…</div>
    </NCard>

    <div class="quick-links">
      <RouterLink class="quick-link" to="/modules"><span class="quick-link__icon"><AppIcon name="modules" /></span><span><strong>模块与功能</strong><small>查看状态、依赖与能力</small></span><AppIcon name="arrow" :size="16" /></RouterLink>
      <RouterLink class="quick-link" to="/runtime"><span class="quick-link__icon"><AppIcon name="runtime" /></span><span><strong>运行批次</strong><small>手动创建、启动或停止</small></span><AppIcon name="arrow" :size="16" /></RouterLink>
      <RouterLink class="quick-link" to="/settings"><span class="quick-link__icon"><AppIcon name="settings" /></span><span><strong>偏好设置</strong><small>主题与本机路径状态</small></span><AppIcon name="arrow" :size="16" /></RouterLink>
    </div>
  </div>
</template>
