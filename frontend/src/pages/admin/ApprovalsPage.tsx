import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { CompletionRequest } from '../../types'
import { useCompletionRequests, useDecideCompletion } from '../../hooks/queries'
import { Badge, Button, Card, ErrorState, Modal, Skeleton } from '../../components/ui'
import { Alert, Field, formatDateTime, inputCls, Table } from '../../components/ui/form'
import { AdminHeader, MockNotice } from './AdminTabs'
import { USE_MOCK } from '../../api/client'

const statusTone = { pending: 'amber', approved: 'green', rejected: 'rose', expired: 'slate' } as const
const statusText = { pending: 'ожидает', approved: 'подтверждено', rejected: 'отклонено', expired: 'устарело' }

export function ApprovalsPage() {
  const [scope, setScope] = useState<'pending' | 'all'>('pending')
  const requests = useCompletionRequests(scope, !USE_MOCK)
  const decide = useDecideCompletion()
  const [rejecting, setRejecting] = useState<CompletionRequest | null>(null)
  const [comment, setComment] = useState('')

  const approve = (item: CompletionRequest) =>
    decide.mutate({ requestId: item.request_id, approve: true, employeeId: item.employee_id })
  const reject = () => {
    if (!rejecting) return
    decide.mutate({ requestId: rejecting.request_id, approve: false, comment, employeeId: rejecting.employee_id },
      { onSuccess: () => { setRejecting(null); setComment('') } })
  }

  return (
    <div>
      <AdminHeader title="Подтверждение выполнения" subtitle="Навыки сотрудника меняются только после того, как HR подтвердит выполнение активности." />
      <MockNotice />
      {!USE_MOCK && (
        <Card>
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <Button variant={scope === 'pending' ? 'primary' : 'ghost'} onClick={() => setScope('pending')}>Ожидают решения</Button>
            <Button variant={scope === 'all' ? 'primary' : 'ghost'} onClick={() => setScope('all')}>Все заявки</Button>
          </div>
          {decide.error && <div className="mb-4"><Alert>{decide.error.message}</Alert></div>}
          {requests.error && <ErrorState error={requests.error} onRetry={requests.refetch} />}
          {requests.isLoading && <Skeleton className="h-40" />}
          {requests.data && (
            <Table head={['Сотрудник', 'Активность', 'Отправлено', 'Комментарий', 'Статус', '']}
                   empty={requests.data.length === 0 ? (scope === 'pending' ? 'Новых заявок нет' : 'Заявок пока не было') : undefined}>
              {requests.data.map((item) => (
                <tr key={item.request_id} className="align-top">
                  <td className="px-3 py-3">
                    <Link to={`/employee/${item.employee_id}`} className="font-medium text-slate-900 hover:underline">{item.employee_name ?? item.employee_id}</Link>
                    <p className="text-xs text-slate-500">{item.employee_id}</p>
                  </td>
                  <td className="px-3 py-3">{item.event_title ?? item.event_id}</td>
                  <td className="px-3 py-3 text-slate-600">{formatDateTime(item.requested_at)}</td>
                  <td className="max-w-[16rem] px-3 py-3 text-slate-600">{item.comment || '—'}{item.decision_comment && <p className="mt-1 text-xs text-slate-500">HR: {item.decision_comment}</p>}</td>
                  <td className="px-3 py-3"><Badge tone={statusTone[item.status]}>{statusText[item.status]}</Badge></td>
                  <td className="px-3 py-3 text-right">
                    {item.status === 'pending' && (
                      <div className="flex justify-end gap-2">
                        <Button onClick={() => approve(item)} loading={decide.isPending && decide.variables?.requestId === item.request_id && decide.variables.approve}>Подтвердить</Button>
                        <Button variant="danger" onClick={() => setRejecting(item)}>Отклонить</Button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </Table>
          )}
        </Card>
      )}
      <Modal open={!!rejecting} onClose={() => setRejecting(null)}>
        <h2 className="text-lg font-semibold text-slate-900">Отклонить заявку</h2>
        <p className="mt-1 text-sm text-slate-600">{rejecting?.employee_name} · {rejecting?.event_title}</p>
        <Field label="Причина (увидит сотрудник)" className="mt-4">
          <textarea className={inputCls} rows={3} maxLength={500} value={comment} onChange={(e) => setComment(e.target.value)} />
        </Field>
        <div className="mt-4 flex gap-2">
          <Button variant="danger" onClick={reject} loading={decide.isPending}>Отклонить</Button>
          <Button variant="ghost" onClick={() => setRejecting(null)}>Отмена</Button>
        </div>
      </Modal>
    </div>
  )
}
