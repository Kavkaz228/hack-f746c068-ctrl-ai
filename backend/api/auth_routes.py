"""Sign-in, sessions, password change, consent and HR-managed user accounts."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from backend.accounts import AccountError, AccountStore, User
from backend.api.auth import (
    SESSION_COOKIE, Principal, _origin_allowed, client_ip, current_principal, require_hr,
)
from backend.models import Model
from backend.storage import Actor, DatasetStore

router = APIRouter()


class Identity(Model):
    account_id: str
    role: Literal["employee", "hr"]
    employee_id: str | None
    name: str
    email: str | None
    auth: Literal["session", "token"]
    must_change_password: bool
    consent_required: bool
    consent_version: str
    completion_mode: Literal["instant", "approval"]


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class ConsentRequest(BaseModel):
    version: str = Field(min_length=1, max_length=32)
    accepted: Literal[True]


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    name: str = Field(min_length=1, max_length=255)
    role: Literal["employee", "hr"]
    employee_id: str | None = Field(default=None, max_length=64)


class UserOut(Model):
    user_id: str
    email: str
    name: str
    role: str
    employee_id: str | None
    must_change_password: bool
    is_active: bool
    consent_version: str | None
    consent_at: str | None
    created_at: str | None
    last_login_at: str | None
    locked: bool


class UserWithPassword(Model):
    user: UserOut
    temporary_password: str


def accounts(request: Request) -> AccountStore:
    return request.app.state.accounts


def store(request: Request) -> DatasetStore:
    return request.app.state.store


def identity(request: Request, user: User, via: str) -> Identity:
    settings = request.app.state.settings
    required = settings.consent_version
    session = via == "session"
    return Identity(account_id=user.user_id, role=user.role, employee_id=user.employee_id, name=user.name,
                    email=None if user.email.endswith("@service.invalid") else user.email, auth=via,
                    must_change_password=session and user.must_change_password,
                    consent_required=session and user.consent_version != required,
                    consent_version=required, completion_mode=settings.completion_mode)


def _secure(request: Request) -> bool:
    configured = request.app.state.settings.cookie_secure
    return request.url.scheme == "https" if configured is None else configured


def set_session_cookie(request: Request, response: Response, token: str):
    settings = request.app.state.settings
    response.set_cookie(SESSION_COOKIE, token, max_age=settings.session_hours * 3600, httponly=True,
                        secure=_secure(request), samesite="strict", path="/")


def clear_session_cookie(request: Request, response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict", secure=_secure(request))


@router.post("/auth/login", response_model=Identity)
def login(body: LoginRequest, request: Request, response: Response, users: AccountStore = Depends(accounts),
          repository: DatasetStore = Depends(store)):
    ip = client_ip(request)
    if not _origin_allowed(request):
        raise HTTPException(403, detail={"error": "csrf_failed", "message": "Request origin is not allowed"})
    if not request.app.state.login_limiter.allow(ip or "unknown"):
        raise HTTPException(429, detail={"error": "too_many_attempts",
                                         "message": "Слишком много попыток входа. Повторите позже."},
                            headers={"Retry-After": "900"})
    try:
        user, _ = users.authenticate(body.email, body.password)
    except AccountError as exc:
        repository.audit(Actor(None, None, ip), "login_failed", body.email.strip().lower()[:200], {"reason": exc.reason})
        raise
    token, _ = users.create_session(user.user_id, ip=ip, user_agent=request.headers.get("user-agent"))
    set_session_cookie(request, response, token)
    repository.audit(Actor(user.user_id, user.role, ip), "login_succeeded", user.user_id)
    return identity(request, user, "session")


@router.post("/auth/logout")
def logout(request: Request, response: Response, users: AccountStore = Depends(accounts)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        users.revoke(token)
    clear_session_cookie(request, response)
    return {"status": "signed_out"}


@router.get("/auth/me", response_model=Identity)
def me(request: Request, principal: Principal = Depends(current_principal)):
    return identity(request, principal.user, principal.via)


@router.post("/auth/password", response_model=Identity)
def change_password(body: PasswordChange, request: Request, principal: Principal = Depends(current_principal),
                    users: AccountStore = Depends(accounts), repository: DatasetStore = Depends(store)):
    if principal.via != "session":
        raise HTTPException(400, detail={"error": "not_supported", "message": "Service accounts have no password"})
    users.change_password(principal.user.user_id, body.current_password, body.new_password, principal.session_hash)
    repository.audit(principal.actor(request), "password_changed", principal.user.user_id)
    return identity(request, users.get(principal.user.user_id), "session")


@router.post("/auth/consent", response_model=Identity)
def give_consent(body: ConsentRequest, request: Request, principal: Principal = Depends(current_principal),
                 users: AccountStore = Depends(accounts), repository: DatasetStore = Depends(store)):
    required = request.app.state.settings.consent_version
    if body.version != required:
        raise HTTPException(409, detail={"error": "consent_version_mismatch",
                                         "message": f"Current consent version is {required}"})
    users.record_consent(principal.user.user_id, required)
    repository.audit(principal.actor(request), "consent_given", principal.user.user_id, {"version": required})
    return identity(request, users.get(principal.user.user_id), principal.via)


@router.delete("/auth/consent", response_model=Identity)
def withdraw_consent(request: Request, principal: Principal = Depends(current_principal),
                     users: AccountStore = Depends(accounts), repository: DatasetStore = Depends(store)):
    users.withdraw_consent(principal.user.user_id)
    repository.audit(principal.actor(request), "consent_withdrawn", principal.user.user_id)
    return identity(request, users.get(principal.user.user_id), principal.via)


# ------------------------------------------------------------------ HR: users

def _public(user: User) -> UserOut:
    return UserOut(**user.public())


@router.get("/users", response_model=list[UserOut])
def list_users(principal: Principal = Depends(require_hr), users: AccountStore = Depends(accounts)):
    return [_public(user) for user in users.list() if not user.email.endswith("@service.invalid")]


@router.post("/users", response_model=UserWithPassword, status_code=201)
def create_user(body: UserCreate, request: Request, principal: Principal = Depends(require_hr),
                users: AccountStore = Depends(accounts), repository: DatasetStore = Depends(store)):
    employee_id = (body.employee_id or "").strip() or None
    if body.role == "employee":
        if not employee_id or not repository.employee_exists(employee_id):
            raise AccountError("employee_not_found", "Профиль сотрудника не найден", 404)
        if any(user.employee_id == employee_id and user.role == "employee" for user in users.list()):
            raise AccountError("employee_has_account", "У этого сотрудника уже есть учётная запись", 409)
    user, password = users.create(email=body.email, name=body.name, role=body.role,
                                  employee_id=employee_id if body.role == "employee" else None)
    repository.audit(principal.actor(request), "user_created", user.user_id,
                     {"role": user.role, "employee_id": user.employee_id})
    return UserWithPassword(user=_public(user), temporary_password=password)


@router.post("/users/{user_id}/reset-password", response_model=UserWithPassword)
def reset_password(user_id: str, request: Request, principal: Principal = Depends(require_hr),
                   users: AccountStore = Depends(accounts), repository: DatasetStore = Depends(store)):
    user, password = users.reset_password(user_id)
    repository.audit(principal.actor(request), "password_reset", user_id)
    return UserWithPassword(user=_public(user), temporary_password=password)


@router.post("/users/{user_id}/disable", response_model=UserOut)
def disable_user(user_id: str, request: Request, principal: Principal = Depends(require_hr),
                 users: AccountStore = Depends(accounts), repository: DatasetStore = Depends(store)):
    if user_id == principal.user.user_id:
        raise AccountError("cannot_disable_self", "Нельзя отключить собственную учётную запись", 400)
    user = users.set_active(user_id, False)
    repository.audit(principal.actor(request), "user_disabled", user_id)
    return _public(user)


@router.post("/users/{user_id}/enable", response_model=UserOut)
def enable_user(user_id: str, request: Request, principal: Principal = Depends(require_hr),
                users: AccountStore = Depends(accounts), repository: DatasetStore = Depends(store)):
    user = users.set_active(user_id, True)
    repository.audit(principal.actor(request), "user_enabled", user_id)
    return _public(user)
