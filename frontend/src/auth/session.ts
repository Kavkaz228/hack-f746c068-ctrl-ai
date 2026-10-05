// Состояние входа в браузере.
// Реальный режим: сессия живёт в httpOnly-cookie, которую JavaScript не видит;
// здесь держим только данные для интерфейса (имя, роль) и восстанавливаем их
// запросом GET /auth/me. Мок-режим: токен демо-аккаунта хранится в localStorage.
import { USE_MOCK } from '../api/config'

export type Role = 'employee' | 'hr'

export interface Session {
  token?: string // только мок-режим
  accountId: string
  role: Role
  employeeId: string | null
  name: string
  email?: string | null
  mustChangePassword: boolean
  consentRequired: boolean
  consentVersion: string
  completionMode: 'instant' | 'approval' // approval: выполнение подтверждает HR
}

const KEY = 'cq_session'
let memory: Session | null = null
let loaded = false

export function getSession(): Session | null {
  if (loaded || !USE_MOCK) return memory
  loaded = true
  try {
    const raw = localStorage.getItem(KEY)
    memory = raw ? (JSON.parse(raw) as Session) : null
  } catch {
    memory = null
  }
  return memory
}

export function setSession(s: Session | null) {
  memory = s
  loaded = true
  if (USE_MOCK) {
    try {
      if (s) localStorage.setItem(KEY, JSON.stringify(s))
      else localStorage.removeItem(KEY)
    } catch {
      /* хранилище недоступно — остаёмся в памяти */
    }
  } else {
    // Старые версии хранили токен в localStorage — удаляем его.
    try { localStorage.removeItem(KEY) } catch { /* ignore */ }
  }
  window.dispatchEvent(new Event('cq-session'))
}
