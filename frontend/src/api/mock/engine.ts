// ============================================================
// Упрощённая копия логики бэкенда — только для мок-режима.
// Нужна, чтобы демо работало без FastAPI: скоринг, прогресс, HR-статистика.
// На реальном API всё это считает бэкенд, фронт только отображает.
// ============================================================
import type {
  ActivityComparison, ActivitySimulation, DecisionGaps, DecisionReadiness,
  DecisionRecommendation, DecisionRecommendations, EmployeeShort, Gap, HistoryItem,
  HrStats, Profile, ProgressResult, Recommendation, ScoreFactor,
} from '../../types'
import { activities, type MockActivity } from './data/events'
import { employeesSeed, type MockEmployee } from './data/employees'
import { departmentOfRole, nextGradeOf, roleRequirements } from './data/skills'

const dept = (role: string) => departmentOfRole[role] ?? 'Прочее'

// In-memory «база данных»: сбрасывается при перезагрузке страницы
let db: MockEmployee[] = structuredClone(employeesSeed)
export const resetDb = () => { db = structuredClone(employeesSeed) }

const WEIGHTS = {
  gapCoverage: 0.3,
  criticality: 0.2,
  readinessImpact: 0.15,
  effectiveGain: 0.15,
  historyFit: 0.1,
  roleFit: 0.1,
}

const FACTOR_NAMES: Record<keyof typeof WEIGHTS, string> = {
  gapCoverage: 'Закрывает разрыв',
  criticality: 'Критичность навыка',
  readinessImpact: 'Рост готовности к grade',
  effectiveGain: 'Эффективный gain (с учётом max_level)',
  historyFit: 'История участия',
  roleFit: 'Соответствие роли',
}

const getEmp = (id: string) => db.find((e) => e.id === id)
const getAct = (id: string) => activities.find((a) => a.id === id)!
const round = (n: number) => Math.round(n * 100) / 100
const applyGain = (before: number, act: MockActivity) => Math.max(before, Math.min(before + act.gain, act.maxLevel))

function requirements(emp: MockEmployee) {
  const next = nextGradeOf(emp.grade)
  return next ? roleRequirements[emp.role]?.[next] ?? {} : {}
}

function calcReadiness(skills: Record<string, number>, req: Record<string, number>) {
  const keys = Object.keys(req)
  if (!keys.length) return 100
  const sum = keys.reduce((s, k) => s + Math.min(skills[k] ?? 0, req[k]) / req[k], 0)
  return Math.round((sum / keys.length) * 100)
}

function calcGaps(emp: MockEmployee): Gap[] {
  const req = requirements(emp)
  return Object.entries(req)
    .map(([skill, required]) => {
      const current = emp.skills[skill] ?? 0
      const gap = Math.max(required - current, 0)
      return { skill, current, required, gap, critical: gap >= 2 }
    })
    .filter((g) => g.gap > 0)
    .sort((a, b) => b.gap - a.gap)
}

function explain(emp: MockEmployee, act: MockActivity, g: Gap, after: number, rBefore: number, rAfter: number, completedSameType: number) {
  const next = nextGradeOf(emp.grade)
  const parts = [
    `Для грейда ${next} ${emp.role} требуется ${act.skill} уровня ${g.required}, сейчас — ${g.current}.`,
    `Активность «${act.title}» повысит навык до ${after}${after === act.maxLevel && g.current + act.gain > act.maxLevel ? ` (упор в max_level = ${act.maxLevel})` : ''}.`,
  ]
  if (g.critical) parts.push('Это один из ключевых разрывов для следующего грейда.')
  if (after >= g.required) parts.push('После неё разрыв по этому навыку будет закрыт полностью.')
  if (completedSameType > 0) parts.push(`Сотрудник уже успешно завершил ${completedSameType} активност${completedSameType === 1 ? 'ь' : 'и'} такого формата.`)
  parts.push(`Готовность к ${next}: ${rBefore}% → ${rAfter}%.`)
  return parts.join(' ')
}

