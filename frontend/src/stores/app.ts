import { reactive } from 'vue'
import { ApiRequestError, api, setCsrfToken } from '../api/client'
import type {
  BudgetSnapshot,
  FeatureStatusData,
  HostSettings,
  ModuleItem,
  OverviewData,
  SessionData,
  TrialBatch,
  UiSettings,
} from '../api/types'

export interface Section<T> {
  data: T | null
  error: Error | null
  loading: boolean
}

function section<T>(): Section<T> {
  return { data: null, error: null, loading: false }
}

export const appState = reactive({
  authStatus: 'checking' as 'checking' | 'ready' | 'unavailable',
  authError: null as Error | null,
  setupRequired: false,
  authenticated: false,
  principal: null as SessionData['principal'],
  overview: section<OverviewData>(),
  modules: section<ModuleItem[]>(),
  features: section<FeatureStatusData>(),
  budgets: section<BudgetSnapshot>(),
  batches: section<TrialBatch[]>(),
  uiSettings: section<UiSettings>(),
  hostSettings: section<HostSettings>(),
  refreshing: false,
})

let panelRefresh: Promise<void> | null = null

function clearPanel() {
  appState.overview.data = null
  appState.modules.data = null
  appState.features.data = null
  appState.budgets.data = null
  appState.batches.data = null
  appState.uiSettings.data = null
  appState.hostSettings.data = null
}

async function loadSection<T>(target: Section<T>, loader: () => Promise<T>) {
  target.loading = true
  target.error = null
  try {
    target.data = await loader()
  } catch (error) {
    target.data = null
    target.error = error instanceof Error ? error : new Error('本机接口暂不可用。')
  } finally {
    target.loading = false
  }
}

export async function refreshPanel() {
  if (!appState.authenticated) return
  if (panelRefresh) return panelRefresh
  appState.refreshing = true
  panelRefresh = Promise.all([
    loadSection(appState.overview, api.overview),
    loadSection(appState.modules, async () => (await api.modules()).items),
    loadSection(appState.features, api.features),
    loadSection(appState.budgets, api.budgets),
    loadSection(appState.batches, async () => (await api.batches()).items),
    loadSection(appState.uiSettings, api.uiSettings),
    loadSection(appState.hostSettings, api.hostSettings),
  ]).then(() => undefined).finally(() => {
    appState.refreshing = false
    panelRefresh = null
  })
  return panelRefresh
}

export async function refreshAuthentication() {
  appState.authStatus = 'checking'
  appState.authError = null
  try {
    const bootstrap = await api.bootstrap()
    setCsrfToken(bootstrap.csrf_token)
    const session = await api.session()
    if (session.csrf_token) setCsrfToken(session.csrf_token)
    appState.setupRequired = session.setup_required ?? bootstrap.setup_required
    appState.authenticated = session.authenticated
    appState.principal = session.principal ?? null
    appState.authStatus = 'ready'
    if (session.authenticated) await refreshPanel()
    else clearPanel()
  } catch (error) {
    appState.authStatus = 'unavailable'
    appState.authError = error instanceof Error ? error : new Error('本机服务暂不可用。')
    appState.authenticated = false
    appState.principal = null
    clearPanel()
  }
}

export async function authenticate(password: string) {
  const result = appState.setupRequired ? await api.setup(password) : await api.login(password)
  if (result.csrf_token) setCsrfToken(result.csrf_token)
  const session = await api.session()
  if (session.csrf_token) setCsrfToken(session.csrf_token)
  appState.setupRequired = session.setup_required
  appState.authenticated = session.authenticated
  appState.principal = session.principal ?? null
  appState.authStatus = 'ready'
  if (!session.authenticated) {
    throw new ApiRequestError({ message: '本机服务没有建立管理员会话，请重新尝试。', code: 'needs_login' })
  }
  await refreshPanel()
}

export async function logout() {
  await api.logout()
  setCsrfToken(null)
  appState.authenticated = false
  appState.principal = null
  clearPanel()
  await refreshAuthentication()
}

