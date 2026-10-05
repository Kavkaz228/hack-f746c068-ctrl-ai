"""Apply one completed activity and recalculate an employee's career progress."""

from datetime import date
from typing import Literal

from pydantic import Field, ValidationError

from backend.engine.grade_progress import GradeReadinessResult, get_employee_grade_readiness
from backend.engine.recommendation import RecommendationResult, get_recommendations
from backend.models import Dataset, HistoryRecord, Model


class ProgressError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class SkillChange(Model):
    skill_id: str
    before: int = Field(ge=0, le=5)
    after: int = Field(ge=0, le=5)
    gain: int = Field(ge=1, le=5)
    max_level: int = Field(ge=1, le=5)
    applied_gain: int = Field(ge=0, le=5)


class CompletionResult(Model):
    employee_id: str
    event_id: str
    record_id: str
    status: Literal["completed", "already_completed"]
    skill_changes: list[SkillChange]
    readiness_before: GradeReadinessResult
    readiness_after: GradeReadinessResult
    recommendations_before: RecommendationResult
    recommendations_after: RecommendationResult


def complete_activity(
    dataset: Dataset,
    employee_id: str,
    event_id: str,
    record_id: str,
    completed_on: date,
) -> tuple[Dataset, CompletionResult]:
    """Return a validated new snapshot and the exact before/after calculation.

    Current skill levels are already a snapshot: historical completions are never
    replayed. A repeated record_id is a no-op, while a different record_id may
    represent a later repeat of the same activity. Persistence belongs to the
    caller so this function can be tested and reused by an API or CLI.
    """
    employee = next((item for item in dataset.employees
                     if item.employee_id == employee_id), None)
    if employee is None:
        raise ProgressError("employee_not_found", f"Employee '{employee_id}' not found")
    event = next((item for item in dataset.events if item.event_id == event_id), None)
    if event is None:
        raise ProgressError("event_not_found", f"Activity '{event_id}' not found")

    existing = next((item for item in dataset.activity_history
                     if item.record_id == record_id), None)
    if existing is not None:
        if (existing.employee_id != employee_id or existing.event_id != event_id
                or existing.status != "completed"):
            raise ProgressError("record_id_conflict", f"Record '{record_id}' already exists")
        readiness = get_employee_grade_readiness(dataset, employee_id)
        recommendations = get_recommendations(dataset, employee_id)
        return dataset, CompletionResult(
            employee_id=employee_id, event_id=event_id, record_id=record_id,
            status="already_completed", skill_changes=[],
            readiness_before=readiness, readiness_after=readiness,
            recommendations_before=recommendations, recommendations_after=recommendations,
        )

    if not event.active:
        raise ProgressError("inactive_event", f"Activity '{event_id}' is inactive")
    if employee.role not in event.audience:
        raise ProgressError("ineligible_role", f"Activity '{event_id}' is not available to '{employee.role}'")
    try:
        record = HistoryRecord(record_id=record_id, employee_id=employee_id,
                               event_id=event_id, status="completed", event_date=completed_on)
    except ValidationError as exc:
        raise ProgressError("invalid_completion", str(exc)) from exc

    readiness_before = get_employee_grade_readiness(dataset, employee_id)
    recommendations_before = get_recommendations(dataset, employee_id)
    payload = dataset.model_dump(mode="json")
    updated_employee = next(item for item in payload["employees"]
                            if item["employee_id"] == employee_id)
    changes = []
    for skill_gain in event.skills:
        before = employee.skills.get(skill_gain.skill_id, 0)
        # An event with a lower cap must never reduce an existing skill level.
        after = max(before, min(before + skill_gain.gain, skill_gain.max_level))
        updated_employee["skills"][skill_gain.skill_id] = after
        changes.append(SkillChange(
            skill_id=skill_gain.skill_id, before=before, after=after,
            gain=skill_gain.gain, max_level=skill_gain.max_level,
            applied_gain=after - before,
        ))
    payload["activity_history"].append(record.model_dump(mode="json"))
    updated_dataset = Dataset.model_validate(payload)
    return updated_dataset, CompletionResult(
        employee_id=employee_id, event_id=event_id, record_id=record_id,
        status="completed", skill_changes=changes,
        readiness_before=readiness_before,
        readiness_after=get_employee_grade_readiness(updated_dataset, employee_id),
        recommendations_before=recommendations_before,
        recommendations_after=get_recommendations(updated_dataset, employee_id),
    )


