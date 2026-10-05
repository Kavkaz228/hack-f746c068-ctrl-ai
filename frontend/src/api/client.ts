import { API_URL, USE_MOCK } from './config'
import { getSession, setSession } from '../auth/session'

export { API_URL, USE_MOCK }

export class ApiError extends Error {
  status: number
  code?: string
  constructor(status: number, message: string, code?: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

export function createIdempotencyKey(): string {
  const cryptoApi = globalThis.crypto
  if (!cryptoApi) throw new ApiError(0, 'Браузер не поддерживает безопасную генерацию UUID')
  if (typeof cryptoApi.randomUUID === 'function') return cryptoApi.randomUUID()

  const bytes = cryptoApi.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

// Запасной путь для мок-режима: если Service Worker не управляет страницей
// (жёсткая перезагрузка Ctrl+F5, «Bypass for network» в DevTools, браузер без SW),
// прогоняем запрос через те же MSW-обработчики прямо в странице.
async function mockFetch(url: string, init?: RequestInit): Promise<Response> {
  const [{ getResponse }, { handlers }] = await Promise.all([import('msw'), import('./mock/handlers')])
  const absolute = new URL(url, window.location.origin).toString()
  const res = await getResponse(handlers, new Request(absolute, init))
  return res ?? new Response(JSON.stringify({ detail: `Нет мок-обработчика для ${url}` }), { status: 404 })
}

// Ответы, после которых интерфейс должен перечитать /auth/me (временный пароль, согласие)
const GATE_CODES = new Set(['password_change_required', 'consent_required'])

export interface RawResponse<T> { status: number; data: T; headers: Headers }

export async function requestRaw<T>(path: string, init?: RequestInit): Promise<RawResponse<T>> {
  const url = `${API_URL}${path}`
  const token = USE_MOCK ? getSession()?.token : undefined
  const headers = new Headers(init?.headers)
  if (token && !headers.has('Authorization')) headers.set('Authorization', `Bearer ${token}`)
  const isForm = typeof FormData !== 'undefined' && init?.body instanceof FormData
  if (init?.body !== undefined && !isForm && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (!headers.has('Accept')) headers.set('Accept', 'application/json')
  // Cookie сессии отправляется браузером автоматически (тот же origin); JS её не читает.
  const opts: RequestInit = { ...init, headers, credentials: 'same-origin' }
  const swActive = typeof navigator !== 'undefined' && !!navigator.serviceWorker?.controller

  let res: Response
  if (USE_MOCK && !swActive) {
    res = await mockFetch(url, opts)
  } else {
    try {
      res = await fetch(url, opts)
    } catch {
      // В мок-режиме запрос мог пройти мимо Service Worker — отвечаем моками прямо в странице
      if (USE_MOCK) res = await mockFetch(url, opts)
      else throw new ApiError(0, 'Сервер недоступен. Проверьте подключение и повторите попытку.')
    }
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({})) as { detail?: unknown; message?: unknown; error?: unknown }
    const code = typeof body.error === 'string' ? body.error : undefined
    // Сессия истекла или отозвана — сбрасываем её, охрана маршрутов отправит на /login
    if (res.status === 401 && !path.startsWith('/auth/') && getSession()) setSession(null)
    if (res.status === 403 && code && GATE_CODES.has(code)) window.dispatchEvent(new Event('cq-auth-refresh'))
    const nestedDetail = typeof body.detail === 'object' && body.detail !== null && 'message' in body.detail
      ? (body.detail as { message?: unknown }).message : undefined
    const message = typeof body.message === 'string' ? body.message
      : typeof body.detail === 'string' ? body.detail
        : typeof nestedDetail === 'string' ? nestedDetail : `Ошибка ${res.status}`
    throw new ApiError(res.status, message, code)
  }
  try {
    const data = res.status === 204 ? (undefined as T) : await res.json() as T
    return { status: res.status, data, headers: res.headers }
  } catch {
    // Для мутаций это неизвестный исход: сервер мог уже зафиксировать действие,
    // а тело ответа оборвалось. ApiError(0) заставит retry сохранить тот же UUID.
    throw new ApiError(0, 'Не удалось полностью прочитать ответ сервера')
  }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  return (await requestRaw<T>(path, init)).data
}

// Скачивание файла (экспорт) с той же авторизацией
export async function download(path: string, fallbackName: string) {
  const res = await fetch(`${API_URL}${path}`, { credentials: 'same-origin' })
  if (!res.ok) throw new ApiError(res.status, `Ошибка ${res.status}`)
  const disposition = res.headers.get('Content-Disposition') ?? ''
  const name = /filename="([^"]+)"/.exec(disposition)?.[1] ?? fallbackName
  const url = URL.createObjectURL(await res.blob())
  const link = document.createElement('a')
  link.href = url
  link.download = name
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
