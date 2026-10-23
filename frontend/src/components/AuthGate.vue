<script setup lang="ts">
import { ref } from 'vue'
import { NAlert, NButton, NCard, NInput, NSpin, useMessage } from 'naive-ui'
import { authenticate, appState, refreshAuthentication } from '../stores/app'
import { errorMessage, requestReference } from '../api/errors'
import AppIcon from './AppIcon.vue'

const password = ref('')
const submitting = ref(false)
const formError = ref('')
const message = useMessage()

async function submit() {
  formError.value = ''
  if (!password.value) {
    formError.value = appState.setupRequired ? '请先设置本机管理员口令。' : '请输入本机管理员口令。'
    return
  }
  submitting.value = true
  const secret = password.value
  const wasSetup = appState.setupRequired
  try {
    await authenticate(secret)
    message.success(wasSetup ? '管理员入口已设置。' : '已进入本机控制台。')
  } catch (error) {
    formError.value = errorMessage(error)
    const requestId = requestReference(error)
    if (requestId) formError.value += ` 请求编号：${requestId}`
  } finally {
    password.value = ''
    submitting.value = false
  }
}

async function retry() {
  await refreshAuthentication()
}
</script>

<template>
  <main class="auth-page">
    <div class="auth-page__glow auth-page__glow--one" aria-hidden="true"></div>
    <div class="auth-page__glow auth-page__glow--two" aria-hidden="true"></div>
    <section class="auth-page__content" aria-labelledby="auth-title">
      <div class="auth-brand">
        <span class="brand__mark" aria-hidden="true">C</span>
        <div>
          <p class="eyebrow">CUTEFISH · LOCAL</p>
          <h1 id="auth-title">你好，欢迎回来</h1>
          <p class="auth-brand__intro">先通过本机管理员验证，再进入控制台。</p>
        </div>
      </div>

      <NCard v-if="appState.authStatus === 'checking'" class="auth-card" :bordered="false">
        <div class="auth-loading"><NSpin size="small" /><span>正在连接本机服务…</span></div>
      </NCard>

      <NCard v-else-if="appState.authStatus === 'unavailable'" class="auth-card" :bordered="false">
        <NAlert type="warning" :show-icon="true" title="本机服务未连接">
          {{ appState.authError ? errorMessage(appState.authError) : '无法读取本机认证状态。' }}
        </NAlert>
        <p class="auth-card__note">控制台只连接当前设备上的服务，不会回退到演示数据。</p>
        <NButton type="primary" block size="large" @click="retry">
          <template #icon><AppIcon name="refresh" /></template>
          重新连接
        </NButton>
      </NCard>

      <NCard v-else class="auth-card" :bordered="false">
        <div class="auth-card__heading">
          <span class="auth-card__icon"><AppIcon name="lock" :size="20" /></span>
          <div>
            <h2>{{ appState.setupRequired ? '设置管理员口令' : '管理员登录' }}</h2>
            <p>{{ appState.setupRequired ? '口令只提交给本机服务，不会在页面中回显或保存。' : '使用这台设备的管理员口令继续。' }}</p>
          </div>
        </div>
        <NAlert v-if="formError" type="error" :show-icon="true" class="auth-card__alert">{{ formError }}</NAlert>
        <form class="auth-form" @submit.prevent="submit">
          <label class="field-label" for="admin-password">本机管理员口令</label>
          <NInput
            id="admin-password"
            v-model:value="password"
            type="password"
            :autocomplete="appState.setupRequired ? 'new-password' : 'current-password'"
            placeholder="请输入口令"
            show-password-on="mousedown"
            size="large"
            :disabled="submitting"
          />
          <NButton type="primary" block size="large" attr-type="submit" :loading="submitting">
            {{ appState.setupRequired ? '创建本机管理员入口' : '进入控制台' }}
          </NButton>
        </form>
        <div class="auth-card__privacy"><span class="local-mark"></span>本机验证 · 密码字段不会回显</div>
      </NCard>

      <p class="auth-page__foot">本机试用环境 · 127.0.0.1</p>
    </section>
  </main>
</template>
