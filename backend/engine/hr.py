"""Aggregate validated employee data for an HR-only dashboard."""

from collections import Counter, defaultdict
from typing import Literal

from pydantic import Field

from backend.engine.grade_progress import GradeReadinessResult, calculate_readiness_from_gaps
from backend.engine.recommendation import Recommendation, get_recommendations
from backend.engine.skill_gap import SkillGapResult, get_employee_skill_gaps
from backend.models import Activity, Dataset, Employee, HistoryRecord, Model, SkillsDocument


class ParticipationCounts(Model):
    invited: int = Field(default=0, ge=0)
    enrolled: int = Field(default=0, ge=0)
    completed: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    declined: int = Field(default=0, ge=0)


class ActivityParticipation(Model):
    event_id: str
    title: str
    active: bool
    total_records: int = Field(ge=0)
    unique_employees: int = Field(ge=0)
    completed_employees: int = Field(ge=0)
    counts: ParticipationCounts


class SkillDeficit(Model):
    skill_id: str
    skill_name: str
    eligible_employees: int = Field(ge=0)
    affected_employees: int = Field(ge=0)
    total_gap: int = Field(ge=0)
    average_gap_when_affected: float = Field(ge=0)


class RoleSkillDeficit(Model):
    role: str
    skill_id: str
    skill_name: str
    eligible_employees: int = Field(ge=0)
    affected_employees: int = Field(ge=0)
    average_gap_across_role: float = Field(ge=0)


class EmployeeWithoutStep(Model):
    employee_id: str
    name: str
    role: str
    grade: str
    status: Literal["top_grade", "no_skill_gaps", "no_matching_activity"]
    action_required: bool
    gap_skill_ids: list[str]
    reason: str


class HRSummary(Model):
    total_employees: int = Field(ge=0)
    readiness_employee_count: int = Field(ge=0)
    average_readiness_percent: float | None = Field(default=None, ge=0, le=100)
    skill_deficits: list[SkillDeficit]
    role_skill_deficits: list[RoleSkillDeficit]
    employees_without_step: list[EmployeeWithoutStep]
    participation_by_activity: list[ActivityParticipation]
    participation_totals: ParticipationCounts


def _counts(counter: Counter) -> ParticipationCounts:
    return ParticipationCounts(
        invited=counter["invited"], enrolled=counter["enrolled"],
        completed=counter["completed"], skipped=counter["skipped"],
        declined=counter["declined"],
    )


class EmployeeEvaluation(Model):
    """Everything HR aggregates need about one employee; cacheable per employee."""
    employee: Employee
    gaps: SkillGapResult
    readiness: GradeReadinessResult
    recommendation_status: Literal["recommended", "top_grade", "no_skill_gaps", "no_matching_activity"]
    top_recommendation: Recommendation | None = None


def scoped_dataset(dataset: Dataset, employee: Employee, records: list[HistoryRecord]) -> Dataset:
    """One employee with their own history and the shared catalog.

    Engines filter by employee_id, so results are identical to running on the
    full dataset while the cost no longer grows with the number of employees.
    """
    return Dataset.model_construct(employees=[employee], events=dataset.events,
                                   skills=dataset.skills, activity_history=records)


def evaluate_employee(scoped: Dataset, employee_id: str) -> EmployeeEvaluation:
    gaps = get_employee_skill_gaps(scoped, employee_id)
    recommendation = get_recommendations(scoped, employee_id, limit=1)
    employee = next(e for e in scoped.employees if e.employee_id == employee_id)
    return EmployeeEvaluation(
        employee=employee, gaps=gaps, readiness=calculate_readiness_from_gaps(gaps),
        recommendation_status=recommendation.status,
        top_recommendation=recommendation.recommendations[0] if recommendation.recommendations else None,
    )


def evaluate_all(dataset: Dataset) -> list[EmployeeEvaluation]:
    by_employee: dict[str, list[HistoryRecord]] = defaultdict(list)
    for record in dataset.activity_history:
        by_employee[record.employee_id].append(record)
    return [evaluate_employee(scoped_dataset(dataset, employee, by_employee[employee.employee_id]),
                              employee.employee_id)
            for employee in dataset.employees]


