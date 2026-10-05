import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/endpoints'
import { USE_MOCK } from '../../api/client'
import type { DatasetSnapshot, ImportSummary } from '../../types'
import { useInvalidateAll, useSnapshots } from '../../hooks/queries'
import { Button, Card, CardTitle, ErrorState, Modal, Skeleton, cn } from '../../components/ui'
import { Alert, Field, formatDateTime, inputCls, Table } from '../../components/ui/form'
import { AdminHeader, MockNotice } from './AdminTabs'

const FILES = [
  { name: 'employees', label: 'Сотрудники', accept: '.json,application/json', hint: 'employees.json' },
  { name: 'events', label: 'Активности', accept: '.json,application/json', hint: 'events.json' },
  { name: 'skills', label: 'Навыки и треки', accept: '.json,application/json', hint: 'skills.json — только при замене' },
  { name: 'activity_history', label: 'История', accept: '.csv,text/csv', hint: 'activity_history.csv' },
] as const

const reasonText: Record<string, string> = {
  before_import_append: 'перед дозагрузкой',
  before_import_replace: 'перед заменой',
  before_restore: 'перед восстановлением',
  manual: 'вручную',
}

function Summary({ result }: { result: ImportSummary }) {
  return (
    <div className="tile p-4 text-sm text-slate-700">
      <p className="font-semibold text-slate-900">{result.dry_run ? 'Проверка пройдена — данные не изменены' : 'Импорт выполнен'}</p>
      <p className="mt-1">Итого: сотрудников {result.employees}, активностей {result.events}, навыков {result.skills}, записей истории {result.history_records}.</p>
      {result.changes && (
        <p className="mt-1 text-slate-600">
          Изменения: сотрудники +{result.changes.employees.added}/−{result.changes.employees.removed},
          активности +{result.changes.events.added}/−{result.changes.events.removed},
          история +{result.changes.activity_history.added}/−{result.changes.activity_history.removed}
          {result.skills_changed ? ', справочник навыков изменится' : ''}.
        </p>
      )}
    </div>
  )
}

