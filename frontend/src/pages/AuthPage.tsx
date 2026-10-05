import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { authApi, type DirectoryItem } from '../api/auth'
import { USE_MOCK } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { homePath } from '../auth/guards'
import type { Role } from '../auth/session'
import { Button, cn } from '../components/ui'

type Mode = 'login' | 'register'

const DEMO = [
  { label: 'HR-менеджер', email: 'hr@careerquest.kz' },
  { label: 'Сотрудник', email: 'ivan.petrov@careerquest.kz' },
]

function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-sm font-medium text-slate-700">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-slate-500">{hint}</span>}
    </label>
  )
}

const inputCls =
  'w-full rounded-2xl border-0 bg-white/80 px-4 py-3.5 text-[15px] text-slate-900 outline-none ring-1 ring-black/[0.08] transition placeholder:text-slate-400 focus:bg-white focus:ring-2 focus:ring-brand-600/60'

function Segmented<T extends string>({ value, onChange, items }: { value: T; onChange: (v: T) => void; items: { value: T; label: string }[] }) {
  return (
    <div className="grid rounded-full bg-black/[0.05] p-1" style={{ gridTemplateColumns: `repeat(${items.length}, 1fr)` }}>
      {items.map((it) => (
        <button
          key={it.value}
          type="button"
          onClick={() => onChange(it.value)}
          className={cn(
            'rounded-full py-2 text-sm font-medium transition duration-200',
            value === it.value ? 'bg-white text-slate-900 shadow-[0_2px_8px_-2px_rgba(0,0,0,0.18)]' : 'text-slate-600 hover:text-slate-900',
          )}
        >
          {it.label}
        </button>
      ))}
    </div>
  )
}