def get_hr_summary(dataset: Dataset) -> HRSummary:
    """Compute aggregate deficits, uncovered employees, and event participation.

    The caller must enforce HR access before exposing this result. Employees at
    the top grade and those who already meet the next grade are listed without
    a step, but only ``no_matching_activity`` is marked for HR action.
    """
    triples = [(r.employee_id, r.event_id, r.status) for r in dataset.activity_history]
    return aggregate_hr_summary(evaluate_all(dataset), dataset.events, dataset.skills, triples)


def aggregate_hr_summary(evaluations: list[EmployeeEvaluation], events: list[Activity],
                         skills: SkillsDocument, history: list[tuple[str, str, str]]) -> HRSummary:
    """Combine per-employee evaluations with (employee_id, event_id, status) history rows."""
    names = {skill.skill_id: skill.name for skill in skills.skill_catalog}
    global_gaps = {}
    role_gaps = {}
    without_step = []
    readiness_sum = 0.0
    readiness_count = 0

    for evaluation in evaluations:
        employee = evaluation.employee
        gaps = evaluation.gaps
        readiness = evaluation.readiness
        if readiness.readiness_percent is not None:
            readiness_sum += readiness.readiness_percent
            readiness_count += 1
        for skill_id, item in gaps.gaps.items():
            global_stats = global_gaps.setdefault(skill_id, [0, 0, 0])
            role_stats = role_gaps.setdefault((employee.role, skill_id), [0, 0, 0])
            for stats in (global_stats, role_stats):
                stats[0] += 1  # employees with this target-grade requirement
                stats[1] += item.gap > 0
                stats[2] += item.gap

        if evaluation.top_recommendation is None:
            status = evaluation.recommendation_status
            gap_skill_ids = sorted(skill_id for skill_id, item in gaps.gaps.items()
                                   if item.gap > 0)
            if status == "top_grade":
                reason = "Последний грейд: следующая ступень не задана."
            elif status == "no_skill_gaps":
                reason = "Требования следующего грейда уже закрыты."
            else:
                reason = "Нет активной подходящей активности для разрывов: " + ", ".join(gap_skill_ids)
            without_step.append(EmployeeWithoutStep(
                employee_id=employee.employee_id, name=employee.name,
                role=employee.role, grade=employee.grade, status=status,
                action_required=status == "no_matching_activity",
                gap_skill_ids=gap_skill_ids, reason=reason,
            ))

    skill_deficits = [
        SkillDeficit(
            skill_id=skill_id, skill_name=names[skill_id],
            eligible_employees=eligible, affected_employees=affected,
            total_gap=total, average_gap_when_affected=total / affected if affected else 0,
        )
        for skill_id, (eligible, affected, total) in global_gaps.items() if affected > 0
    ]
    skill_deficits.sort(key=lambda item: (-item.affected_employees,
                                          -item.average_gap_when_affected, item.skill_id))
    role_skill_deficits = [
        RoleSkillDeficit(
            role=role, skill_id=skill_id, skill_name=names[skill_id],
            eligible_employees=eligible, affected_employees=affected,
            average_gap_across_role=total / eligible,
        )
        for (role, skill_id), (eligible, affected, total) in role_gaps.items()
    ]
    role_skill_deficits.sort(key=lambda item: (item.role, item.skill_id))

    by_event = {event.event_id: Counter() for event in events}
    employees_by_event = {event.event_id: set() for event in events}
    completed_by_event = {event.event_id: set() for event in events}
    totals = Counter()
    for employee_id, event_id, status in history:
        by_event[event_id][status] += 1
        employees_by_event[event_id].add(employee_id)
        if status == "completed":
            completed_by_event[event_id].add(employee_id)
        totals[status] += 1
    participation = [
        ActivityParticipation(
            event_id=event.event_id, title=event.title, active=event.active,
            total_records=sum(by_event[event.event_id].values()),
            unique_employees=len(employees_by_event[event.event_id]),
            completed_employees=len(completed_by_event[event.event_id]),
            counts=_counts(by_event[event.event_id]),
        )
        for event in events
    ]

    return HRSummary(
        total_employees=len(evaluations),
        readiness_employee_count=readiness_count,
        average_readiness_percent=readiness_sum / readiness_count if readiness_count else None,
        skill_deficits=skill_deficits, role_skill_deficits=role_skill_deficits,
        employees_without_step=without_step,
        participation_by_activity=participation, participation_totals=_counts(totals),
    )
