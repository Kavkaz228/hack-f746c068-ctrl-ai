import type {
  ActivityComparison,
  ActivitySimulation,
  AuditEntry,
  CompletionOutcome,
  CompletionRequest,
  DatasetSnapshot,
  DecisionRecommendations,
  EmployeeShort,
  HistoryItem,
  HrStats,
  ImportSummary,
  ManagedUser,
  Profile,
  ProgressResult,
  PromotionResult,
  Recommendation,
  UserWithPassword,
} from '../types'
import {
  fromOverview,
  recommendationsFromProfile,
  toHistory,
  toHrStats,
  toProfile,
  toProgress,
  type BackendCompletion,
  type BackendEmployeeOverview,
  type BackendEvent,
  type BackendHrDashboard,
  type BackendProfile,
} from './adapters'
import { ApiError, createIdempotencyKey, download, request, requestRaw, USE_MOCK } from './client'

const employeePath = (id: string) => `/employees/${encodeURIComponent(id)}`
const activityPath = (employeeId: string, eventId: string) =>
  `${employeePath(employeeId)}/activities/${encodeURIComponent(eventId)}`

const getBackendProfile = (id: string) => request<BackendProfile>(employeePath(id))
const getBackendEvents = () => request<BackendEvent[]>('/events')
const post = (body?: unknown, headers?: HeadersInit): RequestInit =>
  ({ method: 'POST', headers, body: body === undefined ? undefined : JSON.stringify(body) })

// Админ-разделы работают только с сервером: в мок-режиме нет учётных записей и журнала
const serverOnly = () => new ApiError(501, 'Раздел доступен при работе с сервером (VITE_USE_MOCK=false)', 'mock_mode')

