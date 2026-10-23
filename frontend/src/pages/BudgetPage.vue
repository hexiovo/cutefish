<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { NAlert, NCard, NEmpty, NGrid, NGridItem, NSelect, NSpin, NTag } from 'naive-ui'
import { api } from '../api/client'
import { errorMessage } from '../api/errors'
import type { BudgetEntry, BudgetEntryState } from '../api/types'
import AppIcon from '../components/AppIcon.vue'
import StatusPill from '../components/StatusPill.vue'
import { appState, refreshPanel } from '../stores/app'

const budget = computed(() => appState.budgets.data ?? appState.overview.data?.budget_snapshot ?? null)
const entries = ref<BudgetEntry[]>([])
const entryTotal = ref<number | null>(null)
const entriesLoading = ref(false)
const entriesError = ref('')
const stateFilter = ref<BudgetEntryState | 'all'>('all')
const stateOptions = [
  { label: '全部状态', value: 'all' },
  { label: '已预留', value: 'reserved' },
  { label: '已派发', value: 'dispatched' },
  { label: '结果未知', value: 'unknown' },
  { label: '已结算', value: 'settled' },
  { label: '已释放', value: 'released' },
]

function amount(value: number | null | undefined, currency = budget.value?.currency || 'CNY') {
  if (value === null || value === undefined || !Number.isFinite(value)) return '未知'
  try {
    return new Intl.NumberFormat('zh-CN', { style: 'currency', currency }).format(value)
  } catch {
    return `¥${value.toFixed(2)}`
  }
}

function budgetState(value: number | null | undefined) {
  return value === null || value === undefined || !Number.isFinite(value) ? 'unknown' : 'ready'
}

const metrics = computed(() => [
  { label: '已确认支出', value: amount(budget.value?.spent), state: budgetState(budget.value?.spent), hint: '只累计本机账本已经确认的费用' },
  { label: '费用预留', value: amount(budget.value?.reserved), state: budgetState(budget.value?.reserved), hint: '未结算的调用预留单独统计' },
  { label: '结果未知', value: amount(budget.value?.unknown), state: budgetState(budget.value?.unknown), hint: '请求结果未确认时不会按零处理' },
  { label: '总预算上限', value: amount(budget.value?.limit), state: budgetState(budget.value?.limit), hint: '上限由本机服务实际返回' },
])

const backgroundLimits = computed(() => [
  { label: '后台软上限', value: budget.value?.background_soft_limit },
  { label: '后台借用上限', value: budget.value?.background_borrow_limit },
  { label: '后台硬上限', value: budget.value?.background_hard_limit },
])

async function loadEntries() {
  entriesLoading.value = true
  entriesError.value = ''
  try {
    const result = await api.budgetEntries({
      state: stateFilter.value === 'all' ? undefined : stateFilter.value,
      limit: 100,
    })
    entries.value = result.items
    entryTotal.value = result.total
  } catch (error) {
    entries.value = []
    entryTotal.value = null
    entriesError.value = errorMessage(error)
  } finally {
    entriesLoading.value = false
  }
}

function formatEntryDate(value?: string | null) {
  if (!value) return '日期未提供'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString('zh-CN')
}

watch(stateFilter, () => void loadEntries())

onMounted(() => {
  if (!appState.budgets.data && !appState.budgets.loading) void refreshPanel()
  void loadEntries()
})
</script>

