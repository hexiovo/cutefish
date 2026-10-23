<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NCard, NEmpty, NInput, NPopconfirm, NSelect, NSpin, NTag, useMessage } from 'naive-ui'
import { api } from '../api/client'
import { errorMessage } from '../api/errors'
import type { PersonItem, QqAccountItem, QqIdentityItem, QqServiceStatus } from '../api/types'
import AppIcon from '../components/AppIcon.vue'
import StatusPill from '../components/StatusPill.vue'

const people = ref<PersonItem[]>([])
const accounts = ref<QqAccountItem[]>([])
const qqStatus = ref<QqServiceStatus | null>(null)
const identities = ref<QqIdentityItem[]>([])
const selectedAccountId = ref('')
const peopleError = ref('')
const accountError = ref('')
const statusError = ref('')
const identityError = ref('')
const loading = ref(false)
const newPersonName = ref('')
const creatingPerson = ref(false)
const createPersonError = ref('')
const accountId = ref('')
const accountName = ref('')
const serviceUrl = ref('')
const creatingAccount = ref(false)
const accountCreateError = ref('')
const ownerSenderId = ref('')
const contactPersonId = ref('')
const contactSenderId = ref('')
const bindingOwner = ref(false)
const bindingContact = ref(false)
const napcatAccessToken = ref('')
const webhookSecret = ref('')
const savingCredentials = ref(false)
const credentialsError = ref('')
const savingEnabled = ref(false)
const enabledError = ref('')
const message = useMessage()
const accountOptions = computed(() => accounts.value.map((account) => ({ label: account.display_name || account.account_id, value: account.account_id })))
const contactOptions = computed(() => people.value.filter((person) => person.role === 'contact' && person.chat_only && person.state === 'active').map((person) => ({ label: person.display_name, value: person.person_id })))
const selectedAccount = computed(() => accounts.value.find((account) => account.account_id === selectedAccountId.value) ?? null)
const canEnableSelectedAccount = computed(() => Boolean(
  selectedAccount.value
  && !selectedAccount.value.needs_credentials
  && selectedAccount.value.service_url
  && selectedAccount.value.owner_configured,
))

async function loadPeople() {
  peopleError.value = ''
  try {
    people.value = (await api.people()).items
  } catch (error) {
    people.value = []
    peopleError.value = errorMessage(error)
  }
}

async function loadQqStatus() {
  statusError.value = ''
  try {
    qqStatus.value = await api.qqStatus()
  } catch (error) {
    qqStatus.value = null
    statusError.value = errorMessage(error)
  }
}

async function loadAccounts() {
  accountError.value = ''
  try {
    accounts.value = (await api.qqAccounts()).items
    if (!selectedAccountId.value || !accounts.value.some((item) => item.account_id === selectedAccountId.value)) selectedAccountId.value = accounts.value[0]?.account_id ?? ''
    if (selectedAccountId.value) await loadIdentities(selectedAccountId.value)
    else identities.value = []
  } catch (error) {
    accounts.value = []
    accountError.value = errorMessage(error)
  }
}

async function loadIdentities(id: string) {
  if (!id) { identities.value = []; return }
  identityError.value = ''
  try {
    identities.value = (await api.qqIdentities(id)).items
  } catch (error) {
    identities.value = []
    identityError.value = errorMessage(error)
  }
}

async function refresh() {
  loading.value = true
  await Promise.all([loadPeople(), loadQqStatus(), loadAccounts()])
  loading.value = false
}

async function createPerson() {
  const displayName = newPersonName.value.trim()
  if (!displayName || creatingPerson.value) return
  creatingPerson.value = true
  createPersonError.value = ''
  try {
    await api.createPerson({ display_name: displayName })
    newPersonName.value = ''
    await loadPeople()
    message.success('已登记为仅限聊天的联系人。')
  } catch (error) {
    createPersonError.value = errorMessage(error)
  } finally {
    creatingPerson.value = false
  }
}

async function createAccount() {
  if (!accountId.value.trim() || !accountName.value.trim() || creatingAccount.value) return
  creatingAccount.value = true
  accountCreateError.value = ''
  try {
    const created = await api.createQqAccount({
      account_id: accountId.value.trim(),
      display_name: accountName.value.trim(),
      service_url: serviceUrl.value.trim() || undefined,
    })
    accountId.value = ''
    accountName.value = ''
    serviceUrl.value = ''
    selectedAccountId.value = created.account_id
    await refresh()
    message.success('已登记 QQ 账号元数据；账号仍停用，未连接或发送。')
  } catch (error) {
    accountCreateError.value = errorMessage(error)
  } finally {
    creatingAccount.value = false
  }
}

