import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { authApi } from '../api/auth'
import { USE_MOCK } from '../api/config'
import { getSession, setSession, type Session } from './session'

interface AuthValue {
  session: Session | null
  ready: boolean // false, пока проверяем cookie-сессию на сервере
  signIn: (s: Session) => void
  update: (s: Session) => void // после смены пароля или согласия
  signOut: () => Promise<void>
  refresh: () => Promise<void>
  signedOut: boolean // вышли сами — после входа не возвращаем на прошлую страницу
}

const AuthContext = createContext<AuthValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setState] = useState<Session | null>(() => getSession())
  const [ready, setReady] = useState(USE_MOCK)
  const [signedOut, setSignedOut] = useState(false)
  const qc = useQueryClient()

  const refresh = useCallback(async () => {
    if (USE_MOCK) return
    try {
      const current = await authApi.me()
      setSession(current)
    } catch {
      // Сервер недоступен: оставляем текущее состояние, запросы покажут ошибку
    } finally {
      setReady(true)
    }
  }, [])

  // Реальный режим: восстанавливаем вход по httpOnly-cookie
  useEffect(() => { void refresh() }, [refresh])

  // Синхронизация, если сессию сбросил api-клиент (401) или сервер попросил перечитать /auth/me
  useEffect(() => {
    const sync = () => setState(getSession())
    const gate = () => { void refresh() }
    window.addEventListener('cq-session', sync)
    window.addEventListener('cq-auth-refresh', gate)
    return () => {
      window.removeEventListener('cq-session', sync)
      window.removeEventListener('cq-auth-refresh', gate)
    }
  }, [refresh])

  const signIn = useCallback((s: Session) => {
    qc.clear()
    setSignedOut(false)
    setSession(s)
  }, [qc])

  const update = useCallback((s: Session) => {
    setSession({ ...s, token: s.token ?? getSession()?.token })
  }, [])

  const signOut = useCallback(async () => {
    try { await authApi.logout() } catch { /* сессия на сервере истечёт сама */ }
    qc.clear()
    setSignedOut(true)
    setSession(null)
  }, [qc])

  return <AuthContext.Provider value={{ session, ready, signIn, update, signOut, refresh, signedOut }}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider')
  return ctx
}
