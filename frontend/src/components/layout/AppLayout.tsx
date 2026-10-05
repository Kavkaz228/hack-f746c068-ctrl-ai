import { useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { USE_MOCK } from '../../api/client'
import { useAuth } from '../../auth/AuthContext'
import { useCompletionRequests } from '../../hooks/queries'
import { Avatar, cn } from '../ui'

const link = ({ isActive }: { isActive: boolean }) =>
  cn(
    'rounded-full px-4 py-1.5 text-sm font-medium transition duration-200',
    isActive ? 'bg-white text-slate-900 shadow-[0_2px_8px_-2px_rgba(0,0,0,0.15)]' : 'text-slate-600 hover:text-slate-900',
  )

function MenuItem({ children, onClick }: { children: React.ReactNode; onClick: () => void }) {
  return (
    <button role="menuitem" onClick={onClick} className="w-full rounded-2xl px-3 py-2 text-left text-sm text-slate-700 hover:bg-black/[0.04]">
      {children}
    </button>
  )
}

function UserMenu() {
  const { session, signOut } = useAuth()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false) }
    window.addEventListener('mousedown', close)
    return () => window.removeEventListener('mousedown', close)
  }, [open])

  if (!session) return null
  const go = (path: string) => { setOpen(false); navigate(path) }
  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 rounded-full py-1 pl-1 pr-3 transition hover:bg-black/[0.04]"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Меню пользователя: ${session.name}`}
      >
        <span className="scale-[0.8]"><Avatar name={session.name} /></span>
        <span className="hidden text-left leading-tight sm:block">
          <span className="block text-sm font-medium text-slate-900">{session.name}</span>
          <span className="block text-[11px] text-slate-500">{session.role === 'hr' ? 'HR-менеджер' : 'Сотрудник'}</span>
        </span>
      </button>
      {open && (
        <div role="menu" className="glass-strong absolute right-0 top-full mt-2 w-60 animate-[pop_.2s_ease-out] rounded-3xl p-2">
          <div className="px-3 py-2">
            <p className="truncate text-sm font-semibold text-slate-900">{session.name}</p>
            {session.email && <p className="truncate text-xs text-slate-500">{session.email}</p>}
          </div>
          {session.role === 'employee' && session.employeeId && (
            <MenuItem onClick={() => go(`/employee/${session.employeeId}/history`)}>Моя история</MenuItem>
          )}
          {!USE_MOCK && <MenuItem onClick={() => go('/account/password')}>Сменить пароль</MenuItem>}
          <MenuItem onClick={() => go('/consent')}>Согласие на обработку ПДн</MenuItem>
          <MenuItem onClick={() => go('/privacy')}>Политика конфиденциальности</MenuItem>
          <button role="menuitem" onClick={() => { setOpen(false); void signOut().then(() => navigate('/login', { replace: true })) }} className="w-full rounded-2xl px-3 py-2 text-left text-sm font-medium text-rose-600 hover:bg-rose-500/10">
            Выйти
          </button>
        </div>
      )}
    </div>
  )
}

export function AppLayout() {
  const { session } = useAuth()
  const isHr = session?.role === 'hr'
  const location = useLocation()
  const adminActive = /^\/hr\/(approvals|users|data|audit)/.test(location.pathname)
  const { data: pending } = useCompletionRequests('pending', isHr && !USE_MOCK)
  const pendingCount = pending?.length

  return (
    <div className="min-h-screen">
      {/* Плавающая стеклянная навигация */}
      <header className="sticky top-3 z-40 px-4">
        <div className="glass-strong mx-auto flex max-w-6xl items-center gap-2 rounded-full py-1.5 pl-3 pr-1.5">
          <NavLink to="/" className="mr-2 flex items-center gap-2.5 font-semibold tracking-tight text-slate-900">
            <span className="flex h-8 w-8 items-center justify-center rounded-full bg-gradient-to-br from-[#00815f] to-[#f1a400] text-xs font-bold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.5)]">
              CQ
            </span>
            <span className="hidden sm:inline">Career Quest</span>
          </NavLink>
          <nav className="flex gap-1 overflow-x-auto rounded-full bg-black/[0.04] p-1">
            {isHr ? (
              <>
                <NavLink to="/" end className={link}>Сотрудники</NavLink>
                <NavLink to="/hr" end className={link}>Аналитика</NavLink>
                <NavLink to="/hr/approvals" className={() => link({ isActive: adminActive })}>
                  Управление
                  {(pendingCount ?? 0) > 0 && (
                    <span className="ml-1.5 rounded-full bg-[#f1a400] px-1.5 py-0.5 text-[10px] font-semibold text-white" aria-label={`заявок: ${pendingCount}`}>{pendingCount}</span>
                  )}
                </NavLink>
              </>
            ) : (
              <NavLink to={`/employee/${session?.employeeId}`} end className={link}>Мой профиль</NavLink>
            )}
          </nav>
          <div className="ml-auto flex items-center gap-2">
            {USE_MOCK && (
              <span className="hidden items-center gap-1.5 rounded-full bg-white/70 px-3 py-1 text-xs font-medium text-slate-600 ring-1 ring-black/5 md:flex" title="VITE_USE_MOCK=true">
                <span className="h-1.5 w-1.5 rounded-full bg-[#f1a400]" /> Mock API
              </span>
            )}
            <UserMenu />
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 pb-20 pt-10">
        <Outlet />
      </main>
    </div>
  )
}