async function bindOwner() {
  if (!selectedAccountId.value || !ownerSenderId.value.trim() || bindingOwner.value) return
  bindingOwner.value = true
  identityError.value = ''
  try {
    await api.bindQqOwner(selectedAccountId.value, { sender_id: ownerSenderId.value.trim() })
    ownerSenderId.value = ''
    await refresh()
    message.success('已保存管理员 sender_id 映射；这不会连接 QQ 服务。')
  } catch (error) {
    identityError.value = errorMessage(error)
  } finally {
    bindingOwner.value = false
  }
}

async function bindContact() {
  if (!selectedAccountId.value || !contactPersonId.value || !contactSenderId.value.trim() || bindingContact.value) return
  bindingContact.value = true
  identityError.value = ''
  try {
    await api.bindQqContact(selectedAccountId.value, { person_id: contactPersonId.value, sender_id: contactSenderId.value.trim() })
    contactSenderId.value = ''
    await loadIdentities(selectedAccountId.value)
    message.success('已保存联系人 sender_id 映射；该联系人仍仅限聊天。')
  } catch (error) {
    identityError.value = errorMessage(error)
  } finally {
    bindingContact.value = false
  }
}

async function saveQqCredentials() {
  const account = selectedAccount.value
  if (!account || savingCredentials.value || !napcatAccessToken.value.trim() || webhookSecret.value.trim().length < 32) return
  savingCredentials.value = true
  credentialsError.value = ''
  try {
    const result = await api.configureQqCredentials(account.account_id, {
      napcat_access_token: napcatAccessToken.value.trim(),
      webhook_secret: webhookSecret.value.trim(),
    })
    napcatAccessToken.value = ''
    webhookSecret.value = ''
    await refresh()
    message.success(result.credentials_configured ? '凭据已保存到本机安全凭据库；页面不会回显。' : '凭据写入请求已完成。')
  } catch (error) {
    credentialsError.value = errorMessage(error)
  } finally {
    savingCredentials.value = false
  }
}

async function setSelectedAccountEnabled(enabled: boolean) {
  const account = selectedAccount.value
  if (!account || savingEnabled.value || (enabled && !canEnableSelectedAccount.value)) return
  savingEnabled.value = true
  enabledError.value = ''
  try {
    await api.setQqAccountEnabled(account.account_id, enabled)
    await refresh()
    message.success(enabled
      ? 'QQ桥接适配器已启用；未探测 NapCat、未调用模型、未发送消息。'
      : 'QQ桥接适配器已停用。')
  } catch (error) {
    enabledError.value = errorMessage(error)
  } finally {
    savingEnabled.value = false
  }
}

function qqState() {
  if (qqStatus.value?.needs_credentials) return 'needs_credentials'
  if (qqStatus.value?.needs_connection) return 'needs_connection'
  if (qqStatus.value?.connected === true) return 'ready'
  if (qqStatus.value?.connected === false) return 'not_connected'
  return qqStatus.value?.status || 'unknown'
}

function boolLabel(value?: boolean) {
  if (value === true) return '是'
  if (value === false) return '否'
  return '未提供'
}

onMounted(() => void refresh())
watch(selectedAccountId, (id) => {
  napcatAccessToken.value = ''
  webhookSecret.value = ''
  credentialsError.value = ''
  enabledError.value = ''
  void loadIdentities(id)
})
</script>

