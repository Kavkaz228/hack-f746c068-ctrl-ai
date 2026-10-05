"""FastAPI application factory. Run: python -m uvicorn backend.api.app:app."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import threading
from typing import Literal
from uuid import UUID, uuid4

from fastapi import Body, Depends, FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.exc import DataError, OperationalError
from starlette.exceptions import HTTPException

from backend.accounts import AccountError, AccountStore, generate_password
from backend.api import admin_routes, auth_routes
from backend.api.ai import AIRecommendationResult, get_ai_recommendations
from backend.api.auth import Account, Principal, current_account, require_employee_access, require_hr
from backend.api.imports import import_dataset, parse_upload, preview_import
from backend.api.presenters import (
    CompletionResponse, HRDashboardResult, build_hr_dashboard, complete_activity_response,
)
from backend.api.security import HardeningMiddleware, Metrics, RateLimiter, configure_logging, log
from backend.data_loader import DEFAULT_DATA_DIR, DatasetValidationError, load_dataset
from backend.db import SCHEMA_VERSION, Database
from backend.engine.grade_progress import GradeReadinessResult, get_employee_grade_readiness
from backend.engine.progress import (
    ParticipationResult, ProgressError, PromotionResult, complete_activity, promote_employee,
    record_participation,
)
from backend.engine.recommendation import (
    RecommendationError, RecommendationResult, get_all_recommendations, get_recommendations,
)
from backend.engine.simulation import (
    ComparisonResult, SimulationError, SimulationResult, compare_activities, simulate_activity,
)
from backend.engine.skill_gap import SkillGapError, SkillGapResult, get_employee_skill_gaps
from backend.models import Activity, CareerTrack, Employee, HistoryRecord, Model, Skill
from backend.settings import RUNTIME_DIR, Settings, database_url_from_env
from backend.storage import DatasetStore, NotFound, StateConflict, start_of_utc_day

MAX_UPLOAD_BYTES = 5 * 1024 * 1024


class HistoryEntry(HistoryRecord):
    event_title: str
    event_type: str


class CompletionRequestOut(Model):
    request_id: str
    employee_id: str
    event_id: str
    status: str
    requested_by: str
    requested_at: str | None
    comment: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None
    decision_comment: str | None = None
    record_id: str | None = None
    employee_name: str | None = None
    event_title: str | None = None


class ProfileResponse(Model):
    employee: Employee
    career_track: CareerTrack
    history: list[HistoryEntry]
    skill_catalog: list[Skill]
    skill_gaps: SkillGapResult
    readiness: GradeReadinessResult
    recommendations: RecommendationResult
    pending_completions: list[CompletionRequestOut] = []


class EmployeeSummary(Model):
    employee_id: str
    name: str
    role: str
    grade: str


class NextStep(Model):
    event_id: str
    title: str
    score: float


class EmployeeOverview(Model):
    employee_id: str
    name: str
    role: str
    grade: str
    target_grade: str | None
    readiness_percent: float | None
    mandatory_readiness_percent: float | None
    meets_mandatory: bool | None
    skills_with_gap: int
    mandatory_skills_with_gap: int
    recommendation_status: str
    next_step: NextStep | None


class PendingCompletion(Model):
    status: Literal["pending_approval"]
    request: CompletionRequestOut


class PromotionRequest(BaseModel):
    force: bool = False
    reason: str | None = Field(default=None, max_length=500)
    # Grade the HR user saw on screen; a stale page or a double click cannot promote twice.
    expected_grade: str | None = Field(default=None, max_length=255)


class CompletionComment(BaseModel):
    comment: str | None = Field(default=None, max_length=500)


def store(request: Request) -> DatasetStore:
    return request.app.state.store


def settings_of(request: Request) -> Settings:
    return request.app.state.settings


def _today():
    return datetime.now(timezone.utc).date()


def _bootstrap_users(application, settings: Settings, accounts: AccountStore, repository: DatasetStore,
                     service_accounts: list[Account] | None):
    try:
        _create_initial_users(settings, accounts, repository, service_accounts)
    except AccountError as exc:
        # Several worker processes start at once; the first one wins, the rest skip.
        if exc.code not in {"user_exists", "employee_has_account"}:
            raise
        log(logging.INFO, "bootstrap_skipped", reason="created by another worker")


def _create_initial_users(settings: Settings, accounts: AccountStore, repository: DatasetStore,
                          service_accounts: list[Account] | None):
    if service_accounts is not None:
        for account in service_accounts:
            accounts.upsert_service_account(user_id=account.account_id, role=account.role,
                                            employee_id=account.employee_id, token=account.token)
        return
    if accounts.count():
        return
    if settings.bootstrap_email and settings.bootstrap_password:
        accounts.create(email=settings.bootstrap_email, name=settings.bootstrap_name, role="hr",
                        password=settings.bootstrap_password, must_change_password=True)
        log(logging.WARNING, "bootstrap_hr_created", email=settings.bootstrap_email)
        return
    if settings.environment == "production":
        log(logging.WARNING, "no_users", hint="Create an HR account: python -m backend.manage create-user")
        return
    # Local development only: two demo accounts with generated passwords.
    employee_id = repository.list_employees()[0]["employee_id"]
    hr_password, employee_password = generate_password(), generate_password()
    accounts.create(email="hr@demo.local", name="Демо HR-менеджер", role="hr", password=hr_password,
                    must_change_password=False)
    accounts.create(email="employee@demo.local", name="Демо сотрудник", role="employee",
                    employee_id=employee_id, password=employee_password, must_change_password=False)
    path = RUNTIME_DIR / "demo-credentials.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Career Quest local demo accounts (development only)\n"
                    f"HR:       hr@demo.local / {hr_password}\n"
                    f"Employee: employee@demo.local / {employee_password}  ({employee_id})\n",
                    encoding="utf-8")
    log(logging.WARNING, "demo_users_created", credentials_file=str(path))


def create_app(*, state_path: str | Path | None = None, data_dir: str | Path | None = None,
               database_url: str | None = None, accounts: list[Account] | None = None,
               settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(os.environ.get("CAREER_QUEST_LOG_LEVEL", "INFO"))
    metrics = Metrics()

    @asynccontextmanager
    async def lifespan(application):
        url = database_url or database_url_from_env(state_path)
        database = Database(url, pool_size=int(os.environ.get("CAREER_QUEST_DB_POOL", "5")))
        seed = load_dataset(data_dir or os.environ.get("CAREER_QUEST_DATA_DIR", DEFAULT_DATA_DIR))
        repository = DatasetStore(database, seed, snapshot_keep=settings.snapshot_keep)
        account_store = AccountStore(database, session_hours=settings.session_hours,
                                     session_idle_minutes=settings.session_idle_minutes,
                                     max_failures=settings.login_max_failures,
                                     lock_minutes=settings.login_lock_minutes)
        application.state.store = repository
        application.state.accounts = account_store
        application.state.database = database
        _bootstrap_users(application, settings, account_store, repository, accounts)
        # Warm the per-employee cache so the first HR page load is fast.
        threading.Thread(target=_warm, args=(repository,), daemon=True).start()
        log(logging.INFO, "started", database="sqlite" if database.is_sqlite else "postgresql",
            completion_mode=settings.completion_mode, environment=settings.environment)
        yield
        database.dispose()

    docs = settings.enable_docs
    application = FastAPI(title="Career Quest API", version="1.0.0", lifespan=lifespan,
                          docs_url="/docs" if docs else None, redoc_url=None,
                          openapi_url="/openapi.json" if docs else None)
    application.state.settings = settings
    application.state.metrics = metrics
    application.state.login_limiter = RateLimiter(settings.login_ip_limit_per_15min, 15 * 60)
    application.add_middleware(GZipMiddleware, minimum_size=1024)
    if settings.cors_origins:
        application.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                                   allow_methods=["GET", "POST", "DELETE"],
                                   allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
                                   expose_headers=["Idempotency-Replayed", "X-Request-ID"])
    docs_paths = frozenset(prefix + path for prefix in ("", "/api") for path in ("/docs", "/docs/oauth2-redirect")) \
        if docs else frozenset()
    application.add_middleware(HardeningMiddleware, metrics=metrics,
                               max_json_body=settings.max_json_body_bytes,
                               max_upload_body=settings.max_upload_body_bytes, docs_paths=docs_paths)

    @application.exception_handler(HTTPException)
    async def http_error(request, exc):
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": "http_error", "message": str(exc.detail)}
        return JSONResponse(detail, status_code=exc.status_code, headers=exc.headers)

    @application.exception_handler(RequestValidationError)
    async def request_error(request, exc):
        issues = [{"location": ".".join(map(str, error["loc"])), "message": error["msg"]}
                  for error in exc.errors()]
        return JSONResponse({"error": "invalid_request", "message": "Request validation failed", "issues": issues}, status_code=422)

    @application.exception_handler(ValidationError)
    async def validation_error(request, exc):
        issues = [{"location": ".".join(map(str, error["loc"])) or "dataset", "message": error["msg"]}
                  for error in exc.errors(include_input=False, include_url=False)]
        return JSONResponse({"error": "invalid_dataset", "message": "Dataset validation failed", "issues": issues}, status_code=422)

    async def domain_error(request, exc):
        code = getattr(exc, "code", "invalid_dataset")
        status = 404 if code in {"employee_not_found", "event_not_found"} else 409 if code == "grade_changed" else 422
        if isinstance(exc, StateConflict):
            status = 409
        elif isinstance(exc, NotFound):
            status = 404
        elif isinstance(exc, AccountError):
            status = exc.status
        return JSONResponse({"error": code, "message": str(exc)}, status_code=status)

    for error_type in (SkillGapError, RecommendationError, ProgressError, SimulationError,
                       DatasetValidationError, StateConflict, NotFound, AccountError):
        application.add_exception_handler(error_type, domain_error)

    @application.exception_handler(DataError)
    async def data_error(request, exc):
        return JSONResponse({"error": "invalid_dataset", "message": "A value does not fit the allowed length or format"},
                            status_code=422)

    @application.exception_handler(OperationalError)
    async def storage_error(request, exc):
        log(logging.ERROR, "storage_unavailable", error=type(exc.orig).__name__ if exc.orig else "OperationalError")
        return JSONResponse({"error": "storage_unavailable", "message": "Unable to save or read state; retry later"}, status_code=503)

    application.include_router(auth_routes.router)
    application.include_router(admin_routes.router)

    @application.get("/health")
    def health():
        """Liveness: the process answers. Use /ready for dependencies."""
        return {"status": "ok"}

    @application.get("/ready")
    def ready(request: Request):
        try:
            request.app.state.database.ping()
        except Exception:  # noqa: BLE001 - any failure means not ready
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready", "schema_version": SCHEMA_VERSION}

    @application.get("/metrics", include_in_schema=False)
    def metrics_endpoint(request: Request, authorization: str | None = Header(None)):
        token = settings.metrics_token
        if not token:
            raise HTTPException(404, detail={"error": "not_found", "message": "Metrics are disabled"})
        import secrets
        if not authorization or not secrets.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, detail={"error": "unauthorized", "message": "Metrics token required"},
                                headers={"WWW-Authenticate": "Bearer"})
        info = request.app.state.store.state_info()
        return PlainTextResponse(metrics.render({"career_quest_data_version": info["version"]}),
                                 media_type="text/plain; version=0.0.4")

    @application.get("/employees", response_model=list[EmployeeSummary])
    def employees(account: Principal = Depends(require_hr), repository: DatasetStore = Depends(store)):
        return repository.list_employees()

    @application.get("/hr/employees", response_model=list[EmployeeOverview])
    def overview(account: Principal = Depends(require_hr), repository: DatasetStore = Depends(store)):
        """All employees with readiness and next step in one request (HR list page)."""
        def build():
            version, _, evaluations, _ = repository.evaluations()
            return version, [EmployeeOverview(
                employee_id=item.employee.employee_id, name=item.employee.name, role=item.employee.role,
                grade=item.employee.grade, target_grade=item.readiness.target_grade,
                readiness_percent=item.readiness.readiness_percent,
                mandatory_readiness_percent=item.readiness.mandatory_readiness_percent,
                meets_mandatory=item.readiness.meets_mandatory, skills_with_gap=item.gaps.skills_with_gap,
                mandatory_skills_with_gap=item.gaps.mandatory_skills_with_gap,
                recommendation_status=item.recommendation_status,
                next_step=NextStep(event_id=item.top_recommendation.event_id, title=item.top_recommendation.title,
                                   score=item.top_recommendation.score) if item.top_recommendation else None,
            ) for item in evaluations]
        return repository.cached("overview", build)

    @application.get("/employees/{employee_id}", response_model=ProfileResponse)
    def profile(employee_id: str, request: Request, account: Principal = Depends(require_employee_access),
                repository: DatasetStore = Depends(store)):
        dataset, pending = repository.profile_bundle(employee_id)
        gaps = get_employee_skill_gaps(dataset, employee_id)
        employee = dataset.employees[0]
        catalog = {event.event_id: event for event in dataset.events}
        if account.role == "hr" and account.via == "session":
            repository.audit(account.actor(request), "profile_viewed", employee_id)
        return ProfileResponse(
            employee=employee,
            career_track=next(t for t in dataset.skills.career_tracks if t.role == employee.role),
            history=[HistoryEntry(**r.model_dump(), event_title=catalog[r.event_id].title,
                                  event_type=catalog[r.event_id].type)
                     for r in dataset.activity_history],
            skill_catalog=dataset.skills.skill_catalog,
            skill_gaps=gaps, readiness=get_employee_grade_readiness(dataset, employee_id),
            recommendations=get_recommendations(dataset, employee_id),
            pending_completions=pending,
        )

    @application.get("/employees/{employee_id}/recommendations", response_model=RecommendationResult)
    def recommendations(employee_id: str, limit: int = Query(3, ge=1, le=3),
                        account: Principal = Depends(require_employee_access), repository: DatasetStore = Depends(store)):
        return get_recommendations(repository.scoped(employee_id), employee_id, limit)

    @application.post("/employees/{employee_id}/ai/recommendations", response_model=AIRecommendationResult)
    def ai_recommendations(employee_id: str, request: Request, account: Principal = Depends(require_employee_access),
                           repository: DatasetStore = Depends(store)):
        api_key = os.environ.get("OPENAI_API_KEY")
        reservation = None
        if api_key:
            # Reserve a slot atomically before the paid call; parallel requests cannot exceed the limit.
            reservation = repository.reserve_action(account.actor(request), "ai_recommendation", employee_id,
                                                    limit=settings.ai_daily_limit, since=start_of_utc_day())
            if reservation is None:
                raise HTTPException(429, detail={"error": "ai_limit_reached",
                                                 "message": "Дневной лимит AI-рекомендаций исчерпан"})
        result = get_ai_recommendations(repository.scoped(employee_id), employee_id,
                                        api_key=api_key, model=settings.openai_model)
        if reservation is not None:
            repository.update_audit_details(reservation, {"source": result.source,
                                                          "fallback_reason": result.fallback_reason})
        return result

    @application.get("/employees/{employee_id}/comparison-options", response_model=RecommendationResult)
    def comparison_options(employee_id: str, account: Principal = Depends(require_employee_access),
                           repository: DatasetStore = Depends(store)):
        return get_all_recommendations(repository.scoped(employee_id), employee_id)

    @application.get("/employees/{employee_id}/simulate/{event_id}", response_model=SimulationResult)
    def simulate(employee_id: str, event_id: str,
                 account: Principal = Depends(require_employee_access), repository: DatasetStore = Depends(store)):
        return simulate_activity(repository.scoped(employee_id), employee_id, event_id)

    @application.get("/employees/{employee_id}/compare", response_model=ComparisonResult)
    def compare(employee_id: str, first_event_id: str, second_event_id: str,
                account: Principal = Depends(require_employee_access), repository: DatasetStore = Depends(store)):
        return compare_activities(repository.scoped(employee_id), employee_id, first_event_id, second_event_id)

    @application.get("/events", response_model=list[Activity])
    def events(account: Principal = Depends(current_account), repository: DatasetStore = Depends(store)):
        if account.role == "hr":
            return repository.catalog().events
        dataset = repository.scoped(account.employee_id or "")
        if not dataset.employees:
            raise SkillGapError("employee_not_found", "Employee profile not found")
        role = dataset.employees[0].role
        return [e for e in dataset.events if e.active and role in e.audience]

    @application.post("/employees/{employee_id}/activities/{event_id}/complete",
                      responses={200: {"model": CompletionResponse}, 202: {"model": PendingCompletion}})
    def complete(employee_id: str, event_id: str, request: Request,
                 idempotency_key: UUID = Header(alias="Idempotency-Key"),
                 comment: CompletionComment | None = Body(None),
                 account: Principal = Depends(require_employee_access), repository: DatasetStore = Depends(store)):
        """Employees in approval mode create a request for HR; HR (or instant mode) applies at once."""
        target = f"{employee_id}/{event_id}"
        actor = account.actor(request)
        if settings.completion_mode == "approval" and account.role != "hr":
            def validate(dataset):
                complete_activity(dataset, employee_id, event_id, "VALIDATE_ONLY", _today())
            result, replayed, status = repository.request_completion(
                account.account_id, str(idempotency_key), employee_id, event_id, actor=actor,
                comment=comment.comment if comment else None, validate=validate)
        else:
            result, replayed, status = repository.apply(
                account.account_id, str(idempotency_key), target, employee_id,
                lambda dataset: complete_activity_response(dataset, employee_id, event_id,
                                                           f"CMP_{uuid4().hex}", _today()),
                actor=actor, action="activity_completed",
                audit_details=lambda response: {"record_id": response.get("record_id"),
                                                "skill_changes": response.get("skill_changes")})
        return JSONResponse(result, status_code=status,
                            headers={"Idempotency-Replayed": str(replayed).lower()})

    def _participation(status_name: str, prefix: str):
        def handler(employee_id: str, event_id: str, request: Request,
                    idempotency_key: UUID | None = Header(None, alias="Idempotency-Key"),
                    account: Principal = Depends(require_employee_access),
                    repository: DatasetStore = Depends(store)):
            result, replayed, status = repository.apply(
                account.account_id, str(idempotency_key) if idempotency_key else None,
                f"{employee_id}/{event_id}/{status_name}", employee_id,
                lambda dataset: record_participation(dataset, employee_id, event_id, status_name,
                                                     f"{prefix}_{uuid4().hex}", _today()),
                actor=account.actor(request), action=f"activity_{status_name}")
            return JSONResponse(result, status_code=status, headers={"Idempotency-Replayed": str(replayed).lower()})
        return handler

    application.add_api_route("/employees/{employee_id}/activities/{event_id}/skip", _participation("skipped", "SKP"),
                              methods=["POST"], responses={200: {"model": ParticipationResult}},
                              summary="Record that the employee skipped an activity")
    application.add_api_route("/employees/{employee_id}/activities/{event_id}/decline", _participation("declined", "DCL"),
                              methods=["POST"], responses={200: {"model": ParticipationResult}},
                              summary="Record that the employee declined an activity")

    @application.post("/employees/{employee_id}/promote", responses={200: {"model": PromotionResult}})
    def promote(employee_id: str, request: Request, body: PromotionRequest = Body(PromotionRequest()),
                idempotency_key: UUID | None = Header(None, alias="Idempotency-Key"),
                account: Principal = Depends(require_hr), repository: DatasetStore = Depends(store)):
        """HR moves an employee to the next grade; requires all mandatory skills unless forced with a reason."""
        if body.force and not (body.reason or "").strip():
            raise HTTPException(422, detail={"error": "reason_required",
                                             "message": "A reason is required to promote without meeting requirements"})
        result, replayed, _ = repository.apply(
            account.account_id, str(idempotency_key) if idempotency_key else None, f"{employee_id}/promote", employee_id,
            lambda dataset: promote_employee(dataset, employee_id, force=body.force, expected_grade=body.expected_grade),
            actor=account.actor(request), action="employee_promoted",
            audit_details=lambda response: {"from": response["previous_grade"], "to": response["new_grade"],
                                            "forced": response["forced"], "reason": body.reason})
        return JSONResponse(result, headers={"Idempotency-Replayed": str(replayed).lower()})

    @application.get("/hr/dashboard", response_model=HRDashboardResult)
    def dashboard(account: Principal = Depends(require_hr), repository: DatasetStore = Depends(store)):
        def build():
            version, catalog, evaluations, history = repository.evaluations()
            return version, build_hr_dashboard(evaluations, catalog.events, catalog.skills, history)
        return repository.cached("dashboard", build)

    @application.post("/dataset/import")
    def upload_dataset(
        request: Request,
        mode: Literal["append", "replace"] = Form("append"),
        dry_run: bool = Form(False),
        employees: UploadFile | None = File(None), events: UploadFile | None = File(None),
        skills: UploadFile | None = File(None), activity_history: UploadFile | None = File(None),
        account: Principal = Depends(require_hr), repository: DatasetStore = Depends(store),
    ):
        parts = {}
        for name, upload in {"employees": employees, "events": events,
                             "skills": skills, "activity_history": activity_history}.items():
            if upload is not None:
                content = upload.file.read(MAX_UPLOAD_BYTES + 1)
                if len(content) > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, detail={"error": "file_too_large", "message": f"{name}: maximum size is 5 MiB"})
                parts[name] = parse_upload(name, content)
        if dry_run:
            return preview_import(repository.snapshot(), parts, mode)
        return repository.update(lambda dataset: import_dataset(dataset, parts, mode), replace=mode == "replace",
                                 actor=account.actor(request), action=f"dataset_import_{mode}",
                                 snapshot_reason=f"before_import_{mode}")

    return application


def _warm(repository: DatasetStore):
    try:
        repository.evaluations()
    except Exception as exc:  # noqa: BLE001 - warming is best-effort
        log(logging.WARNING, "cache_warmup_failed", error=str(exc))


app = create_app()
