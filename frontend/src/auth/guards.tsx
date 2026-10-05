import type { ReactNode } from 'react'
import { Navigate, useLocation, useParams } from 'react-router-dom'
import { useAuth } from './AuthContext'
import type { Role } from './session'

export function Splash() {
  return (
    <div className="flex min-h-screen items-center justify-center" role="status" aria-live="polite">
      <span className="h-6 w-6 animate-spin rounded-full border-2 border-brand-600 border-t-transparent" />
      <span className="sr-only">Загрузка…</span>
    </div>
  )
}

// Пускает только авторизованных; временный пароль и согласие на обработку ПДн — раньше всего остального
export function RequireAuth({ children, role, allowGate }: { children: ReactNode; role?: Role; allowGate?: boolean }) {
  const { session, ready, signedOut } = useAuth()
  const location = useLocation()
  if (!ready) return <Splash />
  if (!session) return <Navigate to="/login" replace state={signedOut ? undefined : { from: location.pathname + location.search }} />
  if (!allowGate) {
    if (session.mustChangePassword) return <Navigate to="/account/password" replace state={{ from: location.pathname }} />
    if (session.consentRequired) return <Navigate to="/consent" replace state={{ from: location.pathname }} />
  }
  if (role && session.role !== role) return <Navigate to={homePath(session.role, session.employeeId)} replace />
  return <>{children}</>
}

// Сотрудник видит только свой профиль и историю
export function RequireOwnProfile({ children }: { children: ReactNode }) {
  const { session } = useAuth()
  const { id } = useParams()
  if (session && session.role === 'employee' && session.employeeId !== id) {
    return <Navigate to={homePath('employee', session.employeeId)} replace />
  }
  return <>{children}</>
}

export function homePath(role: Role, employeeId: string | null) {
  return role === 'hr' ? '/' : `/employee/${employeeId}`
}