<template>
  <div class="page-stack">
    <header class="page-heading">
      <div>
        <p class="eyebrow">人物与连接</p>
        <h1>联系人与 QQ</h1>
        <p class="page-heading__description">联系人角色、聊天范围与 QQ 服务状态均来自本机接口；凭据不会在此页展示。</p>
      </div>
      <div class="page-heading__actions">
        <StatusPill :status="qqState()" />
        <NButton quaternary :loading="loading" @click="refresh">刷新本页状态</NButton>
      </div>
    </header>

    <section class="content-grid">
      <NCard class="panel-card" :bordered="false">
        <template #header>
          <div class="card-heading">
            <div><h2>联系人与角色</h2><p>只显示明确登记的人物资料，不推断QQ身份。</p></div>
            <AppIcon name="book" />
          </div>
        </template>
        <NAlert v-if="peopleError" type="warning" :show-icon="true" title="联系人列表暂不可用">{{ peopleError }}</NAlert>
        <NSpin v-else :show="loading && people.length === 0">
          <div class="person-create">
            <label class="field-label" for="new-person-name">新增聊天联系人</label>
            <div class="person-create__controls">
              <NInput id="new-person-name" v-model:value="newPersonName" maxlength="80" placeholder="显示名称" @keyup.enter="createPerson" />
              <NButton type="primary" :disabled="!newPersonName.trim()" :loading="creatingPerson" @click="createPerson">添加</NButton>
            </div>
            <small>新增联系人由服务端固定标记为普通 chat-only；不能在此设置为 owner 或 QQ 发送身份。</small>
          </div>
          <NAlert v-if="createPersonError" type="error" :show-icon="true" class="runtime-note" title="联系人未保存">{{ createPersonError }}</NAlert>
          <div v-if="people.length" class="person-list">
            <article v-for="person in people" :key="person.person_id" class="person-card">
              <div class="person-card__top">
                <div><h3>{{ person.display_name || '未命名联系人' }}</h3><small>{{ person.person_id }}</small></div>
                <StatusPill :status="person.state" />
              </div>
              <div class="person-card__meta">
                <span>角色：<strong>{{ person.role || '未提供' }}</strong></span>
                <NTag v-if="person.chat_only" size="small" round type="info">仅限聊天</NTag>
              </div>
              <div class="person-card__actions">
                <RouterLink class="text-link" :to="{ name: 'memory', query: { personId: person.person_id } }">查看记忆</RouterLink>
              </div>
            </article>
          </div>
          <NEmpty v-else description="本机服务尚未登记联系人" />
        </NSpin>
      </NCard>

      <NCard class="panel-card" :bordered="false">
        <template #header>
          <div class="card-heading">
            <div><h2>QQ 服务账号</h2><p>登录或配对状态由本机服务报告。</p></div>
            <AppIcon name="server" />
          </div>
        </template>
        <NAlert v-if="statusError" type="warning" :show-icon="true" title="QQ 服务状态暂不可用">{{ statusError }}</NAlert>
        <div v-else class="service-row">
          <div><strong>QQ 桥接</strong><small>{{ qqStatus?.detail || (qqStatus?.needs_credentials ? '尚未配置连接凭据' : qqStatus?.needs_connection ? '需要在目标设备完成登录或配对' : '状态以本机接口返回为准') }}</small></div>
          <StatusPill :status="qqState()" kind="runtime" />
        </div>

        <NAlert v-if="qqStatus?.needs_credentials" type="warning" :show-icon="true" class="runtime-note">
          当前全局状态没有可用的已启用账号。凭据是否已配置以单个账号的安全状态为准；本页只提供 write-only 输入，不会读取或回显已保存凭据。
        </NAlert>
        <NAlert v-else-if="qqStatus?.needs_connection" type="info" :show-icon="true" class="runtime-note">
          请在目标设备完成账号登录或配对；此页面不会发送消息。
        </NAlert>

        <NAlert v-if="accountError" type="warning" :show-icon="true" class="runtime-note" title="账号列表暂不可用">{{ accountError }}</NAlert>
        <details class="account-create-form">
          <summary>登记 QQ 账号元数据</summary>
          <div class="account-create-form__body">
            <label class="field-label" for="qq-account-id">本地账号标识</label><NInput id="qq-account-id" v-model:value="accountId" maxlength="128" placeholder="由管理员提供的稳定标识" />
            <label class="field-label" for="qq-account-name">显示名称</label><NInput id="qq-account-name" v-model:value="accountName" maxlength="128" placeholder="例如：管理员 QQ" />
            <label class="field-label" for="qq-service-url">本机 NapCat 服务地址（可选）</label><NInput id="qq-service-url" v-model:value="serviceUrl" maxlength="512" placeholder="只接受 localhost / 127.0.0.1 / ::1" />
            <p class="settings-note">此操作只登记服务地址和账号元数据，账号保持停用；不接收密钥、不登录、不连接、不发送。</p>
            <NAlert v-if="accountCreateError" type="error" :show-icon="true">{{ accountCreateError }}</NAlert>
            <NButton type="primary" :disabled="!accountId.trim() || !accountName.trim()" :loading="creatingAccount" @click="createAccount">保存本机账号登记</NButton>
          </div>
        </details>
        <div v-if="accounts.length && !accountError" class="account-list">
          <article v-for="account in accounts" :key="account.account_id" class="account-row">
            <div>
              <strong>{{ account.display_name || account.account_id }}</strong>
              <small>{{ account.account_id }} · {{ account.needs_credentials ? '需配置凭据' : account.enabled ? '需在目标设备验证连接' : '凭据已配置，默认停用' }}</small>
              <small>NapCat：{{ account.service_url || '未登记' }} · 管理员 sender：{{ account.owner_sender_id || '未登记' }}</small>
              <small>所有者已配置：{{ boolLabel(account.owner_configured) }} · 已启用：{{ boolLabel(account.enabled) }} · 群聊：{{ boolLabel(account.group_chat_enabled) }}</small>
            </div>
            <StatusPill :status="account.needs_credentials ? 'needs_credentials' : account.needs_connection ? 'needs_connection' : account.status" kind="runtime" />
          </article>
        </div>
        <NEmpty v-if="!accounts.length && !loading && !accountError" description="本机服务尚未返回已登记的 QQ 账号" />
        <div class="identity-management">
          <div class="identity-management__top"><div><h3>发送身份映射</h3><small>映射只用于识别来信；不会登录、连接或外发。</small></div><NSelect v-model:value="selectedAccountId" :options="accountOptions" placeholder="选择已登记账号" size="small" /></div>
          <NAlert v-if="identityError" type="warning" :show-icon="true" class="runtime-note">{{ identityError }}</NAlert>
          <div v-if="selectedAccountId" class="identity-forms">
            <section v-if="selectedAccount" class="qq-bridge-controls">
              <NAlert type="info" :show-icon="true" title="账号凭据仅写入本机安全凭据库">
                访问令牌和独立 webhook 密钥不会从服务器读取、写入浏览器存储或在界面回显。启用仅打开本地桥接适配器，不探测 NapCat、不调用 DeepSeek、不发送 QQ 消息。
              </NAlert>
              <div class="credential-form">
                <strong>写入或轮换凭据</strong>
                <small>当前状态：{{ selectedAccount.needs_credentials ? '未配置' : '已配置' }}。启用前需要两项凭据、NapCat 本机地址和管理员身份映射。</small>
                <label class="field-label" for="qq-napcat-token">NapCat 访问令牌</label>
                <NInput id="qq-napcat-token" v-model:value="napcatAccessToken" type="password" maxlength="4096" autocomplete="new-password" placeholder="只在本次页面内暂存" />
                <label class="field-label" for="qq-webhook-secret">独立入站 webhook 密钥（至少 32 个字符）</label>
                <NInput id="qq-webhook-secret" v-model:value="webhookSecret" type="password" maxlength="4096" autocomplete="new-password" placeholder="不要与 NapCat 访问令牌复用" />
                <NAlert v-if="credentialsError" type="error" :show-icon="true">{{ credentialsError }}</NAlert>
                <NButton type="primary" :disabled="!napcatAccessToken.trim() || webhookSecret.trim().length < 32" :loading="savingCredentials" @click="saveQqCredentials">保存凭据</NButton>
              </div>
              <div class="qq-enable-form">
                <div><strong>本机适配器</strong><small>{{ selectedAccount.enabled ? '已启用；仍需各次独立授权才可接收或外发。' : '默认停用。启用不会自动连接或登录。' }}</small></div>
                <NAlert v-if="enabledError" type="error" :show-icon="true">{{ enabledError }}</NAlert>
                <NPopconfirm v-if="!selectedAccount.enabled" :disabled="!canEnableSelectedAccount" @positive-click="setSelectedAccountEnabled(true)">
                  <template #trigger><NButton type="primary" :disabled="!canEnableSelectedAccount" :loading="savingEnabled">显式启用本机适配器</NButton></template>
                  只启用本机 QQ 桥接适配器；不会探测 NapCat、调用模型或发送消息。仍需逐次有效授权。确认启用？
                </NPopconfirm>
                <NButton v-else type="warning" quaternary :loading="savingEnabled" @click="setSelectedAccountEnabled(false)">停用本机适配器</NButton>
              </div>
            </section>
            <div class="identity-form">
              <strong>管理员身份</strong>
              <NInput v-model:value="ownerSenderId" maxlength="128" placeholder="管理员真实 sender_id" />
              <NPopconfirm @positive-click="bindOwner">
                <template #trigger><NButton size="small" :disabled="!ownerSenderId.trim()" :loading="bindingOwner">登记管理员 sender_id</NButton></template>
                确认将此 sender_id 绑定为本账号的管理员身份？这只登记身份，不连接或发送。
              </NPopconfirm>
            </div>
            <div class="identity-form">
              <strong>普通联系人身份</strong>
              <NSelect v-model:value="contactPersonId" :options="contactOptions" placeholder="选择有效 chat-only 联系人" />
              <NInput v-model:value="contactSenderId" maxlength="128" placeholder="此联系人的真实 sender_id" />
              <NButton size="small" :disabled="!contactPersonId || !contactSenderId.trim()" :loading="bindingContact" @click="bindContact">登记联系人 sender_id</NButton>
            </div>
          </div>
          <div v-if="identities.length" class="identity-list">
            <article v-for="identity in identities" :key="identity.id" class="identity-row">
              <div><strong>{{ identity.is_owner ? '管理员' : people.find((person) => person.person_id === identity.person_id)?.display_name || '联系人' }}</strong><small>{{ identity.sender_id }} · {{ identity.person_id }}</small></div>
              <NTag round size="small" :type="identity.is_group ? 'warning' : 'info'">{{ identity.is_group ? '群身份' : identity.is_owner ? 'owner' : 'chat-only' }}</NTag>
            </article>
          </div>
          <NEmpty v-else-if="selectedAccountId && !identityError" description="这个账号还没有登记发送身份" />
          <NEmpty v-else-if="!selectedAccountId" description="请先登记或选择账号" />
        </div>
        <p class="settings-note">不会从联系人信息推断账号所有权，也不会在本机控制台自动登录或发送 QQ 消息。</p>
      </NCard>
    </section>
  </div>
