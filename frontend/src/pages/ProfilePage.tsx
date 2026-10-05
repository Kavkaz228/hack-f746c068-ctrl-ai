import { useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import type { CompletionRequest, ProgressResult } from '../types'
import { completeAttempt, useCompleteActivity, useProfile, useRecommendations, useRejectActivity } from '../hooks/queries'
import { PromotionPanel } from '../components/profile/PromotionPanel'
import { formatDateTime } from '../components/ui/form'
import { ProfileHeader } from '../components/profile/ProfileHeader'
import { SkillGapChart } from '../components/profile/SkillGapChart'
import { GapList } from '../components/profile/GapList'
import { RecommendationCard } from '../components/recommendations/RecommendationCard'
import { DecisionLab } from '../components/recommendations/DecisionLab'
import { AiRecommendation } from '../components/recommendations/AiRecommendation'
import { CompleteModal } from '../components/progress/CompleteModal'
import { Badge, Card, EmptyState, ErrorState, Skeleton } from '../components/ui'
import { useAuth } from '../auth/AuthContext'
import { ApiError, createIdempotencyKey, USE_MOCK } from '../api/client'

export function ProfilePage() {
  const { id = '' } = useParams()
  const { session } = useAuth()
  const profile = useProfile(id)
  const recs = useRecommendations(id)
  const complete = useCompleteActivity(id)
  const reject = useRejectActivity(id)

  const [pending, setPending] = useState<{ aid: string; kind: 'complete' | 'skip' | 'decline' } | null>(null)
  const [result, setResult] = useState<ProgressResult | null>(null)
  const [sent, setSent] = useState<CompletionRequest | null>(null)
  const needsApproval = session?.role === 'employee' && session.completionMode === 'approval'
  const [highlight, setHighlight] = useState<string | null>(null)
  const [previewEventId, setPreviewEventId] = useState('')
  const completionKeys = useRef(new Map<string, string>())

  const onPreview = (eventId: string) => {
    setPreviewEventId(eventId)
    document.getElementById('decision-lab')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  const onComplete = (aid: string) => {
    const attempt = completeAttempt(aid, completionKeys.current.get(aid))
    completionKeys.current.set(aid, attempt.idempotencyKey)
    setPending({ aid, kind: 'complete' })
    complete.mutate(attempt, {
      onSuccess: (outcome) => {
        completionKeys.current.delete(aid)
        if (outcome.kind === 'pending') {
          setSent(outcome.request)
          return
        }
        setResult(outcome.result)
        setHighlight(outcome.result.changes[0]?.skill ?? null)
      },
      // При неизвестном transport-исходе сохраняем UUID и используем его при
      // ручном повторе; при определённом HTTP-ответе новая попытка получит новый ключ.
      onError: (error) => {
        const outcomeUnknown = error instanceof ApiError && (error.status === 0 || error.status >= 500)
        if (!outcomeUnknown) completionKeys.current.delete(aid)
      },
      onSettled: () => setPending(null),
    })
  }
  const onReject = (aid: string, action: 'skip' | 'decline') => {
    setPending({ aid, kind: action })
    reject.mutate({ aid, action, idempotencyKey: createIdempotencyKey() }, { onSettled: () => setPending(null) })
  }
  const waiting = new Set(profile.data?.pendingCompletions.map((item) => item.event_id))

  if (profile.error) return <ErrorState error={profile.error} onRetry={profile.refetch} />

  return (
    <div className="space-y-6">
      {session?.role === 'hr' && <Link to="/" className="inline-flex rounded-full bg-white/60 px-4 py-1.5 text-sm font-medium text-slate-600 ring-1 ring-black/5 backdrop-blur transition hover:bg-white hover:text-slate-900">← Все сотрудники</Link>}

      {profile.data ? <ProfileHeader profile={profile.data} /> : <Skeleton className="h-28" />}

      {session?.role === 'hr' && profile.data && <PromotionPanel profile={profile.data} />}

      {sent && (
        <p role="status" className="rounded-2xl bg-emerald-500/10 px-4 py-3 text-sm text-emerald-800 ring-1 ring-emerald-500/15">
          Заявка «{sent.event_title ?? sent.event_id}» отправлена HR. Навыки обновятся после подтверждения.
        </p>
      )}

      {profile.data && profile.data.pendingCompletions.length > 0 && (
        <Card className="!py-4">
          <p className="font-semibold text-slate-900">Ждут подтверждения HR</p>
          <ul className="mt-2 space-y-1 text-sm text-slate-600">
            {profile.data.pendingCompletions.map((item) => (
              <li key={item.request_id} className="flex flex-wrap items-center gap-2">
                <Badge tone="amber">на проверке</Badge>
                <span className="text-slate-800">{item.event_title ?? item.event_id}</span>
                <span className="text-xs text-slate-500">с {formatDateTime(item.requested_at)}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <div className="grid gap-6 lg:grid-cols-[1fr_22rem]">
        {profile.data ? (
          <SkillGapChart skills={profile.data.skills} nextGrade={profile.data.nextGrade} highlight={highlight} />
        ) : <Skeleton className="h-72" />}
        {profile.data ? <GapList gaps={profile.data.gaps} /> : <Skeleton className="h-72" />}
      </div>

      <section>
        <div className="mb-5 mt-4 flex items-baseline justify-between">
          <h2 className="text-3xl font-semibold tracking-tight text-slate-900">Рекомендации</h2>
          <span className="hidden text-xs text-slate-500 sm:inline">ранжирование и объяснение — прозрачные правила</span>
        </div>

        {recs.error && <ErrorState error={recs.error} onRetry={recs.refetch} />}
        {complete.error && <ErrorState error={complete.error} />}

        {!USE_MOCK && profile.data && (
          <AiRecommendation
            key={`${id}:${profile.dataUpdatedAt}`}
            employeeId={id}
            disabled={!!pending}
            onPreview={onPreview}
            onComplete={onComplete}
          />
        )}

        {recs.isLoading && (
          <div className="space-y-4">
            <p className="text-sm text-slate-500">Подбираем активности и формируем объяснения…</p>
            <Skeleton className="h-56" /><Skeleton className="h-40" />
          </div>
        )}

        {recs.data && recs.data.length === 0 && (
          <EmptyState
            title="Нет подходящих активностей"
            text={profile.data?.gaps.length
              ? 'По текущим разрывам в каталоге нет доступных активностей — сотрудник попадёт в HR-отчёт.'
              : 'Все разрывы закрыты.'}
          />
        )}

        <div className="space-y-4">
          {recs.data?.map((r, i) => (
            <RecommendationCard
              key={r.activityId}
              rec={r}
              rank={i + 1}
              busy={pending?.aid === r.activityId ? pending.kind : null}
              disabled={!!pending || waiting.has(r.activityId)}
              completeLabel={waiting.has(r.activityId) ? 'Ждёт подтверждения HR' : needsApproval ? 'Отправить на подтверждение' : undefined}
              onComplete={() => onComplete(r.activityId)}
              onPreview={() => onPreview(r.activityId)}
              onSkip={() => onReject(r.activityId, 'skip')}
              onDecline={() => onReject(r.activityId, 'decline')}
            />
          ))}
        </div>
      </section>

      {recs.data && recs.data.length > 0 && (
        <DecisionLab
          key={`${id}:${recs.data.map((item) => item.activityId).join(',')}`}
          employeeId={id}
          recommendations={recs.data}
          selectedEventId={previewEventId}
          onSelectEvent={setPreviewEventId}
        />
      )}

      <CompleteModal result={result} onClose={() => { setResult(null); setTimeout(() => setHighlight(null), 2500) }} />
    </div>
  )
}
