"""Dataset persistence on normalized SQL tables (PostgreSQL or SQLite).

Seed JSON/CSV files are never modified. Writes for one employee touch only that
employee's row and their new history records. Every write runs under one global
writer lock (``db.lock_state``) and bumps a data version that keys in-process
caches, so HR aggregates over thousands of employees are recomputed only for the
employees that actually changed.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import json
import threading
from typing import Callable
from uuid import uuid4

from sqlalchemy import and_, delete, func, insert, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from backend import db as schema
from backend.db import Database, dumps, iso, lock_state, migrate, read_legacy_sqlite_payload, utcnow
from backend.engine.hr import EmployeeEvaluation, evaluate_employee, scoped_dataset
from backend.models import Activity, Dataset, Employee, HistoryRecord, SkillsDocument
from backend.settings import database_url_from_env


class StateConflict(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class NotFound(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Actor:
    """Who performs a change; recorded in the audit log."""
    user_id: str | None
    role: str | None
    ip: str | None = None


SYSTEM = Actor("system", "system")


@dataclass
class Catalog:
    rev: int
    events: list[Activity]
    skills: SkillsDocument
    by_id: dict[str, Activity]


class _Replay(Exception):
    def __init__(self, response, status_code):
        self.response = response
        self.status_code = status_code


def _employee_values(employee: Employee) -> dict:
    return {"employee_id": employee.employee_id, "name": employee.name, "role": employee.role,
            "grade": employee.grade, "tenure_months": employee.tenure_months,
            "skills": dumps(employee.skills)}


def _record_values(record: HistoryRecord, created_by: str | None = None) -> dict:
    return {"record_id": record.record_id, "employee_id": record.employee_id,
            "event_id": record.event_id, "status": record.status,
            "event_date": record.event_date.isoformat(), "created_by": created_by}


def _employee_payload(row) -> dict:
    return {"employee_id": row.employee_id, "name": row.name, "role": row.role, "grade": row.grade,
            "tenure_months": row.tenure_months, "skills": json.loads(row.skills)}


def _record_payload(row) -> dict:
    return {"record_id": row.record_id, "employee_id": row.employee_id, "event_id": row.event_id,
            "status": row.status, "event_date": row.event_date}


def _ordered_compatible(old_ids: list[str], new_ids: list[str]) -> bool:
    """True if kept items keep their order and new items are only appended."""
    old_set, new_set = set(old_ids), set(new_ids)
    kept_new = [item for item in new_ids if item in old_set]
    if kept_new != [item for item in old_ids if item in new_set]:
        return False
    seen_new_only = False
    for item in new_ids:
        if item not in old_set:
            seen_new_only = True
        elif seen_new_only:
            return False
    return True


class DatasetStore:
    def __init__(self, target, seed: Dataset | None = None, *, snapshot_keep: int = 20):
        self.db = target if isinstance(target, Database) else Database(database_url_from_env(target))
        self.snapshot_keep = snapshot_keep
        self._lock = threading.Lock()
        self._eval_lock = threading.Lock()
        self._catalog_cache: Catalog | None = None
        self._evaluations: dict[str, tuple[int, int, EmployeeEvaluation]] = {}
        self._aggregate_cache: dict[str, tuple[int, object]] = {}
        migrate(self.db)
        self._seed(seed)
        self.snapshot()  # Fail on corrupt state instead of silently serving partial data.

    # ------------------------------------------------------------------ setup
    def _seed(self, seed: Dataset | None):
        with self.db.read_tx() as connection:
            if connection.execute(select(schema.state.c.id)).first():
                return
        legacy = read_legacy_sqlite_payload(self.db.url)
        dataset = Dataset.model_validate_json(legacy) if legacy else seed
        if dataset is None:
            raise RuntimeError("Database is empty and no seed dataset was provided")
        with self.db.write_tx() as connection:
            if not self.db.is_sqlite:
                connection.execute(text("SELECT pg_advisory_xact_lock(482918)"))
            if connection.execute(select(schema.state.c.id)).first():
                return
            connection.execute(insert(schema.state).values(
                id=1, version=1, catalog_rev=1, epoch=uuid4().hex,
                skills_document=dataset.skills.model_dump_json(), updated_at=utcnow()))
            self._insert_all(connection, dataset)
            self._audit(connection, SYSTEM, "dataset_seeded", None, {
                "source": "legacy_sqlite" if legacy else "seed", **self._counts(dataset)})

    @staticmethod
    def _counts(dataset: Dataset) -> dict:
        return {"employees": len(dataset.employees), "events": len(dataset.events),
                "skills": len(dataset.skills.skill_catalog), "history_records": len(dataset.activity_history)}

    @staticmethod
    def _insert_all(connection: Connection, dataset: Dataset, created_by: dict[str, str | None] | None = None):
        if dataset.employees:
            connection.execute(insert(schema.employees), [
                {"seq": index, "rev": 0, **_employee_values(e)} for index, e in enumerate(dataset.employees, 1)])
        if dataset.events:
            connection.execute(insert(schema.events), [
                {"seq": index, "event_id": e.event_id, "payload": e.model_dump_json()}
                for index, e in enumerate(dataset.events, 1)])
        if dataset.activity_history:
            connection.execute(insert(schema.history), [
                {"seq": index, **_record_values(r, (created_by or {}).get(r.record_id))}
                for index, r in enumerate(dataset.activity_history, 1)])

    # ------------------------------------------------------------------ reads
    def version(self) -> int:
        with self.db.read_tx() as connection:
            return connection.execute(select(schema.state.c.version)).scalar_one()

    def state_info(self) -> dict:
        with self.db.read_tx() as connection:
            row = connection.execute(select(schema.state)).one()
            return {"version": row.version, "catalog_rev": row.catalog_rev, "epoch": row.epoch,
                    "updated_at": iso(row.updated_at)}

    def _load_full(self, connection: Connection) -> Dataset:
        skills = connection.execute(select(schema.state.c.skills_document)).scalar_one()
        payload = {
            "employees": [_employee_payload(row) for row in connection.execute(
                select(schema.employees).order_by(schema.employees.c.seq))],
            "events": [json.loads(row.payload) for row in connection.execute(
                select(schema.events.c.payload).order_by(schema.events.c.seq))],
            "skills": json.loads(skills),
            "activity_history": [_record_payload(row) for row in connection.execute(
                select(schema.history).order_by(schema.history.c.seq))],
        }
        return Dataset.model_validate(payload)

    def snapshot(self) -> Dataset:
        """Full validated dataset (exports, imports, tests). Not used on hot paths."""
        with self.db.read_tx() as connection:
            return self._load_full(connection)

    def _catalog(self, connection: Connection, *, populate: bool = True) -> Catalog:
        """Events + skills document, cached by catalog revision.

        Only committed data populates the cache: write transactions pass
        ``populate=False`` because their revision may still be rolled back.
        """
        rev, document = connection.execute(
            select(schema.state.c.catalog_rev, schema.state.c.skills_document)).one()
        with self._lock:
            cached = self._catalog_cache
        if cached is not None and cached.rev == rev:
            return cached
        events = [Activity.model_validate_json(payload) for payload in connection.execute(
            select(schema.events.c.payload).order_by(schema.events.c.seq)).scalars()]
        catalog = Catalog(rev, events, SkillsDocument.model_validate_json(document),
                          {event.event_id: event for event in events})
        if populate:
            with self._lock:
                self._catalog_cache = catalog
        return catalog

    def catalog(self) -> Catalog:
        with self.db.read_tx() as connection:
            return self._catalog(connection)

    def _scoped(self, connection: Connection, employee_id: str, catalog: Catalog) -> Dataset:
        row = connection.execute(select(schema.employees).where(
            schema.employees.c.employee_id == employee_id)).first()
        employees, records = [], []
        if row is not None:
            employees = [Employee.model_validate(_employee_payload(row))]
            records = [HistoryRecord.model_validate(_record_payload(item)) for item in connection.execute(
                select(schema.history).where(schema.history.c.employee_id == employee_id)
                .order_by(schema.history.c.seq))]
        return Dataset.model_construct(employees=employees, events=catalog.events,
                                       skills=catalog.skills, activity_history=records)

    def scoped(self, employee_id: str) -> Dataset:
        """One employee, their own history and the shared catalog (fast path for profiles)."""
        with self.db.read_tx() as connection:
            return self._scoped(connection, employee_id, self._catalog(connection))

    def profile_bundle(self, employee_id: str) -> tuple[Dataset, list[dict]]:
        with self.db.read_tx() as connection:
            scoped = self._scoped(connection, employee_id, self._catalog(connection))
            pending = self._requests(connection, status="pending", employee_id=employee_id, limit=50)
            return scoped, pending

    def list_employees(self) -> list[dict]:
        with self.db.read_tx() as connection:
            columns = [schema.employees.c[name] for name in ("employee_id", "name", "role", "grade")]
            return [dict(row._mapping) for row in connection.execute(
                select(*columns).order_by(schema.employees.c.seq))]

    def employee_exists(self, employee_id: str) -> bool:
        with self.db.read_tx() as connection:
            return connection.execute(select(schema.employees.c.seq).where(
                schema.employees.c.employee_id == employee_id)).first() is not None

    def evaluations(self) -> tuple[int, Catalog, list[EmployeeEvaluation], list[tuple[str, str, str]]]:
        """Per-employee evaluations for HR views; only changed employees are recomputed."""
        with self._eval_lock, self.db.read_tx() as connection:
            version = connection.execute(select(schema.state.c.version)).scalar_one()
            catalog = self._catalog(connection)
            revisions = connection.execute(select(
                schema.employees.c.employee_id, schema.employees.c.rev).order_by(schema.employees.c.seq)).all()
            stale = [employee_id for employee_id, rev in revisions
                     if self._evaluations.get(employee_id, (None, None))[:2] != (catalog.rev, rev)]
            if stale:
                wanted = set(stale)
                if len(stale) > 200:
                    employee_rows = connection.execute(select(schema.employees)).all()
                    history_rows = connection.execute(
                        select(schema.history).order_by(schema.history.c.seq)).all()
                else:
                    employee_rows, history_rows = [], []
                    for start in range(0, len(stale), 100):
                        chunk = stale[start:start + 100]
                        employee_rows += connection.execute(select(schema.employees).where(
                            schema.employees.c.employee_id.in_(chunk))).all()
                        history_rows += connection.execute(select(schema.history).where(
                            schema.history.c.employee_id.in_(chunk)).order_by(schema.history.c.seq)).all()
                records: dict[str, list[HistoryRecord]] = {employee_id: [] for employee_id in wanted}
                for row in history_rows:
                    if row.employee_id in wanted:
                        records[row.employee_id].append(HistoryRecord.model_validate(_record_payload(row)))
                base = Dataset.model_construct(employees=[], events=catalog.events,
                                               skills=catalog.skills, activity_history=[])
                for row in employee_rows:
                    if row.employee_id not in wanted:
                        continue
                    employee = Employee.model_validate(_employee_payload(row))
                    evaluation = evaluate_employee(
                        scoped_dataset(base, employee, records[row.employee_id]), row.employee_id)
                    self._evaluations[row.employee_id] = (catalog.rev, row.rev, evaluation)
            current = {employee_id for employee_id, _ in revisions}
            for employee_id in list(self._evaluations):
                if employee_id not in current:
                    del self._evaluations[employee_id]
            evaluations = [self._evaluations[employee_id][2] for employee_id, _ in revisions]
            triples = [tuple(row) for row in connection.execute(select(
                schema.history.c.employee_id, schema.history.c.event_id, schema.history.c.status)
                .order_by(schema.history.c.seq))]
            return version, catalog, evaluations, triples

    def cached(self, name: str, build: Callable[[], object]):
        """Memoize an aggregate (dashboard, overview) by data version."""
        version = self.version()
        with self._lock:
            hit = self._aggregate_cache.get(name)
        if hit is not None and hit[0] == version:
            return hit[1]
        value_version, value = build()
        with self._lock:
            self._aggregate_cache[name] = (value_version, value)
        return value

    # ----------------------------------------------------------------- writes
    def _prior(self, connection: Connection, scope: str, request_key: str):
        return connection.execute(select(schema.idempotency).where(and_(
            schema.idempotency.c.scope == scope, schema.idempotency.c.request_key == request_key))).first()

    def _check_prior(self, prior, target: str, epoch: str):
        if prior.target != target:
            raise StateConflict("idempotency_conflict", "This key was used for a different activity or employee")
        if prior.epoch != epoch:
            raise StateConflict("dataset_replaced", "Dataset was replaced; use a new completion key")
        raise _Replay(json.loads(prior.response), prior.status_code)

    def apply(self, scope: str, request_key: str | None, target: str, employee_id: str,
              transform: Callable[[Dataset], tuple[Dataset, object]], *, actor: Actor = SYSTEM,
              action: str = "activity_completed", status_code: int = 200,
              audit_details: Callable[[object], dict] | None = None):
        """Idempotent write scoped to one employee; returns (response, replayed, status_code)."""
        for attempt in range(3):
            try:
                with self.db.write_tx() as connection:
                    if request_key is not None:
                        prior = self._prior(connection, scope, request_key)
                        if prior is not None:
                            epoch = connection.execute(select(schema.state.c.epoch)).scalar_one()
                            self._check_prior(prior, target, epoch)
                    _, _, epoch = lock_state(connection)
                    if request_key is not None:
                        prior = self._prior(connection, scope, request_key)
                        if prior is not None:
                            self._check_prior(prior, target, epoch)
                    catalog = self._catalog(connection, populate=False)
                    old = self._scoped(connection, employee_id, catalog)
                    new, result = transform(old)
                    response = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
                    self._persist_scoped(connection, old, new, actor)
                    if request_key is not None:
                        connection.execute(insert(schema.idempotency).values(
                            scope=scope, request_key=request_key, target=target, epoch=epoch,
                            status_code=status_code, response=dumps(response), created_at=utcnow()))
                    details = audit_details(response) if audit_details else None
                    self._audit(connection, actor, action, target, details)
                    return response, False, status_code
            except _Replay as replay:
                return replay.response, True, replay.status_code
            except IntegrityError:
                # A concurrent request with the same key committed first; replay it.
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable")

    def complete(self, scope: str, request_key: str, target: str, employee_id: str, transform, **options):
        response, replayed, _ = self.apply(scope, request_key, target, employee_id, transform, **options)
        return response, replayed

    def _persist_scoped(self, connection: Connection, old: Dataset, new: Dataset, actor: Actor):
        validated = Dataset.model_validate(new.model_dump(mode="json")) if new.employees else new
        if validated.events != old.events or validated.skills != old.skills:
            raise RuntimeError("Scoped writes must not change the activity catalog or skills")
        before = {employee.employee_id: employee for employee in old.employees}
        touched = set()
        for employee in validated.employees:
            previous = before.get(employee.employee_id)
            if previous is None:
                raise RuntimeError("Scoped writes cannot add employees")
            if previous != employee:
                values = _employee_values(employee)
                values.pop("employee_id")
                connection.execute(update(schema.employees).where(
                    schema.employees.c.employee_id == employee.employee_id).values(
                    **values, rev=schema.employees.c.rev + 1))
                touched.add(employee.employee_id)
        old_records = {record.record_id: record for record in old.activity_history}
        added = []
        for record in validated.activity_history:
            existing = old_records.get(record.record_id)
            if existing is None:
                added.append(record)
            elif existing != record:
                raise RuntimeError("History records are append-only")
        if len(validated.activity_history) - len(added) != len(old.activity_history):
            raise RuntimeError("History records cannot be removed by a scoped write")
        if added:
            start = connection.execute(select(func.coalesce(func.max(schema.history.c.seq), 0))).scalar_one()
            connection.execute(insert(schema.history), [
                {"seq": start + index, **_record_values(record, actor.user_id)}
                for index, record in enumerate(added, 1)])
            for employee_id in {record.employee_id for record in added} - touched:
                connection.execute(update(schema.employees).where(
                    schema.employees.c.employee_id == employee_id).values(rev=schema.employees.c.rev + 1))

    def update(self, transform: Callable[[Dataset], tuple[Dataset, object]], *, replace: bool = False,
               actor: Actor = SYSTEM, action: str = "dataset_updated", snapshot_reason: str | None = None,
               protect_snapshot: int | None = None):
        """Validated whole-dataset change (imports, restores). Serialized with every other write."""
        with self.db.write_tx() as connection:
            lock_state(connection, new_epoch=uuid4().hex if replace else None)
            current = self._load_full(connection)
            proposed, result = transform(current)
            validated = Dataset.model_validate(proposed.model_dump(mode="json"))
            if snapshot_reason:
                self._save_snapshot(connection, current, snapshot_reason, actor, protect=protect_snapshot)
            catalog_changed = self._sync_full(connection, current, validated, rewrite=replace)
            if catalog_changed or replace:
                connection.execute(update(schema.state).where(schema.state.c.id == 1).values(
                    catalog_rev=schema.state.c.catalog_rev + 1,
                    skills_document=validated.skills.model_dump_json()))
            if replace:
                connection.execute(update(schema.completion_requests).where(
                    schema.completion_requests.c.status == "pending").values(
                    status="expired", decided_at=utcnow(), decision_comment="Dataset replaced"))
            details = result if isinstance(result, dict) else None
            self._audit(connection, actor, action, None, details)
            return result

    def _sync_full(self, connection: Connection, old: Dataset, new: Dataset, *, rewrite: bool) -> bool:
        """Write the difference between two validated datasets; returns True if the catalog changed."""
        tables = (
            (old.employees, new.employees, "employee_id"),
            (old.events, new.events, "event_id"),
            (old.activity_history, new.activity_history, "record_id"),
        )
        if not rewrite:
            rewrite = not all(_ordered_compatible([getattr(i, key) for i in before], [getattr(i, key) for i in after])
                              for before, after, key in tables)
        if rewrite:
            created_by = dict(connection.execute(select(schema.history.c.record_id, schema.history.c.created_by).where(
                schema.history.c.created_by.is_not(None))).all())
            connection.execute(delete(schema.history))
            connection.execute(delete(schema.events))
            connection.execute(delete(schema.employees))
            self._insert_all(connection, new, created_by)
            return True

        touched = set()
        old_employees = {e.employee_id: e for e in old.employees}
        new_employee_ids = {e.employee_id for e in new.employees}
        start = connection.execute(select(func.coalesce(func.max(schema.employees.c.seq), 0))).scalar_one()
        added = []
        for employee in new.employees:
            previous = old_employees.get(employee.employee_id)
            if previous is None:
                added.append(employee)
            elif previous != employee:
                values = _employee_values(employee)
                values.pop("employee_id")
                connection.execute(update(schema.employees).where(
                    schema.employees.c.employee_id == employee.employee_id).values(
                    **values, rev=schema.employees.c.rev + 1))
                touched.add(employee.employee_id)
        if added:
            connection.execute(insert(schema.employees), [
                {"seq": start + index, "rev": 0, **_employee_values(e)} for index, e in enumerate(added, 1)])
        removed_employees = [employee_id for employee_id in old_employees if employee_id not in new_employee_ids]

        catalog_changed = new.skills != old.skills
        old_events = {e.event_id: e for e in old.events}
        new_event_ids = {e.event_id for e in new.events}
        start = connection.execute(select(func.coalesce(func.max(schema.events.c.seq), 0))).scalar_one()
        added = []
        for activity in new.events:
            previous = old_events.get(activity.event_id)
            if previous is None:
                added.append(activity)
            elif previous != activity:
                connection.execute(update(schema.events).where(
                    schema.events.c.event_id == activity.event_id).values(payload=activity.model_dump_json()))
                catalog_changed = True
        if added:
            connection.execute(insert(schema.events), [
                {"seq": start + index, "event_id": e.event_id, "payload": e.model_dump_json()}
                for index, e in enumerate(added, 1)])
            catalog_changed = True

        old_records = {r.record_id: r for r in old.activity_history}
        new_record_ids = {r.record_id for r in new.activity_history}
        start = connection.execute(select(func.coalesce(func.max(schema.history.c.seq), 0))).scalar_one()
        added = []
        for record in new.activity_history:
            previous = old_records.get(record.record_id)
            if previous is None:
                added.append(record)
                touched.add(record.employee_id)
            elif previous != record:
                connection.execute(update(schema.history).where(
                    schema.history.c.record_id == record.record_id).values(**_record_values(record)))
                touched.update({record.employee_id, previous.employee_id})
        if added:
            connection.execute(insert(schema.history), [
                {"seq": start + index, **_record_values(r)} for index, r in enumerate(added, 1)])
        removed_records = [r for r in old.activity_history if r.record_id not in new_record_ids]
        for record in removed_records:
            connection.execute(delete(schema.history).where(schema.history.c.record_id == record.record_id))
            touched.add(record.employee_id)
        for employee_id in removed_employees:
            connection.execute(delete(schema.employees).where(schema.employees.c.employee_id == employee_id))
        removed_events = [event_id for event_id in old_events if event_id not in new_event_ids]
        for event_id in removed_events:
            connection.execute(delete(schema.events).where(schema.events.c.event_id == event_id))
            catalog_changed = True
        touched -= set(removed_employees)
        for employee_id in touched - {e.employee_id for e in new.employees if e.employee_id not in old_employees}:
            connection.execute(update(schema.employees).where(
                schema.employees.c.employee_id == employee_id).values(rev=schema.employees.c.rev + 1))
        return catalog_changed

    # -------------------------------------------------------------- snapshots
    def _save_snapshot(self, connection: Connection, dataset: Dataset, reason: str, actor: Actor,
                       protect: int | None = None):
        counts = self._counts(dataset)
        connection.execute(insert(schema.dataset_snapshots).values(
            created_at=utcnow(), created_by=actor.user_id, reason=reason,
            employees=counts["employees"], events=counts["events"],
            history_records=counts["history_records"],
            payload=gzip.compress(dataset.model_dump_json().encode("utf-8"), 6)))
        keep = select(schema.dataset_snapshots.c.snapshot_id).order_by(
            schema.dataset_snapshots.c.snapshot_id.desc()).limit(self.snapshot_keep)
        stale = schema.dataset_snapshots.c.snapshot_id.not_in(keep.scalar_subquery())
        if protect is not None:
            stale = and_(stale, schema.dataset_snapshots.c.snapshot_id != protect)
        connection.execute(delete(schema.dataset_snapshots).where(stale))

    def create_snapshot(self, reason: str, actor: Actor) -> dict:
        with self.db.write_tx() as connection:
            lock_state(connection)
            self._save_snapshot(connection, self._load_full(connection), reason, actor)
            self._audit(connection, actor, "snapshot_created", None, {"reason": reason})
        return self.list_snapshots()[0]

    def list_snapshots(self) -> list[dict]:
        columns = [c for c in schema.dataset_snapshots.c if c.name != "payload"]
        with self.db.read_tx() as connection:
            return [{**dict(row._mapping), "created_at": iso(row.created_at)} for row in connection.execute(
                select(*columns).order_by(schema.dataset_snapshots.c.snapshot_id.desc()))]

    def restore_snapshot(self, snapshot_id: int, actor: Actor) -> dict:
        with self.db.read_tx() as connection:
            payload = connection.execute(select(schema.dataset_snapshots.c.payload).where(
                schema.dataset_snapshots.c.snapshot_id == snapshot_id)).scalar_one_or_none()
        if payload is None:
            raise NotFound("snapshot_not_found", f"Snapshot {snapshot_id} not found")
        restored = Dataset.model_validate_json(gzip.decompress(payload))
        return self.update(lambda _current: (restored, {"status": "restored", "snapshot_id": snapshot_id,
                                                        **self._counts(restored)}),
                           replace=True, actor=actor, action="snapshot_restored",
                           snapshot_reason="before_restore", protect_snapshot=snapshot_id)

    # ----------------------------------------------------- completion requests
    @staticmethod
    def _request_dict(row, names: dict | None = None, titles: dict | None = None) -> dict:
        result = {
            "request_id": row.request_id, "employee_id": row.employee_id, "event_id": row.event_id,
            "status": row.status, "requested_by": row.requested_by, "requested_at": iso(row.requested_at),
            "comment": row.comment, "decided_by": row.decided_by, "decided_at": iso(row.decided_at),
            "decision_comment": row.decision_comment, "record_id": row.record_id,
        }
        if names is not None:
            result["employee_name"] = names.get(row.employee_id)
        if titles is not None:
            result["event_title"] = titles.get(row.event_id)
        return result

    def _requests(self, connection: Connection, *, status: str | None = None,
                  employee_id: str | None = None, limit: int = 200) -> list[dict]:
        query = select(schema.completion_requests).order_by(schema.completion_requests.c.requested_at.desc(),
                                                            schema.completion_requests.c.request_id)
        if status:
            query = query.where(schema.completion_requests.c.status == status)
        if employee_id:
            query = query.where(schema.completion_requests.c.employee_id == employee_id)
        rows = connection.execute(query.limit(limit)).all()
        ids = sorted({row.employee_id for row in rows})
        names = dict(connection.execute(select(schema.employees.c.employee_id, schema.employees.c.name).where(
            schema.employees.c.employee_id.in_(ids))).all()) if ids else {}
        titles = {event_id: event.title for event_id, event in self._catalog(connection, populate=False).by_id.items()}
        return [self._request_dict(row, names, titles) for row in rows]

    def list_requests(self, *, status: str | None = None, employee_id: str | None = None,
                      limit: int = 200) -> list[dict]:
        with self.db.read_tx() as connection:
            return self._requests(connection, status=status, employee_id=employee_id, limit=limit)

    def request_completion(self, scope: str, request_key: str, employee_id: str, event_id: str, *,
                           actor: Actor, comment: str | None, validate: Callable[[Dataset], None]):
        target = f"{employee_id}/{event_id}"
        for attempt in range(3):
            try:
                with self.db.write_tx() as connection:
                    prior = self._prior(connection, scope, request_key)
                    if prior is not None:
                        self._check_prior(prior, target, connection.execute(select(schema.state.c.epoch)).scalar_one())
                    _, _, epoch = lock_state(connection)
                    prior = self._prior(connection, scope, request_key)
                    if prior is not None:
                        self._check_prior(prior, target, epoch)
                    catalog = self._catalog(connection, populate=False)
                    scoped = self._scoped(connection, employee_id, catalog)
                    validate(scoped)
                    pending = connection.execute(select(schema.completion_requests.c.request_id).where(and_(
                        schema.completion_requests.c.employee_id == employee_id,
                        schema.completion_requests.c.event_id == event_id,
                        schema.completion_requests.c.status == "pending"))).first()
                    if pending is not None:
                        raise StateConflict("request_pending", "This activity is already waiting for HR approval")
                    request_id = f"REQ_{uuid4().hex}"
                    connection.execute(insert(schema.completion_requests).values(
                        request_id=request_id, employee_id=employee_id, event_id=event_id, status="pending",
                        requested_by=actor.user_id or "unknown", requested_at=utcnow(), comment=comment,
                        epoch=epoch))
                    row = connection.execute(select(schema.completion_requests).where(
                        schema.completion_requests.c.request_id == request_id)).one()
                    names = {e.employee_id: e.name for e in scoped.employees}
                    titles = {event_id: event.title for event_id, event in catalog.by_id.items()}
                    response = {"status": "pending_approval", "request": self._request_dict(row, names, titles)}
                    connection.execute(insert(schema.idempotency).values(
                        scope=scope, request_key=request_key, target=target, epoch=epoch, status_code=202,
                        response=dumps(response), created_at=utcnow()))
                    self._audit(connection, actor, "completion_requested", target, {"request_id": request_id})
                    return response, False, 202
            except _Replay as replay:
                return replay.response, True, replay.status_code
            except IntegrityError:
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable")

    def decide_request(self, request_id: str, *, approve: bool, actor: Actor, comment: str | None,
                       complete: Callable[[Dataset, str, str], tuple[Dataset, object]]) -> dict:
        with self.db.write_tx() as connection:
            lock_state(connection)
            row = connection.execute(select(schema.completion_requests).where(
                schema.completion_requests.c.request_id == request_id)).first()
            if row is None:
                raise NotFound("request_not_found", f"Request '{request_id}' not found")
            if row.status != "pending":
                raise StateConflict("request_already_decided", f"Request is already {row.status}")
            values = {"decided_by": actor.user_id, "decided_at": utcnow(), "decision_comment": comment}
            completion = None
            if approve:
                catalog = self._catalog(connection, populate=False)
                old = self._scoped(connection, row.employee_id, catalog)
                new, result = complete(old, row.employee_id, row.event_id)
                self._persist_scoped(connection, old, new, actor)
                completion = result.model_dump(mode="json")
                values.update(status="approved", record_id=completion["record_id"])
            else:
                values.update(status="rejected")
            connection.execute(update(schema.completion_requests).where(
                schema.completion_requests.c.request_id == request_id).values(**values))
            updated = connection.execute(select(schema.completion_requests).where(
                schema.completion_requests.c.request_id == request_id)).one()
            self._audit(connection, actor, "completion_approved" if approve else "completion_rejected",
                        f"{row.employee_id}/{row.event_id}", {"request_id": request_id, "comment": comment})
            return {"request": self._request_dict(updated), "completion": completion}

    # ------------------------------------------------------------------ audit
    @staticmethod
    def _audit(connection: Connection, actor: Actor, action: str, target: str | None, details: dict | None):
        connection.execute(insert(schema.audit_log).values(
            at=utcnow(), actor_id=actor.user_id, actor_role=actor.role, action=action, target=target,
            details=dumps(details) if details else None, ip=actor.ip))

    def audit(self, actor: Actor, action: str, target: str | None = None, details: dict | None = None):
        with self.db.write_tx() as connection:
            self._audit(connection, actor, action, target, details)

    def reserve_action(self, actor: Actor, action: str, target: str | None, *, limit: int,
                       since: datetime) -> int | None:
        """Atomically count today's actions and record one more; None when the limit is reached.

        limit=0 means unlimited. Returns the audit row id so the caller can add details later.
        """
        with self.db.write_tx() as connection:
            if not self.db.is_sqlite:
                # Serialize reservations of the same user (SQLite already holds a write lock).
                connection.execute(text("SELECT pg_advisory_xact_lock(482919, hashtext(:key))"),
                                   {"key": f"{actor.user_id}:{action}"})
            if limit:
                used = connection.execute(select(func.count()).select_from(schema.audit_log).where(and_(
                    schema.audit_log.c.actor_id == actor.user_id, schema.audit_log.c.action == action,
                    schema.audit_log.c.at >= since))).scalar_one()
                if used >= limit:
                    return None
            return connection.execute(insert(schema.audit_log).values(
                at=utcnow(), actor_id=actor.user_id, actor_role=actor.role, action=action, target=target,
                details=None, ip=actor.ip).returning(schema.audit_log.c.id)).scalar_one()

    def update_audit_details(self, entry_id: int, details: dict):
        with self.db.write_tx() as connection:
            connection.execute(update(schema.audit_log).where(schema.audit_log.c.id == entry_id).values(
                details=dumps(details)))

    def list_audit(self, *, limit: int = 100, before_id: int | None = None, action: str | None = None,
                   actor_id: str | None = None, target: str | None = None) -> list[dict]:
        query = select(schema.audit_log).order_by(schema.audit_log.c.id.desc()).limit(limit)
        if before_id:
            query = query.where(schema.audit_log.c.id < before_id)
        if action:
            query = query.where(schema.audit_log.c.action == action)
        if actor_id:
            query = query.where(schema.audit_log.c.actor_id == actor_id)
        if target:
            query = query.where(schema.audit_log.c.target == target)
        with self.db.read_tx() as connection:
            return [{"id": row.id, "at": iso(row.at), "actor_id": row.actor_id, "actor_role": row.actor_role,
                     "action": row.action, "target": row.target,
                     "details": json.loads(row.details) if row.details else None, "ip": row.ip}
                    for row in connection.execute(query)]

    def count_actions(self, actor_id: str, action: str, since: datetime) -> int:
        with self.db.read_tx() as connection:
            return connection.execute(select(func.count()).select_from(schema.audit_log).where(and_(
                schema.audit_log.c.actor_id == actor_id, schema.audit_log.c.action == action,
                schema.audit_log.c.at >= since))).scalar_one()


def start_of_utc_day() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
