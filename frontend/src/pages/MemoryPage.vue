<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NCard, NEmpty, NInput, NPopconfirm, NSelect, NTag } from 'naive-ui'
import { useRoute } from 'vue-router'
import { api } from '../api/client'
import { errorMessage } from '../api/errors'
import type { ConversationScope, MemoryItem, PersonItem } from '../api/types'
import AppIcon from '../components/AppIcon.vue'
import StatusPill from '../components/StatusPill.vue'

const people = ref<PersonItem[]>([])
const items = ref<MemoryItem[]>([])
const route = useRoute()
const personId = ref('')
const scope = ref<ConversationScope>('production')
const includeHistory = ref(true)
const loading = ref(false)
const savingId = ref('')
const error = ref('')
const correctionId = ref('')
const correctionContent = ref('')
const correctionSourceMessage = ref('')

const peopleOptions = computed(() => people.value.map((person) => ({ label: `${person.display_name} · ${person.role}`, value: person.person_id })))
const scopeOptions = [
  { label: '正式记忆', value: 'production' },
  { label: '校准隔离区', value: 'calibration' },
]

function formatTime(value?: string | null) {
  if (!value) return '未提供'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN')
}

function sourceText(item: MemoryItem) {
  const parts = [item.source_reference, item.source_locator, item.source_message_id ? `消息 ${item.source_message_id}` : '', item.source_event_id ? `事件 ${item.source_event_id}` : '']
  return parts.filter(Boolean).join(' · ') || '原文来源未提供'
}

async function loadPeople() {
  try {
    people.value = (await api.people()).items
    const requestedPersonId = typeof route.query.personId === 'string' ? route.query.personId : ''
    if (requestedPersonId && people.value.some((person) => person.person_id === requestedPersonId)) personId.value = requestedPersonId
    else if (!personId.value) personId.value = people.value.find((person) => person.role === 'owner')?.person_id ?? people.value[0]?.person_id ?? ''
  } catch (cause) {
    error.value = errorMessage(cause)
  }
}