export const api = {
  getEmployees: async (): Promise<EmployeeShort[]> => {
    if (USE_MOCK) return request<EmployeeShort[]>('/employees')
    // Один запрос вместо профиля на каждого сотрудника: сервер считает готовность и кэширует её
    return (await request<BackendEmployeeOverview[]>('/hr/employees')).map(fromOverview)
  },

  getProfile: async (id: string): Promise<Profile> => {
    if (USE_MOCK) return request<Profile>(`${employeePath(id)}/profile`)
    return toProfile(await getBackendProfile(id))
  },

  getRecommendations: async (id: string): Promise<Recommendation[]> => {
    if (USE_MOCK) return request<Recommendation[]>(`${employeePath(id)}/recommendations`)
    const [recommendations, profile, events] = await Promise.all([
      request<DecisionRecommendations>(`${employeePath(id)}/recommendations`),
      getBackendProfile(id),
      getBackendEvents(),
    ])
    return recommendationsFromProfile(recommendations, profile, events)
  },

  getComparisonOptions: (id: string) =>
    request<DecisionRecommendations>(`${employeePath(id)}/comparison-options`),

  simulateActivity: (id: string, eventId: string) =>
    request<ActivitySimulation>(`${employeePath(id)}/simulate/${encodeURIComponent(eventId)}`),

  compareActivities: (id: string, firstEventId: string, secondEventId: string) =>
    request<ActivityComparison>(
      `${employeePath(id)}/compare?first_event_id=${encodeURIComponent(firstEventId)}&second_event_id=${encodeURIComponent(secondEventId)}`,
    ),

  getHistory: async (id: string): Promise<HistoryItem[]> => {
    if (USE_MOCK) return request<HistoryItem[]>(`${employeePath(id)}/history`)
    const [profile, events] = await Promise.all([getBackendProfile(id), getBackendEvents()])
    return toHistory(profile, events)
  },

  completeActivity: async (id: string, eventId: string, idempotencyKey: string): Promise<CompletionOutcome> => {
    const path = `${activityPath(id, eventId)}/complete`
    const init = post(undefined, { 'Idempotency-Key': idempotencyKey })
    if (USE_MOCK) return { kind: 'applied', result: await request<ProgressResult>(path, init) }

    // Метаданные нужны для названий навыков и configured gain/max_level, которых
    // CompletionResponse намеренно не дублирует. Загружаем их до мутации.
    const [profile, events] = await Promise.all([getBackendProfile(id), getBackendEvents()])
    const response = await requestRaw<BackendCompletion | { status: 'pending_approval'; request: CompletionRequest }>(path, init)
    if (response.status === 202 && 'request' in response.data) return { kind: 'pending', request: response.data.request }
    return { kind: 'applied', result: toProgress(response.data as BackendCompletion, profile, events) }
  },

  skipActivity: (id: string, eventId: string, idempotencyKey = createIdempotencyKey()) =>
    request<unknown>(`${activityPath(id, eventId)}/skip`, post(undefined, { 'Idempotency-Key': idempotencyKey })),

  declineActivity: (id: string, eventId: string, idempotencyKey = createIdempotencyKey()) =>
    request<unknown>(`${activityPath(id, eventId)}/decline`, post(undefined, { 'Idempotency-Key': idempotencyKey })),

  promote: (id: string, body: { force?: boolean; reason?: string; expected_grade?: string },
    idempotencyKey = createIdempotencyKey()) =>
    request<PromotionResult>(`${employeePath(id)}/promote`, post(body, { 'Idempotency-Key': idempotencyKey })),

  getHrStats: async (): Promise<HrStats> => {
    if (USE_MOCK) return request<HrStats>('/hr/stats')
    return toHrStats(await request<BackendHrDashboard>('/hr/dashboard'))
  },

  // ---------- HR: заявки на подтверждение ----------
  listCompletionRequests: (status: 'pending' | 'all' = 'pending') =>
    USE_MOCK ? Promise.resolve<CompletionRequest[]>([])
      : request<CompletionRequest[]>(`/completion-requests?status=${status}`),

  decideCompletion: (requestId: string, approve: boolean, comment?: string) =>
    request<{ request: CompletionRequest }>(
      `/completion-requests/${encodeURIComponent(requestId)}/${approve ? 'approve' : 'reject'}`, post({ comment: comment || null })),

  // ---------- HR: учётные записи ----------
  listUsers: () => USE_MOCK ? Promise.reject(serverOnly()) : request<ManagedUser[]>('/users'),
  createUser: (body: { email: string; name: string; role: 'employee' | 'hr'; employee_id?: string | null }) =>
    request<UserWithPassword>('/users', post(body)),
  resetPassword: (userId: string) => request<UserWithPassword>(`/users/${encodeURIComponent(userId)}/reset-password`, post()),
  setUserActive: (userId: string, active: boolean) =>
    request<ManagedUser>(`/users/${encodeURIComponent(userId)}/${active ? 'enable' : 'disable'}`, post()),

  // ---------- HR: данные ----------
  importDataset: (form: FormData) => request<ImportSummary>('/dataset/import', { method: 'POST', body: form }),
  listSnapshots: () => USE_MOCK ? Promise.reject(serverOnly()) : request<DatasetSnapshot[]>('/dataset/snapshots'),
  createSnapshot: () => request<DatasetSnapshot>('/dataset/snapshots', post()),
  restoreSnapshot: (snapshotId: number) =>
    request<ImportSummary>(`/dataset/snapshots/${snapshotId}/restore`, post()),
  exportDataset: () => download('/dataset/export', 'career-quest-export.json'),

  // ---------- HR: журнал ----------
  listAudit: (params: { beforeId?: number; action?: string } = {}) => {
    if (USE_MOCK) return Promise.reject(serverOnly())
    const query = new URLSearchParams({ limit: '100' })
    if (params.beforeId) query.set('before_id', String(params.beforeId))
    if (params.action) query.set('action', params.action)
    return request<AuditEntry[]>(`/audit?${query}`)
  },
}