function recommend(emp: MockEmployee, limit = 3): Recommendation[] {
  const req = requirements(emp)
  const gaps = calcGaps(emp)
  const readiness = calcReadiness(emp.skills, req)
  const usedIds = new Set(emp.history.map((h) => h.activityId))

  const recs: Recommendation[] = []
  for (const act of activities) {
    if (usedIds.has(act.id)) continue
    const g = gaps.find((x) => x.skill === act.skill)
    if (!g) continue
    const roleOk = act.audience === 'all' || act.audience.includes(emp.role)
    if (!roleOk) continue

    const after = Math.min(g.current + act.gain, act.maxLevel)
    const effGain = after - g.current
    if (effGain <= 0) continue

    const rAfter = calcReadiness({ ...emp.skills, [act.skill]: after }, req)
    const sameType = emp.history.filter((h) => getAct(h.activityId).type === act.type)
    const completedSameType = sameType.filter((h) => h.status === 'completed').length
    const rejectedSameType = sameType.length - completedSameType

    const values: Record<keyof typeof WEIGHTS, number> = {
      gapCoverage: Math.min(effGain, g.gap) / g.gap,
      criticality: g.critical ? 1 : 0.4,
      readinessImpact: Math.min((rAfter - readiness) / 15, 1),
      effectiveGain: effGain / act.gain,
      historyFit: Math.max(0, Math.min(1, 0.5 + 0.25 * completedSameType - 0.25 * rejectedSameType)),
      roleFit: act.audience === 'all' ? 0.7 : 1,
    }
    const factors: ScoreFactor[] = (Object.keys(WEIGHTS) as (keyof typeof WEIGHTS)[]).map((k) => ({
      key: k, name: FACTOR_NAMES[k], weight: WEIGHTS[k], value: round(values[k]),
    }))
    const score = round(factors.reduce((s, f) => s + f.weight * f.value, 0))

    recs.push({
      activityId: act.id, title: act.title, type: act.type, skill: act.skill,
      gain: act.gain, maxLevel: act.maxLevel, currentLevel: g.current, durationHours: act.durationHours,
      score, factors, readinessAfter: rAfter,
      explanation: explain(emp, act, g, after, readiness, rAfter, completedSameType),
    })
  }
  return recs.sort((a, b) => b.score - a.score || a.activityId.localeCompare(b.activityId)).slice(0, limit)
}

// ---------------- Публичные функции (ответы «API») ----------------

export function listEmployees(): EmployeeShort[] {
  return db.map((e) => ({
    id: e.id, name: e.name, role: e.role, department: dept(e.role), grade: e.grade, nextGrade: nextGradeOf(e.grade),
    readiness: calcReadiness(e.skills, requirements(e)),
  }))
}

export function getProfile(id: string): Profile | null {
  const e = getEmp(id)
  if (!e) return null
  const req = requirements(e)
  const skillNames = Array.from(new Set([...Object.keys(req), ...Object.keys(e.skills)]))
  return {
    id: e.id, name: e.name, role: e.role, department: dept(e.role), grade: e.grade, nextGrade: nextGradeOf(e.grade),
    tenureMonths: e.tenureMonths,
    skills: skillNames.map((s) => ({ skill: s, current: e.skills[s] ?? 0, required: req[s] ?? 0 })),
    gaps: calcGaps(e),
    readiness: calcReadiness(e.skills, req),
    completedCount: e.history.filter((h) => h.status === 'completed').length,
    meetsMandatory: nextGradeOf(e.grade) ? calcGaps(e).every((g) => !g.critical) : null,
    pendingCompletions: [],
  }
}

// HR переводит на следующий грейд (в моках — без серверных проверок прав и журнала)
export function promote(id: string, force: boolean) {
  const e = getEmp(id)
  const next = e ? nextGradeOf(e.grade) : null
  if (!e || !next) return null
  if (!force && calcGaps(e).some((g) => g.critical)) return 'requirements_not_met' as const
  const previous = e.grade
  e.grade = next as typeof e.grade
  return { employee_id: id, previous_grade: previous, new_grade: next, forced: force }
}

