import type {
  ActivityStatus,
  ActivityType,
  CompletionRequest,
  DecisionGaps,
  DecisionReadiness,
  DecisionRecommendation,
  DecisionRecommendations,
  EmployeeShort,
  HistoryItem,
  HrStats,
  Profile,
  ProgressResult,
  Recommendation,
} from '../types'

export interface BackendEmployeeSummary {
  employee_id: string
  name: string
  role: string
  grade: string
}

interface BackendEmployee extends BackendEmployeeSummary {
  tenure_months: number
  skills: Record<string, number>
}

interface BackendSkill {
  skill_id: string
  name: string
  category: string
  max_level: number
}

interface BackendHistoryEntry {
  record_id: string
  employee_id: string
  event_id: string
  status: ActivityStatus
  event_date: string
  event_title: string
  event_type: string
}

export interface BackendProfile {
  employee: BackendEmployee
  career_track: unknown
  history: BackendHistoryEntry[]
  skill_catalog: BackendSkill[]
  skill_gaps: DecisionGaps
  readiness: DecisionReadiness
  recommendations: DecisionRecommendations
  pending_completions?: CompletionRequest[]
}

// GET /hr/employees — весь список с готовностью одним запросом
export interface BackendEmployeeOverview extends BackendEmployeeSummary {
  target_grade: string | null
  readiness_percent: number | null
  mandatory_readiness_percent: number | null
  meets_mandatory: boolean | null
  skills_with_gap: number
  mandatory_skills_with_gap: number
  recommendation_status: string
  next_step: { event_id: string; title: string; score: number } | null
}

export interface BackendEvent {
  event_id: string
  title: string
  type: ActivityType
  category: string
  audience: string[]
  skills: { skill_id: string; gain: number; max_level: number }[]
  duration_hours: number
  active: boolean
}

export interface BackendCompletion {
  employee_id: string
  event_id: string
  record_id: string
  skill_changes: { skill_id: string; before: number; after: number; gain_applied: number }[]
  readiness_before: DecisionReadiness
  readiness_after: DecisionReadiness
  skill_gaps: DecisionGaps
  recommendations: DecisionRecommendations
}

interface BackendSkillGapSummary {
  skill_id: string
  skill_name: string
  employee_count: number
  mandatory_employee_count: number
  total_gap: number
}

interface BackendRoleSkillDeficit {
  role: string
  skill_id: string
  skill_name: string
  eligible_employees: number
  affected_employees: number
  average_gap_across_role: number
}

interface BackendEmployeeWithoutRecommendations {
  employee_id: string
  name: string
  role: string
  grade: string
  reason: 'top_grade' | 'no_skill_gaps' | 'no_matching_activity'
}

interface BackendActivityParticipation {
  event_id: string
  title: string
  active: boolean
  total_records: number
  unique_employees: number
  status_counts: Partial<Record<ActivityStatus, number>>
}

export interface BackendHrDashboard {
  total_employees: number
  average_readiness_percent: number | null
  top_skill_gaps: BackendSkillGapSummary[]
  role_skill_deficits: BackendRoleSkillDeficit[]
  employees_without_recommendations: BackendEmployeeWithoutRecommendations[]
  participation: Partial<Record<ActivityStatus, number>>
  activities: BackendActivityParticipation[]
}

const STATUSES: ActivityStatus[] = ['invited', 'enrolled', 'completed', 'skipped', 'declined']

const round = (value: number, digits = 1) => {
  const factor = 10 ** digits
  return Math.round(value * factor) / factor
}

const percent = (value: number | null) => value === null ? null : round(value)

// В API пока нет department. Роль — честный fallback и работает для импортированных ролей.
const departmentOf = (role: string) => role

const skillNameMap = (profile: BackendProfile) =>
  new Map(profile.skill_catalog.map((skill) => [skill.skill_id, skill.name]))

const displaySkill = (skillId: string, names: Map<string, string>) => names.get(skillId) ?? skillId

const normalizeStatusCounts = (counts: Partial<Record<ActivityStatus, number>>): Record<ActivityStatus, number> =>
  Object.fromEntries(STATUSES.map((status) => [status, counts[status] ?? 0])) as Record<ActivityStatus, number>

