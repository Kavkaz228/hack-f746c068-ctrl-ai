import { useMemo, useState, type FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/endpoints'
import { USE_MOCK } from '../../api/client'
import { useAuth } from '../../auth/AuthContext'
import type { ManagedUser, UserWithPassword } from '../../types'
import { qk, useEmployees, useUsers } from '../../hooks/queries'
import { Badge, Button, Card, CardTitle, ErrorState, Modal, Skeleton } from '../../components/ui'
import { Alert, Field, formatDateTime, inputCls, Table } from '../../components/ui/form'
import { AdminHeader, MockNotice } from './AdminTabs'

function TemporaryPassword({ issued, onClose }: { issued: UserWithPassword | null; onClose: () => void }) {
  const [copied, setCopied] = useState(false)
  return (
    <Modal open={!!issued} onClose={onClose}>
      {issued && (
        <>
          <h2 className="text-lg font-semibold text-slate-900">Временный пароль</h2>
          <p className="mt-1 text-sm text-slate-600">
            Передайте его {issued.user.name} лично или по защищённому каналу. Пароль показывается один раз;
            при входе сотрудник задаст свой.
          </p>
          <div className="mt-4 flex items-center gap-2">
            <code className="flex-1 rounded-2xl bg-white px-4 py-3 font-mono text-lg tracking-wider text-slate-900 ring-1 ring-black/10">{issued.temporary_password}</code>
            <Button variant="ghost" onClick={() => { void navigator.clipboard?.writeText(issued.temporary_password).then(() => setCopied(true)) }}>
              {copied ? 'Скопировано' : 'Копировать'}
            </Button>
          </div>
          <p className="mt-3 text-xs text-slate-500">Логин: {issued.user.email}</p>
          <Button className="mt-5 w-full" onClick={() => { setCopied(false); onClose() }}>Готово</Button>
        </>
      )}
    </Modal>
  )
}

export function UsersPage() {
  const { session } = useAuth()
  const qc = useQueryClient()
  const users = useUsers()
  const employees = useEmployees()
  const [issued, setIssued] = useState<UserWithPassword | null>(null)
  const [form, setForm] = useState({ email: '', name: '', role: 'employee' as 'employee' | 'hr', employee_id: '' })
  const [filter, setFilter] = useState('')

  const refresh = () => qc.invalidateQueries({ queryKey: qk.users })
  const create = useMutation({
    mutationFn: () => api.createUser({ ...form, employee_id: form.role === 'employee' ? form.employee_id : null }),
    onSuccess: (result) => { setIssued(result); setForm({ email: '', name: '', role: 'employee', employee_id: '' }); void refresh() },
  })
  const reset = useMutation({ mutationFn: (userId: string) => api.resetPassword(userId), onSuccess: (r) => { setIssued(r); void refresh() } })
  const toggle = useMutation({ mutationFn: (u: ManagedUser) => api.setUserActive(u.user_id, !u.is_active), onSuccess: refresh })

  const linked = useMemo(() => new Set(users.data?.filter((u) => u.role === 'employee').map((u) => u.employee_id)), [users.data])
  const available = (employees.data ?? []).filter((e) => !linked.has(e.id))
  const shown = (users.data ?? []).filter((u) => !filter || `${u.name} ${u.email} ${u.employee_id ?? ''}`.toLowerCase().includes(filter.toLowerCase()))

  const pickEmployee = (id: string) => {
    const employee = employees.data?.find((e) => e.id === id)
    setForm((f) => ({ ...f, employee_id: id, name: f.name || employee?.name || '' }))
  }
  const submit = (e: FormEvent) => { e.preventDefault(); create.mutate() }

  return (
    <div className="space-y-6">
      <AdminHeader title="Учётные записи" subtitle="HR создаёт доступ сотрудникам и выдаёт временный пароль. Самостоятельной регистрации нет." />
      <MockNotice />
      {!USE_MOCK && (
        <>
          <Card>
            <CardTitle hint="пароль сгенерируется автоматически">Новая учётная запись</CardTitle>
            <form onSubmit={submit} className="grid gap-4 md:grid-cols-2">
              <Field label="Роль">
                <select className={inputCls} value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as 'employee' | 'hr' })}>
                  <option value="employee">Сотрудник</option>
                  <option value="hr">HR-менеджер</option>
                </select>
              </Field>
              {form.role === 'employee' && (
                <Field label="Профиль сотрудника" hint={`Без учётной записи: ${available.length}`}>
                  <select className={inputCls} value={form.employee_id} onChange={(e) => pickEmployee(e.target.value)} required>
                    <option value="">Выберите профиль…</option>
                    {available.map((e) => <option key={e.id} value={e.id}>{e.name} — {e.role} ({e.id})</option>)}
                  </select>
                </Field>
              )}
              <Field label="Имя и фамилия">
                <input className={inputCls} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required maxLength={255} />
              </Field>
              <Field label="Рабочая почта (логин)">
                <input className={inputCls} type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} required maxLength={254} />
              </Field>
              <div className="md:col-span-2 flex flex-wrap items-center gap-3">
                <Button type="submit" loading={create.isPending}>Создать и выдать пароль</Button>
                {create.error && <Alert>{create.error.message}</Alert>}
              </div>
            </form>
          </Card>

          <Card>
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <CardTitle>Все учётные записи</CardTitle>
              <input className={`${inputCls} max-w-xs !py-2`} placeholder="Поиск…" value={filter} onChange={(e) => setFilter(e.target.value)} />
            </div>
            {(reset.error || toggle.error) && <div className="mb-3"><Alert>{(reset.error ?? toggle.error)!.message}</Alert></div>}
            {users.error && <ErrorState error={users.error} onRetry={users.refetch} />}
            {users.isLoading && <Skeleton className="h-40" />}
            {users.data && (
              <Table head={['Пользователь', 'Роль', 'Профиль', 'Последний вход', 'Состояние', '']} empty={shown.length ? undefined : 'Никого не найдено'}>
                {shown.map((u) => (
                  <tr key={u.user_id}>
                    <td className="px-3 py-3"><p className="font-medium text-slate-900">{u.name}</p><p className="text-xs text-slate-500">{u.email}</p></td>
                    <td className="px-3 py-3">{u.role === 'hr' ? 'HR' : 'Сотрудник'}</td>
                    <td className="px-3 py-3 text-slate-600">{u.employee_id ?? '—'}</td>
                    <td className="px-3 py-3 text-slate-600">{formatDateTime(u.last_login_at)}</td>
                    <td className="space-x-1 px-3 py-3">
                      {!u.is_active ? <Badge tone="rose">отключена</Badge> : u.locked ? <Badge tone="amber">заблокирована</Badge> : <Badge tone="green">активна</Badge>}
                      {u.must_change_password && <Badge>временный пароль</Badge>}
                      {!u.consent_version && <Badge tone="amber">нет согласия</Badge>}
                    </td>
                    <td className="whitespace-nowrap px-3 py-3 text-right">
                      <Button variant="ghost" className="!px-3 !py-1.5" onClick={() => reset.mutate(u.user_id)} loading={reset.isPending && reset.variables === u.user_id}>Сбросить пароль</Button>
                      {u.user_id !== session?.accountId && (
                        <Button variant={u.is_active ? 'danger' : 'ghost'} className="!px-3 !py-1.5" onClick={() => toggle.mutate(u)}>
                          {u.is_active ? 'Отключить' : 'Включить'}
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </Table>
            )}
          </Card>
        </>
      )}
      <TemporaryPassword issued={issued} onClose={() => setIssued(null)} />
    </div>
  )
}