class ParticipationResult(Model):
    employee_id: str
    event_id: str
    record_id: str
    status: Literal["skipped", "declined", "invited", "enrolled"]
    recommendations_after: RecommendationResult


def record_participation(dataset: Dataset, employee_id: str, event_id: str, status: str,
                         record_id: str, on: date) -> tuple[Dataset, ParticipationResult]:
    """Store a non-completion outcome (skip/decline). Skills never change here,
    but the history multiplier of future recommendations does."""
    if status not in {"skipped", "declined", "invited", "enrolled"}:
        raise ProgressError("invalid_status", f"Unsupported participation status '{status}'")
    employee = next((item for item in dataset.employees if item.employee_id == employee_id), None)
    if employee is None:
        raise ProgressError("employee_not_found", f"Employee '{employee_id}' not found")
    event = next((item for item in dataset.events if item.event_id == event_id), None)
    if event is None:
        raise ProgressError("event_not_found", f"Activity '{event_id}' not found")
    if not event.active:
        raise ProgressError("inactive_event", f"Activity '{event_id}' is inactive")
    if employee.role not in event.audience:
        raise ProgressError("ineligible_role", f"Activity '{event_id}' is not available to '{employee.role}'")
    record = HistoryRecord(record_id=record_id, employee_id=employee_id, event_id=event_id,
                           status=status, event_date=on)
    payload = dataset.model_dump(mode="json")
    payload["activity_history"].append(record.model_dump(mode="json"))
    updated = Dataset.model_validate(payload)
    return updated, ParticipationResult(
        employee_id=employee_id, event_id=event_id, record_id=record_id, status=status,
        recommendations_after=get_recommendations(updated, employee_id))


class PromotionResult(Model):
    employee_id: str
    previous_grade: str
    new_grade: str
    forced: bool
    readiness_before: GradeReadinessResult
    readiness_after: GradeReadinessResult


def promote_employee(dataset: Dataset, employee_id: str, *, force: bool = False,
                     expected_grade: str | None = None) -> tuple[Dataset, PromotionResult]:
    """Move an employee to the next grade of their track (HR decision).

    Without ``force`` every mandatory requirement of the target grade must be met;
    the readiness percentage alone never promotes anyone.
    """
    employee = next((item for item in dataset.employees if item.employee_id == employee_id), None)
    if employee is None:
        raise ProgressError("employee_not_found", f"Employee '{employee_id}' not found")
    if expected_grade is not None and employee.grade != expected_grade:
        raise ProgressError("grade_changed", f"Грейд уже изменён на «{employee.grade}» — обновите страницу")
    readiness = get_employee_grade_readiness(dataset, employee_id)
    if readiness.status == "top_grade" or readiness.target_grade is None:
        raise ProgressError("top_grade", "Employee already has the top grade of the career track")
    if not readiness.meets_mandatory and not force:
        raise ProgressError("requirements_not_met", "Mandatory requirements of the next grade are not met")
    payload = dataset.model_dump(mode="json")
    target = next(item for item in payload["employees"] if item["employee_id"] == employee_id)
    target["grade"] = readiness.target_grade
    updated = Dataset.model_validate(payload)
    return updated, PromotionResult(
        employee_id=employee_id, previous_grade=employee.grade, new_grade=readiness.target_grade,
        forced=bool(force and not readiness.meets_mandatory), readiness_before=readiness,
        readiness_after=get_employee_grade_readiness(updated, employee_id))