export function toProfile(raw: BackendProfile): Profile {
  const names = skillNameMap(raw)
  const skillIds = new Set([...Object.keys(raw.employee.skills), ...Object.keys(raw.readiness.skills)])
  return {
    id: raw.employee.employee_id,
    name: raw.employee.name,
    role: raw.employee.role,
    department: departmentOf(raw.employee.role),
    grade: raw.employee.grade,
    nextGrade: raw.readiness.target_grade,
    tenureMonths: raw.employee.tenure_months,
    skills: [...skillIds].map((skillId) => ({
      skill: displaySkill(skillId, names),
      current: raw.employee.skills[skillId] ?? raw.readiness.skills[skillId]?.current ?? 0,
      required: raw.readiness.skills[skillId]?.required ?? 0,
    })),
    gaps: Object.entries(raw.skill_gaps.gaps)
      .filter(([, gap]) => gap.gap > 0)
      .map(([skillId, gap]) => ({
        skill: displaySkill(skillId, names),
        current: gap.current,
        required: gap.required,
        gap: gap.gap,
        critical: gap.mandatory,
      })),
    readiness: percent(raw.readiness.readiness_percent) ?? 100,
    completedCount: raw.history.filter((item) => item.status === 'completed').length,
    meetsMandatory: raw.readiness.meets_mandatory,
    pendingCompletions: raw.pending_completions ?? [],
  }
}

export function fromOverview(raw: BackendEmployeeOverview): EmployeeShort {
  return {
    id: raw.employee_id,
    name: raw.name,
    role: raw.role,
    department: departmentOf(raw.role),
    grade: raw.grade,
    nextGrade: raw.target_grade,
    readiness: percent(raw.readiness_percent) ?? 100,
  }
}

export function toEmployeeShort(raw: BackendProfile): EmployeeShort {
  const profile = toProfile(raw)
  return {
    id: profile.id,
    name: profile.name,
    role: profile.role,
    department: profile.department,
    grade: profile.grade,
    nextGrade: profile.nextGrade,
    readiness: profile.readiness,
  }
}

function projectedReadiness(readiness: DecisionReadiness, recommendation: DecisionRecommendation) {
  if (readiness.readiness_percent === null) return 100
  const projected = new Map(recommendation.skill_impact.map((impact) => [impact.skill_id, impact.projected]))
  let weight = 0
  let earned = 0
  for (const [skillId, item] of Object.entries(readiness.skills)) {
    const current = projected.get(skillId) ?? item.current
    weight += item.importance
    earned += Math.min(current / item.required, 1) * item.importance
  }
  return weight > 0 ? round(100 * earned / weight) : percent(readiness.readiness_percent) ?? 100
}

export function toRecommendations(
  raw: DecisionRecommendations,
  readiness: DecisionReadiness,
  events: BackendEvent[],
  names: Map<string, string> = new Map(),
): Recommendation[] {
  const eventById = new Map(events.map((event) => [event.event_id, event]))
  return raw.recommendations.map((item) => {
    const event = eventById.get(item.event_id)
    const primary = [...item.skill_impact].sort((a, b) => b.benefit - a.benefit)[0]
    const fallbackSkillId = event?.skills[0]?.skill_id ?? 'skill'
    const primarySkillId = primary?.skill_id ?? fallbackSkillId
    const primaryEventSkill = event?.skills.find((skill) => skill.skill_id === primarySkillId)
    return {
      activityId: item.event_id,
      title: item.title,
      type: event?.type ?? 'project',
      skill: item.skill_impact.map((impact) => displaySkill(impact.skill_id, names)).join(', ')
        || displaySkill(primarySkillId, names),
      gain: primary?.gain ?? primaryEventSkill?.gain ?? 0,
      maxLevel: primary?.max_level ?? primaryEventSkill?.max_level ?? primary?.projected ?? 0,
      currentLevel: primary?.current ?? 0,
      durationHours: event?.duration_hours ?? 0,
      score: item.score,
      factors: item.skill_impact.map((impact) => ({
        key: impact.skill_id,
        name: `${displaySkill(impact.skill_id, names)}${impact.mandatory ? ' · обязательный' : ''}`,
        weight: impact.importance * impact.mandatory_factor,
        value: impact.required > 0 ? impact.gap_reduction / impact.required : 0,
      })),
      explanation: item.explanation,
      readinessAfter: projectedReadiness(readiness, item),
      impacts: item.skill_impact.map((impact) => ({
        skill: displaySkill(impact.skill_id, names),
        current: impact.current,
        required: impact.required,
        gain: impact.gain,
        maxLevel: impact.max_level,
        projected: impact.projected,
        gapReduction: impact.gap_reduction,
        mandatory: impact.mandatory,
      })),
      gradeGapBenefit: item.grade_gap_benefit,
      historyMultiplier: item.history_multiplier,
    }
  })
}

