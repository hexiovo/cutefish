<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterView, useRouter } from 'vue-router'
import {
  NButton,
  NDrawer,
  NDrawerContent,
  NPopconfirm,
  NTag,
  useMessage,
} from 'naive-ui'
import { api } from '../api/client'
import { errorMessage } from '../api/errors'
import { appState, logout, refreshPanel } from '../stores/app'
import AppIcon from './AppIcon.vue'
import NavigationLinks from './NavigationLinks.vue'
import StatusPill from './StatusPill.vue'

const router = useRouter()
const message = useMessage()
const mobileNavOpen = ref(false)
const narrow = ref(false)
let mediaQuery: MediaQueryList | null = null

function updateNarrow() {
  narrow.value = Boolean(mediaQuery?.matches)
  if (!narrow.value) mobileNavOpen.value = false
}

onMounted(() => {
  mediaQuery = window.matchMedia('(max-width: 900px)')
  updateNarrow()
  mediaQuery.addEventListener('change', updateNarrow)
})

onUnmounted(() => mediaQuery?.removeEventListener('change', updateNarrow))

const hostId = computed(() => appState.hostSettings.data?.host_id ?? appState.overview.data?.host_id ?? null)
const mode = computed(() => appState.hostSettings.data?.mode ?? appState.overview.data?.mode ?? null)
const activeBatch = computed(() => {
  const overviewBatch = appState.overview.data?.active_trial_batch
  if (overviewBatch?.state === 'running') return overviewBatch
  return appState.batches.data?.find((batch) => batch.state === 'running') ?? null
})
const activeBatchKnown = computed(() => appState.overview.data !== null || appState.batches.data !== null)
const budget = computed(() => appState.budgets.data ?? appState.overview.data?.budget_snapshot ?? null)

function amount(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return '未知'
  return `¥${value.toFixed(2)}`
}

const budgetSummary = computed(() => {
  if (!budget.value) return '预算状态未知'
  return `已用 ${amount(budget.value.spent)} · 预留 ${amount(budget.value.reserved)} · 未知 ${amount(budget.value.unknown)} / 上限 ${amount(budget.value.limit)}`
})

async function stopActiveBatch() {
  if (!activeBatch.value) return
  try {
    await api.stopBatch(activeBatch.value.id)
    await refreshPanel()
    message.success('已向本机服务提交停止请求。')
  } catch (error) {
    message.error(errorMessage(error))
  }
}

async function refreshAll() {
  await refreshPanel()
  message.info('已从本机服务刷新状态。')
}

async function signOut() {
  try {
    await logout()
    await router.replace('/overview')
    message.success('已退出本机管理员会话。')
  } catch (error) {
    message.error(errorMessage(error))
  }
}
</script>

<template>
  <div class="app-shell">
    <aside v-if="!narrow" class="app-sidebar"><NavigationLinks /></aside>
    <NDrawer v-model:show="mobileNavOpen" placement="left" :width="280" :auto-focus="false">
      <NDrawerContent :native-scrollbar="false" closable>
        <NavigationLinks @navigate="mobileNavOpen = false" />
      </NDrawerContent>
    </NDrawer>

    <div class="app-main">
      <header class="topbar">
        <div class="topbar__left">
          <NButton v-if="narrow" quaternary circle aria-label="打开导航" @click="mobileNavOpen = true">
            <template #icon><AppIcon name="menu" /></template>
          </NButton>
          <div class="topbar__mode">
            <span class="eyebrow">当前模式</span>
            <NTag v-if="mode === 'local_trial'" size="small" round type="info">本机小批量试用</NTag>
            <StatusPill v-else :status="mode" kind="plain" />
          </div>
          <div class="topbar__host">
            <AppIcon name="server" :size="16" />
            <span>{{ hostId || '主机标识未提供' }}</span>
          </div>
        </div>
        <div class="topbar__right">
          <div class="topbar__batch" :title="activeBatch?.id || '当前没有运行批次'">
            <span class="local-mark" :class="{ 'local-mark--active': activeBatch }"></span>
            <span>{{ activeBatch ? `批次运行中 · ${activeBatch.label || activeBatch.id}` : activeBatchKnown ? '无运行批次' : '批次状态未知' }}</span>
          </div>
          <NPopconfirm v-if="activeBatch" @positive-click="stopActiveBatch">
            <template #trigger>
              <NButton size="small" type="error" secondary>
                <template #icon><AppIcon name="stop" :size="15" /></template>
                停止批次
              </NButton>
            </template>
            确认向本机服务提交停止请求？
          </NPopconfirm>
          <div class="topbar__budget" :title="budgetSummary"><AppIcon name="wallet" :size="16" /><span>{{ budgetSummary }}</span></div>
          <NButton size="small" quaternary :loading="appState.refreshing" aria-label="刷新本机状态" @click="refreshAll">
            <template #icon><AppIcon name="refresh" /></template>
            <span class="button-text">刷新</span>
          </NButton>
          <NButton size="small" quaternary aria-label="退出管理员会话" @click="signOut">
            <template #icon><AppIcon name="logout" /></template>
            <span class="button-text">退出</span>
          </NButton>
        </div>
      </header>

      <section v-if="appState.overview.error && !appState.overview.data" class="connection-strip" role="status">
        <AppIcon name="warning" :size="17" />
        <span>总览接口暂不可用：{{ appState.overview.error.message }}</span>
      </section>

      <main class="page-area" id="main-content" tabindex="-1">
        <RouterView />
      </main>
    </div>
  </div>
</template>