async function loadMemory() {
  if (!personId.value) return
  loading.value = true
  error.value = ''
  try {
    items.value = (await api.memoryItems(personId.value, { scope: scope.value, includeHistory: includeHistory.value, limit: 100 })).items
  } catch (cause) {
    items.value = []
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

function beginCorrection(item: MemoryItem) {
  correctionId.value = item.id
  correctionContent.value = item.content
  correctionSourceMessage.value = ''
}

async function saveCorrection(item: MemoryItem) {
  if (!correctionContent.value.trim() || savingId.value) return
  savingId.value = item.id
  error.value = ''
  try {
    await api.correctMemory(item.id, {
      corrected_content: correctionContent.value.trim(),
      expected_version: item.version,
      source_event_id: crypto.randomUUID(),
      source_message_id: correctionSourceMessage.value.trim() || undefined,
    })
    correctionId.value = ''
    await loadMemory()
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    savingId.value = ''
  }
}

async function deleteItem(item: MemoryItem) {
  savingId.value = item.id
  error.value = ''
  try {
    await api.deleteMemory(item.id, { expected_version: item.version })
    await loadMemory()
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    savingId.value = ''
  }
}

watch([personId, scope, includeHistory], () => void loadMemory())
onMounted(async () => {
  await loadPeople()
  await loadMemory()
})
</script>

<template>
  <div class="page-stack">
    <header class="page-heading">
      <div><p class="eyebrow">按人物隔离的记忆</p><h1>记忆与来源</h1><p class="page-heading__description">按人物和用途读取来源记录。纠正会创建新版本并保留旧链；删除是软删除。</p></div>
      <div class="page-heading__actions"><NTag round type="info">不跨联系人混用</NTag><NButton quaternary :loading="loading" @click="loadMemory">刷新列表</NButton></div>
    </header>

    <NAlert v-if="error" type="warning" :show-icon="true" title="记忆操作未完成">{{ error }}</NAlert>
    <NAlert type="info" :show-icon="true" title="隔离范围">
      正式记忆与校准试聊分开保存；页面不会读取原始文件或其他人物的记忆。校准内容不会自动进入正式人格或正式记忆。
    </NAlert>

    <NCard class="panel-card" :bordered="false">
      <div class="memory-filters">
        <label class="memory-filter"><span>人物</span><NSelect v-model:value="personId" :options="peopleOptions" placeholder="选择人物" /></label>
        <label class="memory-filter"><span>用途范围</span><NSelect v-model:value="scope" :options="scopeOptions" /></label>
        <label class="memory-history"><input v-model="includeHistory" type="checkbox"><span>包括已纠正、过期和删除记录</span></label>
      </div>
    </NCard>

    <div v-if="items.length" class="memory-list">
      <NCard v-for="item in items" :key="item.id" class="panel-card" :bordered="false">
        <template #header><div class="card-heading"><div><h2>{{ item.kind }} · v{{ item.version }}</h2><p>{{ item.person_id }} · {{ item.scope }}</p></div><StatusPill :status="item.status" /></div></template>
        <p class="memory-content">{{ item.content }}</p>
        <div class="memory-provenance">
          <div><span>原文来源</span><strong>{{ sourceText(item) }}</strong></div>
          <div><span>生效起点</span><strong>{{ formatTime(item.valid_from) }}</strong></div>
          <div><span>生效终点</span><strong>{{ formatTime(item.valid_until) }}</strong></div>
          <div v-if="item.supersedes_id"><span>修正自</span><strong>{{ item.supersedes_id }}</strong></div>
          <div v-if="item.corrected_by_id"><span>被版本替代</span><strong>{{ item.corrected_by_id }}</strong></div>
          <div v-if="item.invalidation_reason"><span>失效原因</span><strong>{{ item.invalidation_reason }}</strong></div>
        </div>
        <div v-if="correctionId === item.id" class="memory-edit">
          <label class="field-label" :for="`correction-${item.id}`">纠正后的内容</label>
          <NInput :id="`correction-${item.id}`" v-model:value="correctionContent" type="textarea" :autosize="{ minRows: 3, maxRows: 8 }" maxlength="12000" />
          <label class="field-label" :for="`correction-source-${item.id}`">对应原消息 ID（可选）</label>
          <NInput :id="`correction-source-${item.id}`" v-model:value="correctionSourceMessage" placeholder="如果这条纠正确有对应消息，可填入其 ID" />
          <p class="muted-copy">本次纠正会记录为新的 owner 操作事件；原记录与来源链会保留。</p>
          <div class="memory-actions"><NButton quaternary @click="correctionId = ''">取消编辑</NButton><NButton type="primary" :loading="savingId === item.id" @click="saveCorrection(item)">保存新版本</NButton></div>
        </div>
        <div v-else class="memory-actions">
          <NButton v-if="item.status === 'active'" size="small" secondary @click="beginCorrection(item)"><template #icon><AppIcon name="edit" :size="15" /></template>纠正内容</NButton>
          <NPopconfirm v-if="item.status !== 'deleted'" @positive-click="deleteItem(item)">
            <template #trigger><NButton size="small" quaternary type="error" :loading="savingId === item.id">软删除</NButton></template>
            删除后会保留审计记录与原来源，继续吗？
          </NPopconfirm>
        </div>
      </NCard>
    </div>
    <NEmpty v-else-if="!loading" description="所选人物和范围没有记忆记录" />
    <div v-else class="inline-loading">正在读取记忆来源…</div>
  </div>
</template>

<style scoped>
.memory-filters { display: grid; grid-template-columns: repeat(2, minmax(180px, 1fr)) minmax(220px, auto); align-items: end; gap: 12px; }
.memory-filter { display: grid; gap: 6px; color: var(--text-muted); font-size: 11px; font-weight: 700; }
.memory-history { display: flex; align-items: center; gap: 8px; min-height: 40px; color: var(--text-muted); font-size: 11px; }
.memory-list { display: grid; gap: 13px; }
.memory-content { margin: 0; padding: 14px; border-radius: 12px; background: var(--surface-soft); color: var(--text); white-space: pre-wrap; overflow-wrap: anywhere; }
.memory-provenance { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 230px), 1fr)); gap: 8px 16px; margin-top: 13px; }
.memory-provenance > div { display: grid; min-width: 0; gap: 2px; }
.memory-provenance span { color: var(--text-muted); font-size: 10px; }
.memory-provenance strong { color: var(--text-soft); font-size: 11px; overflow-wrap: anywhere; }
.memory-edit { display: grid; gap: 9px; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--border); }
.memory-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; margin-top: 14px; }
@media (max-width: 700px) { .memory-filters { grid-template-columns: 1fr; } }
</style>
