"""Adapt shared domain results to the HTTP contract without repeating business logic."""

from datetime import date
from typing import Literal

from pydantic import Field

from backend.engine.grade_progress import GradeReadinessResult
from backend.engine.hr import EmployeeEvaluation, RoleSkillDeficit, aggregate_hr_summary, evaluate_all
from backend.engine.progress import complete_activity
from backend.engine.recommendation import RecommendationResult
from backend.engine.skill_gap import SkillGapResult, get_employee_skill_gaps
from backend.models import Activity, Dataset, Model, SkillsDocument

PARTICIPATION_STATUSES = ("invited", "enrolled", "completed", "skipped", "declined")


class SkillGapSummary(Model):
    skill_id: str
    skill_name: str
    employee_count: int = Field(ge=0)
    mandatory_employee_count: int = Field(ge=0)
    total_gap: int = Field(ge=0)


class EmployeeWithoutRecommendations(Model):
    employee_id: str
    name: str
    role: str
    grade: str
    reason: Literal["top_grade", "no_skill_gaps", "no_matching_activity"]


class ActivityParticipation(Model):
    event_id: str
    title: str
    active: bool
    total_records: int = Field(ge=0)
    unique_employees: int = Field(ge=0)
    status_counts: dict[str, int]


class HRDashboardResult(Model):
    total_employees: int = Field(ge=0)
    average_readiness_percent: float | None = Field(default=None, ge=0, le=100)
    top_skill_gaps: list[SkillGapSummary]
    role_skill_deficits: list[RoleSkillDeficit]
    employees_without_recommendations: list[EmployeeWithoutRecommendations]
    participation: dict[str, int]
    activities: list[ActivityParticipation]



def get_hr_dashboard(dataset: Dataset) -> HRDashboardResult:
    triples = [(r.employee_id, r.event_id, r.status) for r in dataset.activity_history]
    return build_hr_dashboard(evaluate_all(dataset), dataset.events, dataset.skills, triples)


def build_hr_dashboard(evaluations: list[EmployeeEvaluation], events: list[Activity],
                       skills: SkillsDocument, history: list[tuple[str, str, str]]) -> HRDashboardResult:
    """Linear in employees + history; evaluations may come from a per-employee cache."""
    summary = aggregate_hr_summary(evaluations, events, skills, history)
    mandatory_counts = {}
    for evaluation in evaluations:
        for skill_id, gap in evaluation.gaps.gaps.items():
            if gap.mandatory and gap.gap > 0:
                mandatory_counts[skill_id] = mandatory_counts.get(skill_id, 0) + 1
    return HRDashboardResult(
        total_employees=summary.total_employees,
        average_readiness_percent=summary.average_readiness_percent,
        top_skill_gaps=sorted([
            SkillGapSummary(skill_id=item.skill_id, skill_name=item.skill_name,
                            employee_count=item.affected_employees,
                            mandatory_employee_count=mandatory_counts.get(item.skill_id, 0),
                            total_gap=item.total_gap)
            for item in summary.skill_deficits
        ], key=lambda item: (-item.employee_count, -item.total_gap, item.skill_id)),
        role_skill_deficits=summary.role_skill_deficits,
        employees_without_recommendations=[
            EmployeeWithoutRecommendations(employee_id=item.employee_id, name=item.name,
                                           role=item.role, grade=item.grade, reason=item.status)
            for item in sorted(summary.employees_without_step, key=lambda item: item.employee_id)
        ],
        participation=summary.participation_totals.model_dump(),
        activities=[
            ActivityParticipation(event_id=item.event_id, title=item.title, active=item.active,
                                  total_records=item.total_records, unique_employees=item.unique_employees,
                                  status_counts=item.counts.model_dump())
            for item in sorted(summary.participation_by_activity, key=lambda item: item.event_id)
        ],
    )


class SkillChangeResponse(Model):
    skill_id: str
    before: int = Field(ge=0, le=5)
    after: int = Field(ge=0, le=5)
    gain_applied: int = Field(ge=0, le=5)


class CompletionResponse(Model):
    employee_id: str
    event_id: str
    record_id: str
    skill_changes: list[SkillChangeResponse]
    readiness_before: GradeReadinessResult
    readiness_after: GradeReadinessResult
    skill_gaps: SkillGapResult
    recommendations: RecommendationResult


def complete_activity_response(dataset: Dataset, employee_id: str, event_id: str,
                               record_id: str, completed_on: date) -> tuple[Dataset, CompletionResponse]:
    updated, result = complete_activity(dataset, employee_id, event_id, record_id, completed_on)
    return updated, CompletionResponse(
        employee_id=result.employee_id, event_id=result.event_id, record_id=result.record_id,
        skill_changes=[SkillChangeResponse(skill_id=item.skill_id, before=item.before,
                                           after=item.after, gain_applied=item.applied_gain)
                       for item in result.skill_changes],
        readiness_before=result.readiness_before, readiness_after=result.readiness_after,
        skill_gaps=get_employee_skill_gaps(updated, employee_id),
        recommendations=result.recommendations_after,
    )
