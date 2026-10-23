import { ApiRequestError } from './client'

export function errorMessage(error: unknown): string {
  if (error instanceof ApiRequestError) return error.message
  if (error instanceof Error) return error.message
  return '操作未完成，请查看本机服务状态后重试。'
}

export function requestReference(error: unknown): string | null {
  return error instanceof ApiRequestError ? error.requestId : null
}
