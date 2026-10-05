"""Version 1 dataset contract. Levels and weights are demo policy, not HR facts."""

from datetime import date
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Lengths match the database columns (backend/db.py), so a valid file always fits.
Identifier = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
Label = Annotated[str, Field(min_length=1, max_length=255)]
Level = Annotated[int, Field(strict=True, ge=0, le=5)]
RequiredLevel = Annotated[int, Field(strict=True, ge=1, le=5)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class Employee(Model):
    employee_id: Identifier
    name: Label
    role: Label
    grade: Label
    tenure_months: Annotated[int, Field(strict=True, ge=0)]
    skills: dict[Identifier, Level]


class Skill(Model):
    skill_id: Identifier
    name: Label
    category: Literal["technical", "interpersonal"]
    max_level: RequiredLevel = 5


class Requirement(Model):
    level: RequiredLevel
    importance: Annotated[float, Field(strict=True, gt=0, le=1)] = 1.0
    mandatory: Annotated[bool, Field(strict=True)] = True


class CareerTrack(Model):
    role: Label
    grades: Annotated[list[Label], Field(min_length=1)]
    requirements: dict[Label, dict[Identifier, Requirement]]

    @model_validator(mode="after")
    def validate_grades(self):
        if len(self.grades) != len(set(self.grades)):
            raise ValueError("grades must be unique and ordered")
        if set(self.requirements) != set(self.grades):
            raise ValueError("requirements must cover exactly all grades")
        if any(not requirements for requirements in self.requirements.values()):
            raise ValueError("grade requirements must not be empty")
        if any(not any(r.mandatory for r in requirements.values())
               for requirements in self.requirements.values()):
            raise ValueError("every grade needs at least one mandatory skill")
        return self


class SkillsDocument(Model):
    schema_version: Literal["1.0"] = "1.0"
    skill_catalog: Annotated[list[Skill], Field(min_length=1)]
    career_tracks: Annotated[list[CareerTrack], Field(min_length=1)]


class ActivityGain(Model):
    skill_id: Identifier
    gain: RequiredLevel
    max_level: RequiredLevel


class Activity(Model):
    event_id: Identifier
    title: Label
    type: Literal["workshop", "course", "meetup", "mentoring", "project", "assessment"]
    category: Label
    audience: Annotated[list[Label], Field(min_length=1)]
    skills: Annotated[list[ActivityGain], Field(min_length=1)]
    duration_hours: Annotated[float, Field(strict=True, gt=0)]
    active: Annotated[bool, Field(strict=True)] = True

    @model_validator(mode="after")
    def unique_members(self):
        if len(self.audience) != len(set(self.audience)):
            raise ValueError("audience roles must be unique")
        ids = [s.skill_id for s in self.skills]
        if len(ids) != len(set(ids)):
            raise ValueError("activity skill IDs must be unique")
        return self


class HistoryRecord(Model):
    record_id: Identifier
    employee_id: Identifier
    event_id: Identifier
    status: Literal["invited", "enrolled", "completed", "skipped", "declined"]
    event_date: date

    @field_validator("event_date", mode="before")
    @classmethod
    def require_iso_date(cls, value):
        if type(value) is date:
            return value
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("event_date must be YYYY-MM-DD")
        return value


class Dataset(Model):
    employees: Annotated[list[Employee], Field(min_length=1)]
    events: list[Activity]
    skills: SkillsDocument
    activity_history: list[HistoryRecord]

    @model_validator(mode="after")
    def validate_references(self):
        errors = []

        def index(items, key, path):
            result = {}
            for i, item in enumerate(items):
                identity = getattr(item, key)
                if identity in result:
                    errors.append(f"{path}[{i}].{key}: duplicate '{identity}'")
                result[identity] = item
            return result

        catalog = index(self.skills.skill_catalog, "skill_id", "skills.skill_catalog")
        tracks = index(self.skills.career_tracks, "role", "skills.career_tracks")
        employees = index(self.employees, "employee_id", "employees")
        events = index(self.events, "event_id", "events")
        index(self.activity_history, "record_id", "activity_history")

        def check_skill(skill_id, level, path):
            if skill_id not in catalog:
                errors.append(f"{path}: unknown skill '{skill_id}'")
            elif level > catalog[skill_id].max_level:
                errors.append(f"{path}: level exceeds skill max_level")

        for i, track in enumerate(self.skills.career_tracks):
            for grade, requirements in track.requirements.items():
                for skill_id, requirement in requirements.items():
                    check_skill(skill_id, requirement.level,
                                f"skills.career_tracks[{i}].requirements.{grade}.{skill_id}")
        for i, employee in enumerate(self.employees):
            track = tracks.get(employee.role)
            if track is None:
                errors.append(f"employees[{i}].role: unknown role '{employee.role}'")
            elif employee.grade not in track.grades:
                errors.append(f"employees[{i}].grade: unknown grade '{employee.grade}'")
            for skill_id, level in employee.skills.items():
                check_skill(skill_id, level, f"employees[{i}].skills.{skill_id}")
        for i, activity in enumerate(self.events):
            for role in activity.audience:
                if role not in tracks:
                    errors.append(f"events[{i}].audience: unknown role '{role}'")
            for gain in activity.skills:
                check_skill(gain.skill_id, gain.max_level, f"events[{i}].skills.{gain.skill_id}")
                if gain.gain > gain.max_level:
                    errors.append(f"events[{i}].skills.{gain.skill_id}: gain exceeds max_level")
        for i, record in enumerate(self.activity_history):
            if record.employee_id not in employees:
                errors.append(f"activity_history[{i}].employee_id: unknown employee '{record.employee_id}'")
            if record.event_id not in events:
                errors.append(f"activity_history[{i}].event_id: unknown event '{record.event_id}'")
        if errors:
            raise ValueError("\n".join(errors))
        return self