</template>

<style scoped>
.person-list, .account-list { display: grid; gap: 10px; }
.person-create { display: grid; gap: 8px; margin-bottom: 14px; padding: 13px; border: 1px solid var(--border); border-radius: 13px; background: var(--surface-soft); }
.person-create small { color: var(--text-muted); font-size: 11px; }
.person-create__controls { display: flex; gap: 8px; }
.person-card { display: grid; gap: 12px; padding: 14px; border: 1px solid var(--border); border-radius: 14px; background: var(--surface); }
.person-card__top, .account-row, .person-card__meta { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.person-card__top > div, .account-row > div { display: grid; min-width: 0; gap: 3px; }
.person-card h3 { margin: 0; color: var(--text); font-size: 14px; }
.person-card small, .account-row small { overflow-wrap: anywhere; color: var(--text-muted); font-size: 11px; }
.person-card__meta { color: var(--text-muted); font-size: 12px; }
.person-card__meta strong, .account-row strong { color: var(--text); }
.person-card__actions { display: flex; flex-wrap: wrap; gap: 14px; }
.account-create-form, .identity-management { margin-top: 13px; padding: 13px; border: 1px solid var(--border); border-radius: 13px; background: var(--surface-soft); }
.account-create-form summary { color: var(--primary); font-size: 12px; font-weight: 700; cursor: pointer; }
.account-create-form__body, .identity-form { display: grid; gap: 8px; }
.account-create-form__body { margin-top: 11px; }
.identity-management__top { display: grid; grid-template-columns: minmax(0, 1fr) minmax(180px, 240px); align-items: center; gap: 10px; }
.identity-management__top > div { display: grid; gap: 3px; }
.identity-management h3 { margin: 0; color: var(--text); font-size: 13px; }
.identity-management__top small { color: var(--text-muted); font-size: 10px; }
.identity-forms { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
.identity-form { padding: 10px; border: 1px solid var(--border); border-radius: 11px; background: var(--surface); }
.identity-form > strong { color: var(--text); font-size: 11px; }
.qq-bridge-controls { display: grid; gap: 10px; grid-column: 1 / -1; }
.credential-form, .qq-enable-form { display: grid; gap: 8px; padding: 11px; border: 1px solid var(--border); border-radius: 11px; background: var(--surface); }
.credential-form > strong, .qq-enable-form strong { color: var(--text); font-size: 11px; }
.credential-form > small, .qq-enable-form small { color: var(--text-muted); font-size: 10px; }
.qq-enable-form > div { display: grid; gap: 3px; }
.identity-list { display: grid; gap: 8px; margin-top: 12px; }
.identity-row { display: flex; align-items: center; justify-content: space-between; gap: 9px; padding: 10px; border-radius: 10px; background: var(--surface); }
.identity-row > div { display: grid; min-width: 0; gap: 3px; }
.identity-row strong { color: var(--text); font-size: 11px; }
.identity-row small { overflow-wrap: anywhere; color: var(--text-muted); font-size: 10px; }
.account-row { padding: 12px 0; border-bottom: 1px solid var(--border); }
.account-row:last-child { border-bottom: 0; }
@media (max-width: 620px) { .person-card__top, .person-card__meta, .account-row { align-items: flex-start; flex-direction: column; } .person-create__controls, .identity-forms, .identity-management__top { grid-template-columns: 1fr; flex-direction: column; } }
</style>