export function AuthPage({ mode }: { mode: Mode }) {
  const navigate = useNavigate()
  const location = useLocation()
  const { session, signIn } = useAuth()
  const from = (location.state as { from?: string } | null)?.from

  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<Role>('employee')
  const [employeeId, setEmployeeId] = useState('')
  const [directory, setDirectory] = useState<DirectoryItem[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [showPass, setShowPass] = useState(false)

  // Уже вошли — сразу на главную
  useEffect(() => {
    if (session) navigate(from ?? homePath(session.role, session.employeeId), { replace: true })
  }, [session, from, navigate])

  useEffect(() => {
    setError(null)
    if (mode === 'register' && USE_MOCK && directory.length === 0) {
      authApi.directory().then(setDirectory).catch(() => setDirectory([]))
    }
  }, [mode, directory.length])

  const grouped = useMemo(() => {
    const m = new Map<string, DirectoryItem[]>()
    directory.forEach((d) => m.set(d.department, [...(m.get(d.department) ?? []), d]))
    return [...m.entries()].sort((a, b) => a[0].localeCompare(b[0], 'ru'))
  }, [directory])

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      let s
      if (!USE_MOCK || mode === 'login') s = await authApi.login(email.trim(), password)
      else s = await authApi.register({ name, email, password, role, employee_id: role === 'employee' ? employeeId : undefined })
      signIn(s)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Что-то пошло не так')
    } finally {
      setBusy(false)
    }
  }

  const switchMode = (m: Mode) => navigate(m === 'login' ? '/login' : '/register', { state: location.state, replace: true })

  return (
    <div className="flex min-h-screen items-center justify-center px-4 py-10">
      <div className="grid w-full max-w-5xl animate-[pop_.5s_cubic-bezier(.2,.8,.2,1)] overflow-hidden rounded-[2.5rem] shadow-[0_40px_100px_-40px_rgba(0,80,55,0.45)] lg:grid-cols-[1.05fr_1fr]">
        {/* Левая панель — бренд */}
        <section className="relative hidden overflow-hidden bg-gradient-to-br from-[#00815f] via-[#00946c] to-[#006a4e] p-10 text-white lg:flex lg:flex-col">
          <div className="pointer-events-none absolute -right-24 -top-24 h-80 w-80 rounded-full bg-[#f1a400]/40 blur-3xl" />
          <div className="pointer-events-none absolute -bottom-28 -left-16 h-80 w-80 rounded-full bg-[#ffd24d]/25 blur-3xl" />
          <div className="relative flex items-center gap-3">
            <span className="flex h-11 w-11 items-center justify-center rounded-2xl bg-white/15 text-sm font-bold ring-1 ring-white/30 backdrop-blur">CQ</span>
            <span className="text-lg font-semibold tracking-tight">Career Quest</span>
          </div>
          <div className="relative mt-auto">
            <h1 className="text-5xl font-semibold leading-[1.05] tracking-tightest">
              Карьера,<br />которая <span className="text-[#ffc629]">растёт</span><br />вместе с вами.
            </h1>
            <p className="mt-5 max-w-sm text-lg text-white/80">
              Персональные рекомендации, прозрачный путь к следующему грейду и аналитика для HR.
            </p>
            <div className="mt-8 grid max-w-sm grid-cols-3 gap-3">
              {(USE_MOCK
                ? [['44', 'сотрудника'], ['5', 'разделов'], ['24', 'активности']]
                : [['1–3', 'шага к грейду'], ['100%', 'объяснимый расчёт'], ['2', 'роли доступа']]).map(([n, l]) => (
                <div key={l} className="rounded-2xl bg-white/10 p-3 ring-1 ring-white/20 backdrop-blur">
                  <p className="text-2xl font-semibold">{n}</p>
                  <p className="text-xs text-white/70">{l}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* Правая панель — форма на стекле */}
        <section className="glass-strong p-7 sm:p-10">
          <div className="mb-8 flex items-center gap-3 lg:hidden">
            <span className="flex h-10 w-10 items-center justify-center rounded-2xl bg-gradient-to-br from-[#00815f] to-[#f1a400] text-sm font-bold text-white">CQ</span>
            <span className="text-lg font-semibold tracking-tight">Career Quest</span>
          </div>

          <h2 className="text-3xl font-semibold tracking-tight text-slate-900">
            {!USE_MOCK || mode === 'login' ? 'Вход' : 'Регистрация'}
          </h2>
          <p className="mt-2 text-slate-600">
            {!USE_MOCK || mode === 'login' ? 'Рады видеть вас снова.' : 'Создайте аккаунт, чтобы видеть свой путь развития.'}
          </p>

          {USE_MOCK && (
            <div className="mt-6">
              <Segmented
                value={mode}
                onChange={switchMode}
                items={[{ value: 'login', label: 'Вход' }, { value: 'register', label: 'Регистрация' }]}
              />
            </div>
          )}

          <form onSubmit={submit} className="mt-6 space-y-4">
            {(
              <>
                {USE_MOCK && mode === 'register' && (
                  <Field label="Имя и фамилия">
                    <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} placeholder="Например, Айгерим Сейткали" required autoComplete="name" />
                  </Field>
                )}
                <Field label="Рабочая почта">
                  <input className={inputCls} type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@company.kz" required autoComplete="email" autoFocus />
                </Field>
                <Field label="Пароль" hint={USE_MOCK && mode === 'register' ? 'Минимум 6 символов' : undefined}>
                  <div className="relative">
                    <input
                      className={cn(inputCls, 'pr-24')}
                      type={showPass ? 'text' : 'password'}
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      placeholder="••••••••"
                      required
                      minLength={USE_MOCK && mode === 'register' ? 6 : undefined}
                      autoComplete={!USE_MOCK || mode === 'login' ? 'current-password' : 'new-password'}
                    />
                    <button type="button" onClick={() => setShowPass((v) => !v)} className="absolute right-3 top-1/2 -translate-y-1/2 rounded-full px-3 py-1 text-xs font-medium text-slate-500 hover:bg-black/5">
                      {showPass ? 'Скрыть' : 'Показать'}
                    </button>
                  </div>
                </Field>

                {USE_MOCK && mode === 'register' && (
                  <>
                    <Field label="Кто вы">
                      <Segmented
                        value={role}
                        onChange={setRole}
                        items={[{ value: 'employee', label: 'Сотрудник' }, { value: 'hr', label: 'HR-менеджер' }]}
                      />
                    </Field>
                    {role === 'employee' && (
                      <Field label="Ваш профиль сотрудника" hint="Аккаунт будет привязан к этому профилю — вы увидите свои навыки и рекомендации.">
                        <select className={inputCls} value={employeeId} onChange={(e) => setEmployeeId(e.target.value)} required>
                          <option value="">Выберите себя из списка…</option>
                          {grouped.map(([dep, people]) => (
                            <optgroup key={dep} label={dep}>
                              {people.map((p) => <option key={p.id} value={p.id}>{p.name} — {p.role}</option>)}
                            </optgroup>
                          ))}
                        </select>
                      </Field>
                    )}
                  </>
                )}
              </>
            )}

            {error && (
              <p role="alert" className="rounded-2xl bg-rose-500/10 px-4 py-3 text-sm text-rose-700 ring-1 ring-rose-500/15">{error}</p>
            )}

            <Button type="submit" loading={busy} className="w-full !py-3.5 text-[15px]">
              {!USE_MOCK || mode === 'login' ? 'Войти' : 'Создать аккаунт'}
            </Button>
          </form>

          {USE_MOCK && mode === 'login' && (
            <div className="mt-8 rounded-3xl bg-[#f1a400]/10 p-4 ring-1 ring-[#f1a400]/25">
              <p className="text-sm font-semibold text-slate-800">Демо-доступ</p>
              <p className="mt-0.5 text-xs text-slate-600">Пароль для обоих аккаунтов: <b>demo1234</b></p>
              <div className="mt-3 flex flex-wrap gap-2">
                {DEMO.map((d) => (
                  <button
                    key={d.email}
                    type="button"
                    onClick={() => { setEmail(d.email); setPassword('demo1234') }}
                    className="rounded-full bg-white/80 px-3.5 py-1.5 text-sm font-medium text-slate-800 ring-1 ring-black/5 transition hover:bg-white"
                  >
                    {d.label}
                  </button>
                ))}
              </div>
            </div>
          )}

          {!USE_MOCK && (
            <p className="mt-6 rounded-2xl bg-black/[0.03] px-4 py-3 text-sm text-slate-600">
              Учётные записи выдаёт HR-администратор. Забыли пароль — обратитесь к нему: он выдаст временный
              пароль, который нужно сменить при первом входе.
            </p>
          )}

          <p className="mt-6 text-center text-xs text-slate-500">
            <Link to="/privacy" className="hover:underline">Политика обработки персональных данных</Link>
          </p>

          {USE_MOCK && (
            <p className="mt-4 text-center text-sm text-slate-600">
              {mode === 'login' ? 'Нет аккаунта? ' : 'Уже есть аккаунт? '}
              <Link to={mode === 'login' ? '/register' : '/login'} state={location.state} className="font-semibold text-brand-600 hover:underline">
                {mode === 'login' ? 'Зарегистрироваться' : 'Войти'}
              </Link>
            </p>
          )}
        </section>
      </div>
    </div>
  )
}
