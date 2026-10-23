<script setup lang="ts">
import AppIcon from './AppIcon.vue'

const emit = defineEmits<{ navigate: [] }>()

const links = [
  { to: '/overview', label: '总览', icon: 'overview' },
  { to: '/modules', label: '模块与功能', icon: 'modules' },
  { to: '/people', label: '联系人与 QQ', icon: 'book' },
  { to: '/chat', label: '实聊台', icon: 'chat' },
  { to: '/memory', label: '记忆与来源', icon: 'memory' },
  { to: '/persona', label: '人格与访谈', icon: 'edit' },
  { to: '/tasks', label: '任务与候选', icon: 'runtime' },
  { to: '/outbox', label: '消息草稿', icon: 'outbox' },
  { to: '/runtime', label: '运行批次', icon: 'runtime' },
  { to: '/budget', label: '预算账本', icon: 'wallet' },
  { to: '/rooms', label: '房间目录', icon: 'server' },
  { to: '/voice', label: '语音能力', icon: 'play' },
  { to: '/migration', label: '快照与迁移', icon: 'shield' },
  { to: '/integrations', label: '平台集成', icon: 'modules' },
  { to: '/settings', label: '偏好设置', icon: 'settings' },
]
</script>

<template>
  <div class="nav-panel">
    <RouterLink class="brand" to="/overview" @click="emit('navigate')">
      <span class="brand__mark" aria-hidden="true">C</span>
      <span class="brand__text">
        <strong>CuteFish</strong>
        <small>本地控制台</small>
      </span>
    </RouterLink>

    <p class="nav-panel__eyebrow">工作区</p>
    <nav class="nav-links" aria-label="主导航">
      <RouterLink v-for="link in links" :key="link.to" class="nav-link" :to="link.to" @click="emit('navigate')">
        <AppIcon :name="link.icon" :size="18" />
        <span>{{ link.label }}</span>
        <AppIcon v-if="$route.path === link.to" name="arrow" :size="15" class="nav-link__arrow" />
      </RouterLink>
    </nav>

    <div class="nav-panel__footer">
      <span class="local-mark" aria-hidden="true"></span>
      <span>仅在本机运行</span>
    </div>
  </div>
</template>
