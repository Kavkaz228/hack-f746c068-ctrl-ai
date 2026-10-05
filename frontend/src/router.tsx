import { lazy, Suspense, type ReactNode } from 'react'
import { createBrowserRouter, Navigate } from 'react-router-dom'
import { AppLayout } from './components/layout/AppLayout'
import { RequireAuth, RequireOwnProfile, Splash } from './auth/guards'
import { AuthPage } from './pages/AuthPage'
import { USE_MOCK } from './api/config'
import { Skeleton } from './components/ui'

// Страницы грузятся по требованию: сотрудник не скачивает код HR-аналитики и админки
const named = <T extends string>(loader: () => Promise<Record<T, React.ComponentType>>, name: T) =>
  lazy(() => loader().then((module) => ({ default: module[name] })))

const EmployeesPage = named(() => import('./pages/EmployeesPage'), 'EmployeesPage')
const ProfilePage = named(() => import('./pages/ProfilePage'), 'ProfilePage')
const HistoryPage = named(() => import('./pages/HistoryPage'), 'HistoryPage')
const HrPage = named(() => import('./pages/HrPage'), 'HrPage')
const ApprovalsPage = named(() => import('./pages/admin/ApprovalsPage'), 'ApprovalsPage')
const UsersPage = named(() => import('./pages/admin/UsersPage'), 'UsersPage')
const DataPage = named(() => import('./pages/admin/DataPage'), 'DataPage')
const AuditPage = named(() => import('./pages/admin/AuditPage'), 'AuditPage')
const ChangePasswordPage = named(() => import('./pages/AccountPages'), 'ChangePasswordPage')
const ConsentPage = named(() => import('./pages/AccountPages'), 'ConsentPage')
const PrivacyPage = named(() => import('./pages/PrivacyPage'), 'PrivacyPage')

const page = (node: ReactNode) => <Suspense fallback={<div className="space-y-4"><Skeleton className="h-28" /><Skeleton className="h-72" /></div>}>{node}</Suspense>
const full = (node: ReactNode) => <Suspense fallback={<Splash />}>{node}</Suspense>
const hr = (node: ReactNode) => <RequireAuth role="hr">{page(node)}</RequireAuth>

export const router = createBrowserRouter([
  { path: '/login', element: <AuthPage mode="login" /> },
  // В рабочем режиме самостоятельной регистрации нет — доступ выдаёт HR
  { path: '/register', element: USE_MOCK ? <AuthPage mode="register" /> : <Navigate to="/login" replace /> },
  { path: '/privacy', element: full(<PrivacyPage />) },
  { path: '/account/password', element: <RequireAuth allowGate>{full(<ChangePasswordPage />)}</RequireAuth> },
  { path: '/consent', element: <RequireAuth allowGate>{full(<ConsentPage />)}</RequireAuth> },
  {
    element: <RequireAuth><AppLayout /></RequireAuth>,
    children: [
      // Список сотрудников и HR-аналитика — только для HR; сотрудник попадёт в свой профиль
      { path: '/', element: hr(<EmployeesPage />) },
      { path: '/hr', element: hr(<HrPage />) },
      { path: '/hr/approvals', element: hr(<ApprovalsPage />) },
      { path: '/hr/users', element: hr(<UsersPage />) },
      { path: '/hr/data', element: hr(<DataPage />) },
      { path: '/hr/audit', element: hr(<AuditPage />) },
      { path: '/employee/:id', element: <RequireOwnProfile>{page(<ProfilePage />)}</RequireOwnProfile> },
      { path: '/employee/:id/history', element: <RequireOwnProfile>{page(<HistoryPage />)}</RequireOwnProfile> },
    ],
  },
  { path: '*', element: <Navigate to="/" replace /> },
])