export function recommendationsFromProfile(raw: DecisionRecommendations, profile: BackendProfile, events: BackendEvent[]) {
  return toRecommendations(raw, profile.readiness, events, skillNameMap(profile))
}

export function toHistory(profile: BackendProfile, events: BackendEvent[]): HistoryItem[] {
  const names = skillNameMap(profile)
  const eventById = new Map(events.map((event) => [event.event_id, event]))
  return [...profile.history]
    .sort((a, b) => b.event_date.localeCompare(a.event_date) || b.record_id.localeCompare(a.record_id))
    .map((item) => {
      const event = eventById.get(item.event_id)
      const skills = event?.skills.map((skill) => displaySkill(skill.skill_id, names)).join(', ')
      return {
        activityId: item.event_id,
        title: item.event_title,
        skill: skills || item.event_type,
        status: item.status,
        date: item.event_date,
      }
    })
}

export function toProgress(
  raw: BackendCompletion,
  profile: BackendProfile,
  events: BackendEvent[],
): ProgressResult {
  const names = skillNameMap(profile)
  const event = events.find((item) => item.event_id === raw.event_id)
  return {
    changes: raw.skill_changes.map((change) => {
      const configured = event?.skills.find((skill) => skill.skill_id === change.skill_id)
      return {
        skill: displaySkill(change.skill_id, names),
        before: change.before,
        after: change.after,
        gain: configured?.gain ?? change.gain_applied,
        maxLevel: configured?.max_level ?? Math.max(change.before, change.after),
      }
    }),
    readinessBefore: percent(raw.readiness_before.readiness_percent),
    readinessAfter: percent(raw.readiness_after.readiness_percent),
    gradeUnlocked: raw.skill_gaps.status !== 'top_grade' && raw.skill_gaps.skills_with_gap === 0,
    newRecommendations: toRecommendations(raw.recommendations, raw.readiness_after, events, names),
  }
}

const WITHOUT_STEP_REASON: Record<BackendEmployeeWithoutRecommendations['reason'], string> = {
  top_grade: 'Последний грейд: следующая ступень не задана.',
  no_skill_gaps: 'Требования следующего грейда уже закрыты.',
  no_matching_activity: 'Нет активной подходящей активности для текущих разрывов.',
}

export function toHrStats(raw: BackendHrDashboard): HrStats {
  return {
    totalEmployees: raw.total_employees,
    avgReadiness: percent(raw.average_readiness_percent),
    topGaps: raw.top_skill_gaps.map((gap) => ({
      skill: gap.skill_name,
      employees: gap.employee_count,
      avgGap: gap.employee_count > 0 ? round(gap.total_gap / gap.employee_count, 2) : 0,
    })),
    statusCounts: normalizeStatusCounts(raw.participation),
    uncovered: raw.employees_without_recommendations.map((employee) => ({
      id: employee.employee_id,
      name: employee.name,
      role: employee.role,
      department: departmentOf(employee.role),
      grade: employee.grade,
      reason: WITHOUT_STEP_REASON[employee.reason],
    })),
    gapsByRole: raw.role_skill_deficits.map((gap) => ({
      role: gap.role,
      skill: gap.skill_name,
      avgGap: round(gap.average_gap_across_role, 2),
    })),
    activities: raw.activities.map((activity) => ({
      id: activity.event_id,
      title: activity.title,
      totalRecords: activity.total_records,
      uniqueEmployees: activity.unique_employees,
      statusCounts: normalizeStatusCounts(activity.status_counts),
    })),
  }
}
