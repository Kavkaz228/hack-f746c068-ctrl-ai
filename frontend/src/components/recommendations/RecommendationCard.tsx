import { useState } from 'react'
import { USE_MOCK } from '../../api/client'
import type { Recommendation } from '../../types'
import { activityTypeLabel } from '../labels'
import { Badge, Button, Card, cn } from '../ui'
import { ScoreBreakdown } from './ScoreBreakdown'

interface Props {
  rec: Recommendation
  rank: number
  busy: 'complete' | 'skip' | 'decline' | null
  disabled: boolean
  onComplete: () => void
  onPreview: () => void
  onSkip?: () => void
  onDecline?: () => void
  completeLabel?: string
}

export function RecommendationCard({ rec, rank, busy, disabled, onComplete, onPreview, onSkip, onDecline, completeLabel }: Props) {
  const [open, setOpen] = useState(rank === 1)
  const after = Math.max(rec.currentLevel, Math.min(rec.currentLevel + rec.gain, rec.maxLevel))
  const capped = rec.currentLevel + rec.gain > rec.maxLevel
  const score = USE_MOCK ? String(Math.round(rec.score * 100)) : Number(rec.score.toFixed(3)).toString()
  const impacts = rec.impacts?.length ? rec.impacts : null

  return (
    <Card className={cn('animate-[fadein_.35s_ease-out]', rank === 1 && 'ring-2 ring-[#f1a400]/40')}>
      <div className="flex gap-4">
        <div className={cn('flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-sm font-semibold', rank === 1 ? 'bg-gradient-to-br from-[#ffc629] to-[#f1a400] text-slate-900 shadow-[inset_0_1px_0_rgba(255,255,255,0.5)]' : 'bg-white/70 text-slate-600 ring-1 ring-black/5')}>
          {rank}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0">
              <h3 className="text-lg font-semibold tracking-tight text-slate-900">{rec.title}</h3>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                <Badge tone="brand">{rec.skill}</Badge>
                <Badge>{activityTypeLabel[rec.type]}</Badge>
                <Badge>{rec.durationHours} ч</Badge>
              </div>
            </div>
            <div className="shrink-0 text-left sm:text-right">
              <p className="text-4xl font-semibold tracking-tight tabular-nums text-slate-900">{score}</p>
              <p className="text-xs text-slate-500">score</p>
            </div>
          </div>

          <div className="mt-4 grid gap-3 text-sm sm:grid-cols-[2fr_1fr]">
            <div className="tile px-4 py-2.5">
              <p className="text-xs text-slate-500">{impacts && impacts.length > 1 ? 'Изменение навыков' : 'Навык'}</p>
              {impacts ? (
                <ul className="mt-1 space-y-1.5">
                  {impacts.map((impact) => (
                    <li key={impact.skill} className="flex flex-wrap items-baseline justify-between gap-x-3 text-slate-800">
                      <span className="font-medium">
                        {impact.skill}{impact.mandatory && <span className="ml-1 text-xs font-normal text-amber-700">обязательный</span>}
                      </span>
                      <span className="tabular-nums">
                        {impact.current} → <span className="font-medium text-emerald-600">{impact.projected}</span>
                        <span className="ml-1 text-xs text-slate-500">(+{impact.gain}, cap {impact.maxLevel}; разрыв −{impact.gapReduction})</span>
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="font-medium text-slate-800">
                  {rec.skill}: {rec.currentLevel} → <span className="text-emerald-600">{after}</span>
                  <span className="ml-1 text-xs font-normal text-slate-500">(+{rec.gain}{capped ? `, cap ${rec.maxLevel}` : ''})</span>
                </p>
              )}
            </div>
            <div className="tile px-4 py-2.5">
              <p className="text-xs text-slate-500">Готовность после</p>
              <p className="font-medium text-emerald-600">{rec.readinessAfter}%</p>
            </div>
          </div>

          <div className="mt-4 rounded-2xl bg-brand-500/[0.07] p-4 ring-1 ring-brand-500/15">
            <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-brand-700">Почему это подходит</p>
            <p className="text-sm leading-relaxed text-slate-700">{rec.explanation}</p>
          </div>

          <button onClick={() => setOpen((v) => !v)} className="mt-3 text-sm font-medium text-brand-600 hover:underline">
            {open ? 'Скрыть расчёт score' : 'Как посчитан score?'}
          </button>
          {open && (
            <div className="mt-3">
              <ScoreBreakdown
                factors={rec.factors}
                score={rec.score}
                gradeGapBenefit={rec.gradeGapBenefit}
                historyMultiplier={rec.historyMultiplier}
              />
            </div>
          )}

          <div className="mt-4 flex flex-wrap gap-2">
            <Button variant="ghost" onClick={onPreview} disabled={disabled}>Что будет, если…</Button>
            <Button onClick={onComplete} loading={busy === 'complete'} disabled={disabled}>{completeLabel ?? 'Выполнить активность'}</Button>
            {onSkip && <Button variant="ghost" onClick={onSkip} loading={busy === 'skip'} disabled={disabled}>Пропустить</Button>}
            {onDecline && <Button variant="danger" onClick={onDecline} loading={busy === 'decline'} disabled={disabled}>Отказаться</Button>}
          </div>
        </div>
      </div>
    </Card>
  )
}