export function getRecommendations(id: string): Recommendation[] {
  const e = getEmp(id)
  return e ? recommend(e) : []
}

// Адаптер новых ответов FastAPI для самостоятельного демо. Старый мок скоринга
// остаётся источником score; backend-поля decomposition здесь нейтральные,
// поэтому UI в mock mode показывает исходные факторы карточек, а не их.
function decisionReadiness(emp: MockEmployee): DecisionReadiness {
  const req = requirements(emp)
  const target = nextGradeOf(emp.grade)
  const percent = calcReadiness(emp.skills, req)
  return {
    employee_id: emp.id, role: emp.role, current_grade: emp.grade, target_grade: target,
    status: target ? 'calculated' : 'top_grade',
    readiness_percent: target ? percent : null,
    mandatory_readiness_percent: target ? percent : null,
    meets_mandatory: target ? Object.entries(req).every(([skill, required]) => (emp.skills[skill] ?? 0) >= required) : null,
    skills: Object.fromEntries(Object.entries(req).map(([skill, required]) => {
      const current = emp.skills[skill] ?? 0
      return [skill, {
        current, required, progress_percent: Math.min(current / required, 1) * 100,
        importance: 1, mandatory: true, fulfilled: current >= required,
      }]
    })),
  }
}

function decisionGaps(emp: MockEmployee): DecisionGaps {
  const req = requirements(emp)
  const target = nextGradeOf(emp.grade)
  const gaps: DecisionGaps['gaps'] = Object.fromEntries(Object.entries(req).map(([skill, required]) => {
    const current = emp.skills[skill] ?? 0
    const gap = Math.max(required - current, 0)
    return [skill, {
      current, required, gap, normalized_gap: gap / required,
      importance: 1, weighted_gap: gap / required,
      mandatory: true, fulfilled: gap === 0,
    }]
  }))
  const count = Object.values(gaps).filter((g) => g.gap > 0).length
  return {
    employee_id: emp.id, role: emp.role, current_grade: emp.grade, target_grade: target,
    status: target ? 'calculated' : 'top_grade',
    total_target_skills: Object.keys(gaps).length,
    fulfilled_target_skills: Object.keys(gaps).length - count,
    skills_with_gap: count, mandatory_skills_with_gap: count, gaps,
  }
}

function decisionRecommendation(emp: MockEmployee, rec: Recommendation): DecisionRecommendation {
  const req = requirements(emp)
  const required = req[rec.skill] ?? Math.max(rec.currentLevel, 1)
  const gap = Math.max(required - rec.currentLevel, 0)
  const projected = applyGain(rec.currentLevel, {
    id: rec.activityId, title: rec.title, type: rec.type, skill: rec.skill,
    gain: rec.gain, maxLevel: rec.maxLevel, durationHours: rec.durationHours, audience: 'all',
  })
  const gapReduction = Math.min(gap, projected - rec.currentLevel)
  const related = emp.history.filter((h) => activities.find((a) => a.id === h.activityId)?.skill === rec.skill)
  return {
    event_id: rec.activityId, title: rec.title, score: rec.score,
    grade_gap_benefit: rec.score, history_multiplier: 1,
    skill_impact: [{
      skill_id: rec.skill, current: rec.currentLevel, required, gap, gain: rec.gain,
      max_level: rec.maxLevel, projected, projected_gap: gap - gapReduction,
      gap_reduction: gapReduction, importance: 1, mandatory: true,
      weighted_gap_reduction: gapReduction / required, mandatory_factor: 1,
      benefit: gapReduction / required,
    }],
    participation: {
      completed: related.filter((h) => h.status === 'completed').length,
      skipped: related.filter((h) => h.status === 'skipped').length,
      declined: related.filter((h) => h.status === 'declined').length,
      same_event_completed: related.filter((h) => h.activityId === rec.activityId && h.status === 'completed').length,
    },
    explanation: rec.explanation,
  }
}

