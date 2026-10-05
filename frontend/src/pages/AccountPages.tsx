import { useState, type FormEvent } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { authApi } from '../api/auth'
import { useAuth } from '../auth/AuthContext'
import { homePath } from '../auth/guards'
import { CONSENT_SECTIONS } from '../legal/texts'
import { Button, Card, cn } from '../components/ui'
import { Alert, Field, inputCls } from '../components/ui/form'

function Shell({ title, subtitle, children }: { title: string; subtitle: string; children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center px-4 py-10">
      <Card className="w-full max-w-xl animate-[pop_.4s_cubic-bezier(.2,.8,.2,1)] !p-8">
        <div className="mb-6 flex items-center gap-3">
          <span className="flex h-10 w-10 items-center justify-center rounded-2xl bg-gradient-to-br from-[#00815f] to-[#f1a400] text-sm font-bold text-white">CQ</span>
          <span className="text-lg font-semibold tracking-tight">Career Quest</span>
        </div>
        <h1 className="text-2xl font-semibold tracking-tight text-slate-900">{title}</h1>
        <p className="mt-2 text-slate-600">{subtitle}</p>
        <div className="mt-6">{children}</div>
      </Card>
    </div>
  )
}

const rules = [
  { test: (v: string) => v.length >= 10, label: 'не короче 10 символов' },
  { test: (v: string) => /\p{L}/u.test(v) && /\d/.test(v), label: 'буквы и цифры' },
]

export function ChangePasswordPage() {
  const { session, update, signOut } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const forced = !!session?.mustChangePassword
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [repeat, setRepeat] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    if (next !== repeat) return setError('Новые пароли не совпадают')
    setBusy(true)
    try {
      const s = await authApi.changePassword(current, next)
      update(s)
      setDone(true)
      if (forced) {
        const from = (location.state as { from?: string } | null)?.from
        navigate(s.consentRequired ? '/consent' : from ?? homePath(s.role, s.employeeId), { replace: true })
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось сменить пароль')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Shell
      title={forced ? 'Задайте свой пароль' : 'Смена пароля'}
      subtitle={forced ? 'Вы вошли по временному паролю от HR. Придумайте постоянный — временный перестанет действовать.' : 'После смены пароля другие устройства выйдут из системы.'}
    >
      <form onSubmit={submit} className="space-y-4">
        <Field label={forced ? 'Временный пароль' : 'Текущий пароль'}>
          <input className={inputCls} type="password" value={current} onChange={(e) => setCurrent(e.target.value)} required autoComplete="current-password" autoFocus />
        </Field>
        <Field label="Новый пароль">
          <input className={inputCls} type="password" value={next} onChange={(e) => setNext(e.target.value)} required autoComplete="new-password" maxLength={128} />
        </Field>
        <ul className="flex flex-wrap gap-2 text-xs">
          {rules.map((rule) => (
            <li key={rule.label} className={cn('rounded-full px-2.5 py-1 ring-1', rule.test(next) ? 'bg-emerald-500/10 text-emerald-700 ring-emerald-500/15' : 'bg-white/70 text-slate-500 ring-black/5')}>
              {rule.test(next) ? '✓ ' : ''}{rule.label}
            </li>
          ))}
        </ul>
        <Field label="Повторите новый пароль">
          <input className={inputCls} type="password" value={repeat} onChange={(e) => setRepeat(e.target.value)} required autoComplete="new-password" maxLength={128} />
        </Field>
        {error && <Alert>{error}</Alert>}
        {done && !forced && <Alert tone="success">Пароль изменён.</Alert>}
        <div className="flex flex-wrap gap-2">
          <Button type="submit" loading={busy}>Сохранить пароль</Button>
          {forced
            ? <Button type="button" variant="ghost" onClick={() => { void signOut().then(() => navigate('/login', { replace: true })) }}>Выйти</Button>
            : <Button type="button" variant="ghost" onClick={() => navigate(-1)}>Назад</Button>}
        </div>
      </form>
    </Shell>
  )
}

export function ConsentPage() {
  const { session, update, signOut } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [checked, setChecked] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirmWithdraw, setConfirmWithdraw] = useState(false)
  const version = session?.consentVersion ?? ''
  const given = session ? !session.consentRequired : false

  const withdraw = async () => {
    setBusy(true)
    setError(null)
    try {
      update(await authApi.withdrawConsent())
      setConfirmWithdraw(false)
      setChecked(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось отозвать согласие')
    } finally {
      setBusy(false)
    }
  }

  const accept = async () => {
    setBusy(true)
    setError(null)
    try {
      const s = await authApi.giveConsent(version)
      update(s)
      const from = (location.state as { from?: string } | null)?.from
      navigate(from && from !== '/consent' ? from : homePath(s.role, s.employeeId), { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось сохранить согласие')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Shell title="Согласие на обработку персональных данных" subtitle="Прочитайте, какие данные использует Career Quest и зачем. Без согласия работа с системой невозможна.">
      <div className="max-h-[50vh] space-y-4 overflow-y-auto rounded-2xl bg-white/60 p-4 text-sm leading-relaxed text-slate-700 ring-1 ring-black/5">
        {CONSENT_SECTIONS.map((section) => (
          <section key={section.title}>
            <h2 className="font-semibold text-slate-900">{section.title}</h2>
            <ul className="mt-1 list-disc space-y-1 pl-5">{section.items.map((item) => <li key={item}>{item}</li>)}</ul>
          </section>
        ))}
        <p className="text-xs text-slate-500">Редакция {version}. Полный текст — в <Link to="/privacy" className="text-brand-600 hover:underline">политике обработки персональных данных</Link>.</p>
      </div>
      {given ? (
        <>
          <div className="mt-4"><Alert tone="success">Вы дали согласие (редакция {version}).</Alert></div>
          {error && <div className="mt-4"><Alert>{error}</Alert></div>}
          {confirmWithdraw ? (
            <div className="mt-4 space-y-3">
              <Alert tone="info">После отзыва доступ к профилю и рекомендациям закроется, пока вы снова не дадите согласие.</Alert>
              <div className="flex flex-wrap gap-2">
                <Button variant="danger" onClick={withdraw} loading={busy}>Да, отозвать</Button>
                <Button variant="ghost" onClick={() => setConfirmWithdraw(false)}>Оставить</Button>
              </div>
            </div>
          ) : (
            <div className="mt-5 flex flex-wrap gap-2">
              <Button variant="ghost" onClick={() => navigate(-1)}>Назад</Button>
              <Button variant="danger" onClick={() => setConfirmWithdraw(true)}>Отозвать согласие</Button>
            </div>
          )}
        </>
      ) : (
        <>
          <label className="mt-4 flex items-start gap-3 text-sm text-slate-700">
            <input type="checkbox" className="mt-0.5 h-4 w-4 accent-[#00815f]" checked={checked} onChange={(e) => setChecked(e.target.checked)} />
            Я, {session?.name}, даю согласие на обработку моих персональных данных на условиях, изложенных выше.
          </label>
          {error && <div className="mt-4"><Alert>{error}</Alert></div>}
          <div className="mt-5 flex flex-wrap gap-2">
            <Button onClick={accept} disabled={!checked} loading={busy}>Даю согласие</Button>
            <Button variant="ghost" onClick={() => { void signOut().then(() => navigate('/login', { replace: true })) }}>Не согласен, выйти</Button>
          </div>
        </>
      )}
    </Shell>
  )
}
