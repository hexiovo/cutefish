<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ status?: string | null; kind?: 'delivery' | 'runtime' | 'plain' }>()

const catalog: Record<string, { label: string; tone: string }> = {
  implemented: { label: '已实现', tone: 'info' },
  local_verified: { label: '本机已核验', tone: 'success' },
  target_pending: { label: '目标待核验', tone: 'warning' },
  production_disabled: { label: '正式运行停用', tone: 'muted' },
  not_started: { label: '未开始', tone: 'muted' },
  confirmed: { label: '需求已确认', tone: 'info' },
  passed: { label: '已核验通过', tone: 'success' },
  partial: { label: '部分完成', tone: 'warning' },
  not_implemented: { label: '未实现', tone: 'muted' },
  not_run: { label: '未运行', tone: 'muted' },
  target_verified: { label: '目标已核验', tone: 'success' },
  needs_model_verification: { label: '视觉模型未核验', tone: 'warning' },
  needs_resource: { label: '需要本地资源', tone: 'warning' },
  needs_adapter_verification: { label: '适配器待核验', tone: 'warning' },
  ready_local: { label: '本机已就绪', tone: 'success' },
  transcribed: { label: '已转写', tone: 'success' },
  submitted: { label: '已提交', tone: 'warning' },
  needs_outbox_support: { label: '等待 Outbox 支持', tone: 'warning' },
  budget_unavailable: { label: '预算不可用', tone: 'warning' },
  source_changed: { label: '来源已变化', tone: 'warning' },
  policy_changed: { label: '策略已变化', tone: 'warning' },
  duplicate: { label: '重复请求', tone: 'muted' },
  dnd: { label: '勿扰已启用', tone: 'warning' },
  manual_takeover: { label: '人工接管', tone: 'warning' },
  unsupported_action: { label: '动作不受支持', tone: 'muted' },
  disabled: { label: '已停用', tone: 'muted' },
  needs_configuration: { label: '需要配置', tone: 'warning' },
  not_checked: { label: '尚未读取', tone: 'muted' },
  created: { label: '已生成候选', tone: 'success' },
  pending: { label: '待确认', tone: 'warning' },
  cancelled: { label: '已取消', tone: 'muted' },
  not_connected: { label: '未连接', tone: 'warning' },
  needs_login: { label: '需要登录', tone: 'warning' },
  needs_credentials: { label: '需要凭据', tone: 'warning' },
  needs_connection: { label: '需要连接', tone: 'warning' },
  needs_authorization: { label: '需要授权', tone: 'warning' },
  needs_verification: { label: '待验证', tone: 'warning' },
  needs_input: { label: '需要输入', tone: 'warning' },
  starting: { label: '启动中', tone: 'info' },
  ready: { label: '已就绪', tone: 'success' },
  lazy_idle: { label: '按需待机', tone: 'info' },
  failed: { label: '失败', tone: 'danger' },
  draft: { label: '草稿', tone: 'muted' },
  prepared: { label: '待发送草稿', tone: 'info' },
  reserved: { label: '已预留', tone: 'info' },
  dispatched: { label: '已派发', tone: 'warning' },
  settled: { label: '已结算', tone: 'success' },
  released: { label: '已释放', tone: 'muted' },
  sending: { label: '发送中', tone: 'warning' },
  sent: { label: '已发送', tone: 'success' },
  running: { label: '运行中', tone: 'info' },
  active: { label: '进行中', tone: 'info' },
  open: { label: '待处理', tone: 'info' },
  busy: { label: '请求处理中', tone: 'warning' },
  paused: { label: '已暂停', tone: 'warning' },
  stopped: { label: '已停止', tone: 'muted' },
  taken_over: { label: '人工已接管', tone: 'warning' },
  needs_reconciliation: { label: '等待核对结果', tone: 'warning' },
  completed: { label: '已完成', tone: 'success' },
  expired: { label: '已到期', tone: 'warning' },
  enabled: { label: '已启用', tone: 'info' },
  configured: { label: '已登记', tone: 'info' },
  granted: { label: '已授权', tone: 'success' },
  revoked: { label: '已撤销授权', tone: 'warning' },
  not_dispatched: { label: '尚未派发', tone: 'muted' },
  proposed: { label: '候选待处理', tone: 'info' },
  superseded: { label: '已被新版本替代', tone: 'muted' },
  complete: { label: '已完成', tone: 'success' },
  dispatching: { label: '派发处理中', tone: 'warning' },
  review: { label: '待人工复核', tone: 'warning' },
  stale: { label: '已失效', tone: 'muted' },
  local_trial: { label: '本机小批量试用', tone: 'info' },
  unknown: { label: '状态未提供', tone: 'muted' },
}

const result = computed(() => {
  const key = (props.status ?? 'unknown').toLowerCase()
  return catalog[key] ?? { label: props.status || '状态未提供', tone: 'muted' }
})
</script>

<template>
  <span class="status-pill" :class="`status-pill--${result.tone}`" :data-state="status || 'unknown'">
    <span class="status-pill__dot" aria-hidden="true"></span>
    {{ result.label }}
  </span>
</template>
