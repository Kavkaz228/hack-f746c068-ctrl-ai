"""Authentication dependencies: httpOnly session cookies for people, bearer tokens for services.

Browser sessions are checked for CSRF with SameSite=Strict cookies plus an
Origin check on state-changing requests. Users who must change their temporary
password or have not given consent to personal-data processing can reach only
the /auth endpoints until they do.
"""

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field, model_validator

from backend.accounts import AccountStore, User
from backend.models import Identifier, Model
from backend.storage import Actor

SESSION_COOKIE = "cq_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class Account(Model):
    """Service (bearer-token) account: integrations, smoke checks and tests."""
    account_id: Identifier
    token: str = Field(min_length=16)
    role: Literal["employee", "hr"]
    employee_id: Identifier | None = None

    @model_validator(mode="after")
    def employee_identity(self):
        if self.role == "employee" and self.employee_id is None:
            raise ValueError("Employee account requires employee_id")
        return self


@dataclass(frozen=True)
class Principal:
    user: User
    via: Literal["session", "token"]
    session_hash: str | None = None

    @property
    def account_id(self) -> str:
        return self.user.user_id

    @property
    def role(self) -> str:
        return self.user.role

    @property
    def employee_id(self) -> str | None:
        return self.user.employee_id

    def actor(self, request: Request) -> Actor:
        return Actor(self.user.user_id, self.user.role, client_ip(request))


bearer = HTTPBearer(auto_error=False)


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _unauthorized(message="Sign in or provide a valid bearer token"):
    return HTTPException(401, detail={"error": "unauthorized", "message": message},
                         headers={"WWW-Authenticate": "Bearer"})


def _origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        # Non-browser client: SameSite=Strict already keeps the cookie off cross-site requests.
        return True
    if origin == "null":
        return False
    parsed = urlsplit(origin)
    host = request.headers.get("host", "")
    if parsed.netloc and parsed.netloc.lower() == host.lower():
        return True
    settings = request.app.state.settings
    allowed = {value.rstrip("/").lower() for value in settings.trusted_origins + settings.cors_origins}
    return f"{parsed.scheme}://{parsed.netloc}".lower() in allowed


def current_principal(request: Request,
                      credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
    accounts: AccountStore = request.app.state.accounts
    authorization = request.headers.get("authorization")
    if authorization:
        if credentials is None:
            raise _unauthorized()
        user = accounts.by_api_token(credentials.credentials)
        if user is None:
            raise _unauthorized()
        return Principal(user, "token")
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        found = accounts.session_user(token)
        if found is not None:
            user, session_hash = found
            if request.method not in SAFE_METHODS and not _origin_allowed(request):
                raise HTTPException(403, detail={"error": "csrf_failed",
                                                 "message": "Request origin is not allowed"})
            return Principal(user, "session", session_hash)
    raise _unauthorized()


def current_account(request: Request, principal: Principal = Depends(current_principal)) -> Principal:
    """Principal allowed to use business endpoints (password changed, consent given)."""
    if principal.via == "session":
        if principal.user.must_change_password:
            raise HTTPException(403, detail={"error": "password_change_required",
                                             "message": "Change the temporary password first"})
        if principal.user.consent_version != request.app.state.settings.consent_version:
            raise HTTPException(403, detail={"error": "consent_required",
                                             "message": "Consent to personal data processing is required"})
    return principal


def require_hr(principal: Principal = Depends(current_account)) -> Principal:
    if principal.role != "hr":
        raise HTTPException(403, detail={"error": "forbidden", "message": "HR access required"})
    return principal


def require_employee_access(employee_id: str, principal: Principal = Depends(current_account)) -> Principal:
    if principal.role != "hr" and principal.employee_id != employee_id:
        raise HTTPException(403, detail={"error": "forbidden", "message": "Access to another employee is not allowed"})
    return principal