export function DataPage() {
  const qc = useQueryClient()
  const invalidateAll = useInvalidateAll()
  const snapshots = useSnapshots()
  const formRef = useRef<HTMLFormElement>(null)
  const [mode, setMode] = useState<'append' | 'replace'>('append')
  const [result, setResult] = useState<ImportSummary | null>(null)
  const [restoring, setRestoring] = useState<DatasetSnapshot | null>(null)

  const upload = useMutation({
    mutationFn: (dryRun: boolean) => {
      const form = new FormData(formRef.current!)
      form.set('mode', mode)
      form.set('dry_run', String(dryRun))
      for (const file of FILES) {
        const value = form.get(file.name)
        if (value instanceof File && value.size === 0) form.delete(file.name)
      }
      return api.importDataset(form)
    },
    onSuccess: (r) => { setResult(r); if (!r.dry_run) { void invalidateAll(); formRef.current?.reset() } },
  })
  const snapshot = useMutation({ mutationFn: api.createSnapshot, onSuccess: () => qc.invalidateQueries({ queryKey: ['snapshots'] }) })
  const restore = useMutation({
    mutationFn: (id: number) => api.restoreSnapshot(id),
    onSuccess: () => { setRestoring(null); void invalidateAll() },
  })
  const exporter = useMutation({ mutationFn: api.exportDataset })

  return (
    <div className="space-y-6">
      <AdminHeader title="Данные" subtitle="Загрузка профилей и истории в формате стартового набора, снимки и выгрузка." />
      <MockNotice />
      {!USE_MOCK && (
        <>
          <Card>
            <CardTitle hint="до 5 МБ на файл">Импорт JSON/CSV</CardTitle>
            <form ref={formRef} onSubmit={(e) => { e.preventDefault(); upload.mutate(false) }} className="space-y-4">
              <div className="flex flex-wrap gap-2">
                {(['append', 'replace'] as const).map((value) => (
                  <button key={value} type="button" onClick={() => setMode(value)}
                    className={cn('rounded-full px-4 py-1.5 text-sm font-medium transition', mode === value ? 'bg-slate-900 text-white' : 'bg-white/70 text-slate-700 ring-1 ring-black/5')}>
                    {value === 'append' ? 'Дозагрузить новых' : 'Заменить всё'}
                  </button>
                ))}
              </div>
              <p className="text-sm text-slate-600">
                {mode === 'append'
                  ? 'Добавляет новых сотрудников, активности и записи истории. Существующие ID не перезаписываются.'
                  : 'Полностью заменяет данные: нужны все четыре файла. Прогресс и неподтверждённые заявки сбросятся; текущее состояние сохранится в снимок.'}
              </p>
              <div className="grid gap-4 md:grid-cols-2">
                {FILES.filter((f) => mode === 'replace' || f.name !== 'skills').map((file) => (
                  <Field key={file.name} label={file.label} hint={file.hint}>
                    <input type="file" name={file.name} accept={file.accept} className={`${inputCls} file:mr-3 file:rounded-full file:border-0 file:bg-brand-600 file:px-3 file:py-1 file:text-white`} />
                  </Field>
                ))}
              </div>
              {upload.error && <Alert>{upload.error.message}</Alert>}
              {result && <Summary result={result} />}
              <div className="flex flex-wrap gap-2">
                <Button type="button" variant="ghost" onClick={() => upload.mutate(true)} loading={upload.isPending && upload.variables}>Проверить без записи</Button>
                <Button type="submit" loading={upload.isPending && !upload.variables}>{mode === 'append' ? 'Импортировать' : 'Заменить данные'}</Button>
              </div>
            </form>
          </Card>

          <Card>
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <CardTitle hint="создаются автоматически перед импортом">Снимки данных</CardTitle>
              <div className="flex gap-2">
                <Button variant="ghost" onClick={() => snapshot.mutate()} loading={snapshot.isPending}>Создать снимок</Button>
                <Button variant="ghost" onClick={() => exporter.mutate()} loading={exporter.isPending}>Скачать JSON</Button>
              </div>
            </div>
            {(snapshot.error || exporter.error) && <div className="mb-3"><Alert>{(snapshot.error ?? exporter.error)!.message}</Alert></div>}
            {snapshots.error && <ErrorState error={snapshots.error} onRetry={snapshots.refetch} />}
            {snapshots.isLoading && <Skeleton className="h-32" />}
            {snapshots.data && (
              <Table head={['Когда', 'Причина', 'Сотрудники', 'Активности', 'История', '']} empty={snapshots.data.length ? undefined : 'Снимков пока нет'}>
                {snapshots.data.map((item) => (
                  <tr key={item.snapshot_id}>
                    <td className="px-3 py-3">{formatDateTime(item.created_at)}</td>
                    <td className="px-3 py-3 text-slate-600">{reasonText[item.reason] ?? item.reason}</td>
                    <td className="px-3 py-3 tabular-nums">{item.employees}</td>
                    <td className="px-3 py-3 tabular-nums">{item.events}</td>
                    <td className="px-3 py-3 tabular-nums">{item.history_records}</td>
                    <td className="px-3 py-3 text-right"><Button variant="ghost" className="!px-3 !py-1.5" onClick={() => setRestoring(item)}>Восстановить</Button></td>
                  </tr>
                ))}
              </Table>
            )}
          </Card>
        </>
      )}
      <Modal open={!!restoring} onClose={() => setRestoring(null)}>
        <h2 className="text-lg font-semibold text-slate-900">Восстановить снимок?</h2>
        <p className="mt-2 text-sm text-slate-600">
          Данные будут заменены состоянием от {formatDateTime(restoring?.created_at)}. Текущее состояние сначала
          сохранится в новый снимок, поэтому действие можно отменить.
        </p>
        {restore.error && <div className="mt-3"><Alert>{restore.error.message}</Alert></div>}
        <div className="mt-5 flex gap-2">
          <Button onClick={() => restoring && restore.mutate(restoring.snapshot_id)} loading={restore.isPending}>Восстановить</Button>
          <Button variant="ghost" onClick={() => setRestoring(null)}>Отмена</Button>
        </div>
      </Modal>
    </div>
  )
}
