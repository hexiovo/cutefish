<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { NConfigProvider, NDialogProvider, NMessageProvider, NNotificationProvider, darkTheme } from 'naive-ui'
import AppShell from './components/AppShell.vue'
import AuthGate from './components/AuthGate.vue'
import { appState, refreshAuthentication } from './stores/app'

const systemDark = ref(false)
let colorQuery: MediaQueryList | null = null

const themePreference = computed(() => appState.uiSettings.data?.theme ?? 'system')
const resolvedDark = computed(() => themePreference.value === 'dark' || (themePreference.value === 'system' && systemDark.value))

function updateTheme() {
  document.documentElement.dataset.theme = resolvedDark.value ? 'dark' : 'light'
}

watch(resolvedDark, updateTheme, { immediate: true })

onMounted(() => {
  colorQuery = window.matchMedia('(prefers-color-scheme: dark)')
  systemDark.value = colorQuery.matches
  updateTheme()
  colorQuery.addEventListener('change', onSystemThemeChange)
  void refreshAuthentication()
})

function onSystemThemeChange(event: MediaQueryListEvent) {
  systemDark.value = event.matches
  if (themePreference.value === 'system') updateTheme()
}

onUnmounted(() => colorQuery?.removeEventListener('change', onSystemThemeChange))
</script>

<template>
  <NConfigProvider :theme="resolvedDark ? darkTheme : null">
    <NMessageProvider>
      <NDialogProvider>
        <NNotificationProvider>
          <AuthGate v-if="!appState.authenticated" />
          <AppShell v-else />
        </NNotificationProvider>
      </NDialogProvider>
    </NMessageProvider>
  </NConfigProvider>
</template>
