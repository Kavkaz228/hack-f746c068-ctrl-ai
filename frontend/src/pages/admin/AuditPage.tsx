import { useState } from 'react'
import { useInfiniteQuery } from '@tanstack/react-query'
import { api } from '../../api/endpoints'
import { USE_MOCK } from '../../api/client'
import { Button, Card, ErrorState, Skeleton } from '../../components/ui'
import { formatDateTime, inputCls, Table } from '../../components/ui/form'
import { AdminHeader, MockNotice } from './AdminTabs'

const ACTIONS: Record<string, string> = {
  login_succeeded: 'Вход', login_failed: 'Неудачный вход', password_changed: 'Смена пароля',
  password_reset: 'Сброс пароля', consent_given: 'Согласие на ПДн', consent_withdrawn: 'Отзыв согласия',
  user_created: 'Создана учётная запись', user_disabled: 'Учётная запись отключена', user_enabled: 'Учётная запись включена',
  profile_viewed: 'Просмотр профиля', activity_completed: 'Активность выполнена', activity_skipped: 'Пропуск',
  activity_declined: 'Отказ', completion_requested: 'Заявка на подтверждение', completion_approved: 'Заявка подтверждена',
  completion_rejected: 'Заявка отклонена', employee_promoted: 'Повышение грейда', dataset_import_append: 'Дозагрузка данных',
  dataset_import_replace: 'Замена данных', snapshot_created: 'Снимок', snapshot_restored: 'Восстановление снимка',
  dataset_exported: 'Выгрузка данных', ai_recommendation: 'AI-подсказка', dataset_seeded: 'Начальные данные',
}

export function AuditPage() {
  const [action, setAction] = useState('')
  const audit = useInfiniteQuery({
    queryKey: ['audit', action],
    queryFn: ({ pageParam }) => api.listAudit({ action: action || undefined, beforeId: pageParam }),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (last) => (last.length === 100 ? last[last.length - 1].id : undefined),
    enabled: !USE_MOCK,
    retry: false,
  })
  const rows = audit.data?.pages.flat() ?? []

  return (
    <div>
      <AdminHeader title="Журнал действий" subtitle="Кто и когда входил, смотрел профили, менял данные и права. Записи нельзя изменить из интерфейса." />
      <MockNotice />
      {!USE_MOCK && (
        <Card>
          <div className="mb-4">
            <select className={`${inputCls} max-w-xs !py-2`} value={action} onChange={(e) => setAction(e.target.value)}>
              <option value="">Все действия</option>
              {Object.entries(ACTIONS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
            </select>
          </div>
          {audit.error && <ErrorState error={audit.error} onRetry={audit.refetch} />}
          {audit.isLoading && <Skeleton className="h-40" />}
          {audit.data && (
            <Table head={['Время', 'Действие', 'Кто', 'Объект', 'Детали', 'IP']} empty={rows.length ? undefined : 'Записей нет'}>
              {rows.map((row) => (
                <tr key={row.id} className="align-top">
                  <td className="whitespace-nowrap px-3 py-2 text-slate-600">{formatDateTime(row.at)}</td>
                  <td className="px-3 py-2 font-medium text-slate-900">{ACTIONS[row.action] ?? row.action}</td>
                  <td className="px-3 py-2 text-slate-600">{row.actor_role ? `${row.actor_role} · ` : ''}<span className="font-mono text-xs">{row.actor_id?.slice(0, 12) ?? '—'}</span></td>
                  <td className="px-3 py-2 font-mono text-xs text-slate-600">{row.target ?? '—'}</td>
                  <td className="max-w-[18rem] px-3 py-2 font-mono text-xs text-slate-500">{row.details ? JSON.stringify(row.details) : ''}</td>
                  <td className="px-3 py-2 text-xs text-slate-500">{row.ip ?? ''}</td>
                </tr>
              ))}
            </Table>
          )}
          {audit.hasNextPage && (
            <Button variant="ghost" className="mt-4" onClick={() => audit.fetchNextPage()} loading={audit.isFetchingNextPage}>Показать ещё</Button>
          )}
        </Card>
      )}
    </div>
  )
}
