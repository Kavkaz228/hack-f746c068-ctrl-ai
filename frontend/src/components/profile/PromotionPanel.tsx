import { useState } from 'react'
import type { Profile } from '../../types'
import { usePromote } from '../../hooks/queries'
import { Button, Card, Modal } from '../ui'
import { Alert, Field, inputCls } from '../ui/form'

// Только для HR: перевод на следующий грейд. Без закрытых обязательных навыков — с причиной.
export function PromotionPanel({ profile }: { profile: Profile }) {
  const promote = usePromote(profile.id)
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  if (!profile.nextGrade) return null
  const ready = profile.meetsMandatory === true
  // expected_grade: сервер откажет, если грейд уже изменили (двойной клик, устаревшая вкладка)
  const submit = () => promote.mutate(
    ready ? { expected_grade: profile.grade } : { force: true, reason, expected_grade: profile.grade },
    { onSuccess: () => { setOpen(false); setReason('') } },
  )

  return (
    <Card className="flex flex-wrap items-center gap-4 !py-4">
      <div className="min-w-0 flex-1">
        <p className="font-semibold text-slate-900">Решение по грейду</p>
        <p className="text-sm text-slate-600">
          {ready
            ? `Все обязательные требования ${profile.nextGrade} закрыты — можно перевести на следующий грейд.`
            : 'Обязательные требования следующего грейда ещё не закрыты. Повышение возможно только с обоснованием.'}
        </p>
        {promote.isSuccess && <p className="mt-1 text-sm font-medium text-emerald-700">Грейд обновлён: {promote.data.previous_grade} → {promote.data.new_grade}</p>}
      </div>
      <Button variant={ready ? 'primary' : 'ghost'} onClick={() => setOpen(true)}>Повысить до {profile.nextGrade}</Button>
      <Modal open={open} onClose={() => setOpen(false)}>
        <h2 className="text-lg font-semibold text-slate-900">Перевести {profile.name} на {profile.nextGrade}?</h2>
        <p className="mt-2 text-sm text-slate-600">Готовность {profile.readiness}%. Решение и его автор сохранятся в журнале.</p>
        {!ready && (
          <Field label="Обоснование (обязательно)" className="mt-4">
            <textarea className={inputCls} rows={3} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
          </Field>
        )}
        {promote.error && <div className="mt-3"><Alert>{promote.error.message}</Alert></div>}
        <div className="mt-5 flex gap-2">
          <Button onClick={submit} loading={promote.isPending} disabled={!ready && reason.trim().length < 5}>Подтвердить</Button>
          <Button variant="ghost" onClick={() => setOpen(false)}>Отмена</Button>
        </div>
      </Modal>
    </Card>
  )
}
