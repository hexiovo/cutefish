import { createRouter, createWebHistory } from 'vue-router'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/overview' },
    { path: '/overview', name: 'overview', component: () => import('./pages/OverviewPage.vue') },
    { path: '/modules', name: 'modules', component: () => import('./pages/ModulesPage.vue') },
    { path: '/modules/:moduleId', name: 'module-config', component: () => import('./pages/ModuleConfigPage.vue'), props: true },
    { path: '/people', name: 'people', component: () => import('./pages/PeoplePage.vue') },
    { path: '/chat', name: 'chat', component: () => import('./pages/ChatPage.vue') },
    { path: '/memory', name: 'memory', component: () => import('./pages/MemoryPage.vue') },
    { path: '/persona', name: 'persona', component: () => import('./pages/PersonaPage.vue') },
    { path: '/tasks', name: 'tasks', component: () => import('./pages/TasksPage.vue') },
    { path: '/outbox', name: 'outbox', component: () => import('./pages/OutboxPage.vue') },
    { path: '/runtime', name: 'runtime', component: () => import('./pages/RuntimePage.vue') },
    { path: '/budget', name: 'budget', component: () => import('./pages/BudgetPage.vue') },
    { path: '/rooms', name: 'rooms', component: () => import('./pages/RoomsPage.vue') },
    { path: '/voice', name: 'voice', component: () => import('./pages/VoicePage.vue') },
    { path: '/migration', name: 'migration', component: () => import('./pages/MigrationPage.vue') },
    { path: '/integrations', name: 'integrations', component: () => import('./pages/IntegrationsPage.vue') },
    { path: '/settings', name: 'settings', component: () => import('./pages/SettingsPage.vue') },
    { path: '/:pathMatch(.*)*', redirect: '/overview' },
  ],
  scrollBehavior: () => ({ top: 0 }),
})
