import type { Role, Session } from '../auth/session'
import { request, USE_MOCK } from './client'

// Ответ GET /auth/me и POST /auth/login (backend/api/auth_routes.py → Identity)
export interface Identity {
  account_id: string
  role: Role
  employee_id: string | null
  name: string
  email: string | null
  auth?: 'session' | 'token'
  must_change_password: boolean
  consent_required: boolean
  consent_version: string
  completion_mode?: 'instant' | 'approval'
  token?: string // только мок-режим
}

export interface DirectoryItem { id: string; name: string; role: string; department: string }

export const toSession = (r: Identity): Session => ({
  token: r.token,
  accountId: r.account_id,
  role: r.role,
  employeeId: r.employee_id,
  name: r.name,
  email: r.email,
  mustChangePassword: r.must_change_password,
  consentRequired: r.consent_required,
  consentVersion: r.consent_version,
  completionMode: r.completion_mode ?? 'instant',
})

const post = (body?: unknown): RequestInit => ({ method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })

export const authApi = {
  login: async (email: string, password: string) =>
    toSession(await request<Identity>('/auth/login', post({ email, password }))),

  // Регистрация есть только в мок-режиме: в рабочей системе учётные записи создаёт HR
  register: async (data: { name: string; email: string; password: string; role: Role; employee_id?: string }) =>
    toSession(await request<Identity>('/auth/register', post(data))),

  directory: () => request<DirectoryItem[]>('/auth/directory'),

  me: async (): Promise<Session | null> => {
    try {
      return toSession(await request<Identity>('/auth/me'))
    } catch (error) {
      if (error instanceof Error && 'status' in error && (error as { status: number }).status === 401) return null
      throw error
    }
  },

  logout: () => USE_MOCK ? Promise.resolve() : request<unknown>('/auth/logout', post()).then(() => undefined),

  changePassword: async (currentPassword: string, newPassword: string) =>
    toSession(await request<Identity>('/auth/password', post({ current_password: currentPassword, new_password: newPassword }))),

  giveConsent: async (version: string) =>
    toSession(await request<Identity>('/auth/consent', post({ version, accepted: true }))),

  withdrawConsent: async () => toSession(await request<Identity>('/auth/consent', { method: 'DELETE' })),
}
