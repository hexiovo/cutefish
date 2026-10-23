<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { NAlert, NCard, NEmpty, NTag } from 'naive-ui'
import AppIcon from '../components/AppIcon.vue'
import StatusPill from '../components/StatusPill.vue'
import { appState } from '../stores/app'

const tab = ref<'modules' | 'features'>('modules')
const moduleCount = computed(() => appState.modules.data?.length ?? null)
const featureCount = computed(() => appState.features.data?.items.length ?? null)
const evidenceStates = [
  { key: 'implemented', label: '实现状态' },
  { key: 'local_verified', label: '本机核验' },
  { key: 'target_verified', label: '目标核验' },
  { key: 'production_enabled', label: '正式启用' },
] as const

function requirementLabel(status?: string) {
  const labels: Record<string, string> = { confirmed: '需求已确认', pending: '待确认', cancelled: '已取消', unknown: '状态未提供' }
  return labels[status || 'unknown'] ?? (status || '状态未提供')
}

function featureIdentity(feature: { feature_id?: string; id?: string }) {
  return feature.feature_id || feature.id || 'unknown'
}
</script>

<template>
  <div class="page-stack">
    <header class="page-heading">
      <div><p class="eyebrow">能力清单</p><h1>模块与功能</h1><p class="page-heading__description">注册状态、运行状态与交付状态分别来自本机服务。</p></div>
      <NTag round type="info">{{ moduleCount === null ? '模块数未知' : moduleCount + ' 个模块' }}</NTag>
    </header>

    <div class="tab-switch" role="tablist" aria-label="模块与功能视图">
      <button type="button" role="tab" :aria-selected="tab === 'modules'" :class="{ active: tab === 'modules' }" @click="tab = 'modules'">已注册模块</button>
      <button type="button" role="tab" :aria-selected="tab === 'features'" :class="{ active: tab === 'features' }" @click="tab = 'features'">功能交付状态 <span>{{ featureCount === null ? '—' : featureCount }}</span></button>
    </div>

    <template v-if="tab === 'modules'">
      <NAlert v-if="appState.modules.error" type="warning" :show-icon="true" title="模块清单暂不可用">{{ appState.modules.error.message }}</NAlert>
      <div v-if="appState.modules.data?.length" class="module-grid">
        <NCard v-for="module in appState.modules.data" :key="module.id" class="module-card" :bordered="false">
          <div class="module-card__header">
            <div class="module-card__identity"><span class="module-card__icon"><AppIcon name="modules" /></span><div><h2>{{ module.name || module.id }}</h2><small>{{ module.id }}<span v-if="module.version"> · v{{ module.version }}</span></small></div></div>
            <StatusPill :status="module.runtime_state || 'unknown'" kind="runtime" />
          </div>
          <div class="module-card__states">
            <div><span>启用状态</span><StatusPill :status="module.enabled === true ? 'enabled' : module.enabled === false ? 'disabled' : 'unknown'" kind="plain" /></div>
            <div><span>生命周期</span><span class="plain-state">{{ module.lifecycle || '未提供' }}</span></div>
            <div><span>就绪标记</span><span class="plain-state">{{ module.ready === true ? '已就绪' : module.ready === false ? '未就绪' : '未知' }}</span></div>
          </div>
          <div v-if="module.dependencies?.length" class="chip-list"><span class="muted-label">依赖</span><NTag v-for="dependency in module.dependencies" :key="dependency" size="small" round>{{ dependency }}</NTag></div>
          <div v-if="module.capabilities?.length" class="chip-list"><span class="muted-label">能力</span><NTag v-for="capability in module.capabilities" :key="capability.id" size="small" round>{{ capability.name || capability.id }}</NTag></div>
          <template #footer>
            <RouterLink class="text-link" :to="'/modules/' + encodeURIComponent(module.id)">查看配置 <AppIcon name="arrow" :size="15" /></RouterLink>
          </template>
        </NCard>
      </div>
      <NEmpty v-else-if="!appState.modules.loading" description="本机服务尚未提供模块清单" />
      <div v-else class="inline-loading">正在读取已注册模块…</div>
    </template>

    <template v-else>
      <NAlert v-if="appState.features.error" type="warning" :show-icon="true" title="功能交付状态暂不可用">{{ appState.features.error.message }}</NAlert>
      <NAlert v-else-if="appState.features.data?.source_status === 'unknown'" type="warning" :show-icon="true" title="四态状态源暂不可用">本机服务未能读取独立交付状态源；实现、本机核验和目标核验显示为 unknown。正式启用状态仍遵循运行时安全门禁，本机试用和迁移暂存模式下会被压为 disabled。不会从旧的交付摘要推断这三项证据状态。</NAlert>
      <div v-if="appState.features.data?.items.length" class="feature-list">
        <article v-for="feature in appState.features.data.items" :key="featureIdentity(feature)" class="feature-row">
          <div class="feature-row__main"><h2>{{ feature.name || featureIdentity(feature) }}</h2><p>{{ featureIdentity(feature) }}<span v-if="feature.module_id"> · {{ feature.module_id }}</span></p></div>
          <div class="feature-row__states feature-row__states--evidence">
            <div class="feature-state"><span>需求</span><strong>{{ requirementLabel(feature.requirement_status) }}</strong></div>
            <div v-for="item in evidenceStates" :key="item.key" class="feature-state">
              <span>{{ item.label }}</span>
              <StatusPill :status="feature[item.key]?.status || 'unknown'" kind="delivery" />
              <small v-if="feature[item.key]?.note">{{ feature[item.key]?.note }}</small>
              <small v-if="feature[item.key]?.evidence_refs?.length">证据：{{ feature[item.key]?.evidence_refs?.join(' · ') }}</small>
            </div>
          </div>
          <div class="feature-row__evidence">更新时间 {{ feature.status_updated_at || '未知' }} · 证据版本 {{ feature.evidence_revision ?? '未知' }}<span v-if="feature.evidence_refs?.length"> · 兼容证据 {{ feature.evidence_refs.join(' · ') }}</span></div>
        </article>
      </div>
      <NEmpty v-else-if="!appState.features.loading" description="本机服务没有提供功能交付记录" />
      <div v-else class="inline-loading">正在读取功能状态…</div>
    </template>
  </div>
</template>

<style scoped>
.feature-row__states--evidence { display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); gap: 9px; }
.feature-state { display: grid; min-width: 0; align-content: start; gap: 5px; padding: 9px; border-radius: 10px; background: var(--surface-soft); }
.feature-state > span { color: var(--text-muted); font-size: 10px; }
.feature-state > strong { color: var(--text-soft); font-size: 11px; }
.feature-state > small { color: var(--text-muted); font-size: 9px; line-height: 1.4; overflow-wrap: anywhere; }
.feature-row__evidence { overflow-wrap: anywhere; }
</style>