function decisionRecommendations(emp: MockEmployee, limit = 3): DecisionRecommendations {
  const recs = recommend(emp, limit)
  const target = nextGradeOf(emp.grade)
  return {
    employee_id: emp.id, role: emp.role, current_grade: emp.grade, target_grade: target,
    status: target ? (recs.length ? 'recommended' : calcGaps(emp).length ? 'no_matching_activity' : 'no_skill_gaps') : 'top_grade',
    recommendations: recs.map((rec) => decisionRecommendation(emp, rec)),
  }
}

export function getComparisonOptions(empId: string): DecisionRecommendations | null {
  const employee = getEmp(empId)
  return employee ? decisionRecommendations(employee, activities.length) : null
}

export function simulateActivity(empId: string, actId: string): ActivitySimulation | null {
  const employee = getEmp(empId)
  const act = activities.find((a) => a.id === actId)
  if (!employee || !act || (act.audience !== 'all' && !act.audience.includes(employee.role))) return null
  const before = employee.skills[act.skill] ?? 0
  const after = applyGain(before, act)
  const simulated = structuredClone(employee)
  simulated.skills[act.skill] = after
  simulated.history.push({ activityId: actId, status: 'completed', date: new Date().toISOString(), delta: { before, after } })
  return {
    employee_id: empId, event_id: actId, title: act.title, target_grade: nextGradeOf(employee.grade),
    skill_changes: [{ skill_id: act.skill, before, after, gain: act.gain, max_level: act.maxLevel, applied_gain: after - before }],
    readiness_before: decisionReadiness(employee), readiness_after: decisionReadiness(simulated),
    gaps_before: decisionGaps(employee), gaps_after: decisionGaps(simulated),
    recommendations_after: decisionRecommendations(simulated),
  }
}

export function compareActivities(empId: string, firstEventId: string, secondEventId: string): ActivityComparison | null {
  const employee = getEmp(empId)
  if (!employee || firstEventId === secondEventId) return null
  const candidates = recommend(employee, activities.length)
  const first = candidates.find((rec) => rec.activityId === firstEventId)
  const second = candidates.find((rec) => rec.activityId === secondEventId)
  const firstPreview = simulateActivity(empId, firstEventId)
  const secondPreview = simulateActivity(empId, secondEventId)
  if (!first || !second || !firstPreview || !secondPreview) return null
  const preferred = first.score === second.score
    ? (first.activityId.localeCompare(second.activityId) <= 0 ? first : second)
    : (first.score > second.score ? first : second)
  const delta = Math.abs(first.score - second.score)
  const differences = first.factors.map((factor) => {
    const other = second.factors.find((item) => item.key === factor.key)
    const firstContribution = factor.weight * factor.value
    const secondContribution = other ? other.weight * other.value : 0
    return { name: factor.name, firstContribution, secondContribution, difference: Math.abs(firstContribution - secondContribution) }
  }).sort((a, b) => b.difference - a.difference).slice(0, 2)
  const factorExplanation = differences.map((factor) =>
    `${factor.name}: ${factor.firstContribution.toFixed(2)} против ${factor.secondContribution.toFixed(2)}`,
  ).join('; ')
  return {
    employee_id: empId, target_grade: nextGradeOf(employee.grade),
    first: decisionRecommendation(employee, first),
    second: decisionRecommendation(employee, second),
    preferred_event_id: preferred.activityId, score_delta: delta,
    explanation: delta === 0
      ? `В демо-скоринге обе активности получили одинаковый score ${Math.round(first.score * 100)} из 100. Наибольшие различия во вкладах факторов (шкала 0–1): ${factorExplanation}. При равенстве технический порядок задан ID активности; сотрудник может выбрать любую из них.`
      : `В демо-скоринге «${preferred.title}» получила ${Math.round(preferred.score * 100)} из 100, альтернативная активность — ${Math.round((preferred === first ? second.score : first.score) * 100)} из 100. Наибольшие различия во вкладах факторов (шкала 0–1): ${factorExplanation}.`,
    readiness_after_first: firstPreview.readiness_after,
    readiness_after_second: secondPreview.readiness_after,
  }
}