<template>
  <div class="page-stack">
    <header class="page-heading">
      <div>
        <p class="eyebrow">费用与调用状态</p>
        <h1>预算账本</h1>
        <p class="page-heading__description">支出、预留与结果未知分开显示；未知金额不会被当作零或可用额度。</p>
      </div>
      <div class="page-heading__actions">
        <NTag round type="info">{{ budget?.currency || '本机账本' }}</NTag>
        <StatusPill :status="budget ? 'ready' : 'unknown'" />
      </div>
    </header>

    <NAlert v-if="appState.budgets.error" type="warning" :show-icon="true" title="预算账本暂不可用">
      {{ appState.budgets.error.message }}
    </NAlert>

    <NSpin :show="appState.budgets.loading && !budget">
      <NGrid cols="1 s:2 l:4" :x-gap="14" :y-gap="14" responsive="screen">
        <NGridItem v-for="metric in metrics" :key="metric.label">
          <NCard class="metric-card" :bordered="false">
            <span class="metric-card__label">{{ metric.label }}</span>
            <strong class="metric-card__value">{{ metric.value }}</strong>
            <span class="metric-card__hint">{{ metric.hint }}</span>
          </NCard>
        </NGridItem>
      </NGrid>
    </NSpin>

    <section class="content-grid">
      <NCard class="panel-card" :bordered="false">
        <template #header>
          <div class="card-heading">
            <div><h2>后台调用限额</h2><p>仅展示本机服务返回的实际策略。</p></div>
            <AppIcon name="wallet" />
          </div>
        </template>
        <div v-if="budget" class="budget-list">
          <div v-for="limit in backgroundLimits" :key="limit.label">
            <span>{{ limit.label }}</span><strong>{{ amount(limit.value) }}</strong>
          </div>
          <div class="budget-list__foot">时区：{{ budget.timezone || appState.hostSettings.data?.timezone || '未提供' }}</div>
        </div>
        <NEmpty v-else description="本机服务尚未返回预算策略" />
      </NCard>

      <NCard class="panel-card" :bordered="false">
        <template #header>
          <div class="card-heading">
            <div><h2>账本说明</h2><p>费用状态以本机服务的持久记录为准。</p></div>
            <AppIcon name="info" />
          </div>
        </template>
        <div class="budget-notes">
          <p><strong>已确认支出</strong>是本机服务已经结算的金额。</p>
          <p><strong>费用预留</strong>用于尚未结算的请求，避免重复占用预算。</p>
          <p><strong>结果未知</strong>表示请求已经发出但无法确认结果；处理前会保留其预算占用。</p>
        </div>
        <NAlert v-if="budget?.unknown == null" type="warning" :show-icon="true" class="runtime-note">
          未知费用金额尚未由账本确认，不能据此推算可用余额。
        </NAlert>
      </NCard>
    </section>

    <NCard class="panel-card" :bordered="false">
      <template #header>
        <div class="card-heading">
          <div><h2>费用流水</h2><p>每条记录展示预留、结算或结果未知状态。</p></div>
          <NSelect v-model:value="stateFilter" :options="stateOptions" size="small" class="ledger-filter" aria-label="按账目状态筛选" />
        </div>
      </template>
      <NAlert v-if="entriesError" type="warning" :show-icon="true" title="费用流水暂不可用">{{ entriesError }}</NAlert>
      <div v-else-if="entriesLoading && entries.length === 0" class="inline-loading">正在读取费用流水…</div>
      <div v-else-if="entries.length" class="budget-entry-list">
        <article v-for="entry in entries" :key="entry.reservation_id" class="budget-entry">
          <div class="budget-entry__top">
            <div class="budget-entry__title">
              <strong>{{ entry.purpose || entry.model || entry.provider || '调用费用记录' }}</strong>
              <small>{{ formatEntryDate(entry.local_date) }} · {{ entry.is_background ? '后台调用' : '前台调用' }}</small>
            </div>
            <StatusPill :status="entry.state" />
          </div>
          <div class="budget-entry__meta">
            <span>预留 <strong>{{ amount(entry.estimated_cost, entry.currency || undefined) }}</strong></span>
            <span>已结算 <strong>{{ amount(entry.actual_cost, entry.currency || undefined) }}</strong></span>
            <span>服务 <strong>{{ entry.provider || '未提供' }}<template v-if="entry.model"> · {{ entry.model }}</template></strong></span>
            <span>对象 <strong>{{ entry.person || '未提供' }}<template v-if="entry.account"> · {{ entry.account }}</template></strong></span>
            <span>价格版本 <strong>{{ entry.price_version || '未提供' }}</strong></span>
          </div>
        </article>
        <p v-if="entryTotal !== null" class="budget-list__foot">当前筛选返回 {{ entries.length }} 条，共 {{ entryTotal }} 条。</p>
      </div>
      <NEmpty v-else-if="!entriesLoading && !entriesError" description="当前筛选下没有费用记录" />
    </NCard>

    <NEmpty v-if="!budget && !appState.budgets.loading && !appState.budgets.error" description="尚无预算快照" />
  </div>
</template>

<style scoped>
.budget-notes { display: grid; gap: 12px; color: var(--text-soft); font-size: 13px; }
.budget-notes p { margin: 0; }
.budget-notes strong { color: var(--text); }
.ledger-filter { width: min(100%, 180px); }
.budget-entry-list { display: grid; gap: 10px; }
.budget-entry { display: grid; gap: 12px; min-width: 0; padding: 14px; border: 1px solid var(--border); border-radius: 14px; background: var(--surface); }
.budget-entry__top { display: flex; min-width: 0; align-items: flex-start; justify-content: space-between; gap: 12px; }
.budget-entry__title { display: grid; min-width: 0; gap: 4px; }
.budget-entry__title strong { overflow-wrap: anywhere; color: var(--text); font-size: 13px; }
.budget-entry__title small { color: var(--text-muted); font-size: 11px; }
.budget-entry__meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 210px), 1fr)); gap: 8px 16px; color: var(--text-muted); font-size: 11px; }
.budget-entry__meta strong { color: var(--text-soft); font-weight: 600; }
@media (max-width: 620px) { .card-heading { align-items: flex-start; } .ledger-filter { width: 100%; } }
</style>