export function getHistory(id: string): HistoryItem[] {
  const e = getEmp(id)
  if (!e) return []
  return [...e.history]
    .sort((a, b) => b.date.localeCompare(a.date))
    .map((h) => {
      const a = getAct(h.activityId)
      return { activityId: h.activityId, title: a.title, skill: a.skill, status: h.status, date: h.date, delta: h.delta }
    })
}

export function completeActivity(empId: string, actId: string): ProgressResult | null {
  const e = getEmp(empId)
  const act = activities.find((a) => a.id === actId)
  if (!e || !act) return null
  const req = requirements(e)
  const before = e.skills[act.skill] ?? 0
  const after = applyGain(before, act) // cap не должен понижать уже достигнутый уровень
  const readinessBefore = calcReadiness(e.skills, req)
  e.skills[act.skill] = after
  e.history.push({ activityId: actId, status: 'completed', date: new Date().toISOString(), delta: { before, after } })
  const readinessAfter = calcReadiness(e.skills, req)
  return {
    changes: [{ skill: act.skill, before, after, maxLevel: act.maxLevel, gain: act.gain }],
    readinessBefore, readinessAfter, gradeUnlocked: readinessAfter >= 100,
    newRecommendations: recommend(e),
  }
}

export function rejectActivity(empId: string, actId: string, status: 'skipped' | 'declined') {
  const e = getEmp(empId)
  if (!e) return false
  e.history.push({ activityId: actId, status, date: new Date().toISOString() })
  return true
}

export function getHrStats(): HrStats {
  const gapAgg = new Map<string, { employees: number; total: number }>()
  const roleAgg = new Map<string, { total: number; n: number }>()
  const statusCounts: HrStats['statusCounts'] = { invited: 0, enrolled: 0, completed: 0, skipped: 0, declined: 0 }
  const uncovered: HrStats['uncovered'] = []
  let readinessSum = 0

  for (const e of db) {
    const gaps = calcGaps(e)
    readinessSum += calcReadiness(e.skills, requirements(e))
    for (const g of gaps) {
      const cur = gapAgg.get(g.skill) ?? { employees: 0, total: 0 }
      gapAgg.set(g.skill, { employees: cur.employees + 1, total: cur.total + g.gap })
    }
    for (const [skill, required] of Object.entries(requirements(e))) {
      const key = `${e.role}|${skill}`
      const cur = roleAgg.get(key) ?? { total: 0, n: 0 }
      roleAgg.set(key, { total: cur.total + Math.max(required - (e.skills[skill] ?? 0), 0), n: cur.n + 1 })
    }
    for (const h of e.history) statusCounts[h.status]++
    if (gaps.length && recommend(e).length === 0) {
      uncovered.push({
        id: e.id, name: e.name, role: e.role, department: dept(e.role), grade: e.grade,
        reason: `Нет доступных активностей по разрывам: ${gaps.map((g) => g.skill).join(', ')}`,
      })
    }
  }

  return {
    totalEmployees: db.length,
    avgReadiness: Math.round(readinessSum / db.length),
    topGaps: [...gapAgg.entries()]
      .map(([skill, v]) => ({ skill, employees: v.employees, avgGap: round(v.total / v.employees) }))
      .sort((a, b) => b.employees - a.employees || b.avgGap - a.avgGap)
      .slice(0, 8),
    statusCounts,
    uncovered,
    gapsByRole: [...roleAgg.entries()].map(([k, v]) => {
      const [role, skill] = k.split('|')
      return { role, skill, avgGap: round(v.total / v.n) }
    }),
    activities: activities.map((activity) => {
      const participants = db.flatMap((employee) => employee.history
        .filter((item) => item.activityId === activity.id)
        .map((item) => ({ employeeId: employee.id, status: item.status })))
      const counts: HrStats['statusCounts'] = { invited: 0, enrolled: 0, completed: 0, skipped: 0, declined: 0 }
      participants.forEach((item) => { counts[item.status]++ })
      return {
        id: activity.id,
        title: activity.title,
        totalRecords: participants.length,
        uniqueEmployees: new Set(participants.map((item) => item.employeeId)).size,
        statusCounts: counts,
      }
    }),
  }
}
