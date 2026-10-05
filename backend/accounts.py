"""User accounts, password hashing (scrypt) and server-side sessions.

Passwords are stored only as salted scrypt hashes. Browser sessions are random
256-bit tokens kept in an httpOnly cookie; the database stores only their
SHA-256 hash, so a database leak does not reveal usable session tokens.
"""

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import hmac
import re
import secrets
from uuid import uuid4

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from backend import db as schema
from backend.db import Database, iso, utcnow

SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2 ** 14, 8, 1, 32
EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,63}$")
COMMON_PASSWORDS = {"password", "password1", "qwerty123", "1234567890", "12345678910", "admin12345",
                    "welcome123", "qwertyuiop", "career123", "halyk12345", "parol12345"}


GENERIC_LOGIN_ERROR = ("Не удалось войти: неверная почта или пароль, либо вход временно заблокирован. "
                       "Если ошибка повторяется, обратитесь к HR-администратору.")


class AccountError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        self.code = code
        self.status = status
        self.reason = code
        super().__init__(message)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            dklen=SCRYPT_LEN, maxmem=64 * 1024 * 1024)
    encode = lambda value: base64.b64encode(value).decode("ascii")  # noqa: E731
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${encode(salt)}${encode(digest)}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        algorithm, n, r, p, salt, digest = encoded.split("$")
        if algorithm != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r),
                                p=int(p), dklen=len(expected), maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Verifying against a dummy hash keeps login timing equal for unknown e-mails.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_password() -> str:
    """Readable temporary password: 4 groups, letters and digits, ~70 bits of entropy."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        groups = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4)]
        candidate = "-".join(groups)
        if any(c.isdigit() for c in candidate) and any(c.isalpha() for c in candidate):
            return candidate


def check_password_policy(password: str, email: str | None = None):
    if not isinstance(password, str) or len(password) < 10:
        raise AccountError("weak_password", "Пароль должен быть не короче 10 символов")
    if len(password) > 128:
        raise AccountError("weak_password", "Пароль должен быть не длиннее 128 символов")
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        raise AccountError("weak_password", "Пароль должен содержать буквы и цифры")
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS or (email and lowered == email.lower()) or len(set(password)) < 5:
        raise AccountError("weak_password", "Пароль слишком простой")


def normalize_email(email: str) -> str:
    value = (email or "").strip().lower()
    if not EMAIL_PATTERN.fullmatch(value):
        raise AccountError("invalid_email", "Укажите корректный адрес электронной почты")
    return value


@dataclass(frozen=True)
class User:
    user_id: str
    email: str
    name: str
    role: str
    employee_id: str | None
    must_change_password: bool
    is_active: bool
    consent_version: str | None
    consent_at: datetime | None
    created_at: datetime
    last_login_at: datetime | None
    locked_until: datetime | None
    has_api_token: bool = False

    def public(self) -> dict:
        return {"user_id": self.user_id, "email": self.email, "name": self.name, "role": self.role,
                "employee_id": self.employee_id, "must_change_password": self.must_change_password,
                "is_active": self.is_active, "consent_version": self.consent_version,
                "consent_at": iso(self.consent_at), "created_at": iso(self.created_at),
                "last_login_at": iso(self.last_login_at),
                "locked": bool(self.locked_until and self.locked_until > utcnow())}


def _user(row) -> User:
    return User(user_id=row.user_id, email=row.email, name=row.name, role=row.role,
                employee_id=row.employee_id, must_change_password=bool(row.must_change_password),
                is_active=bool(row.is_active), consent_version=row.consent_version,
                consent_at=row.consent_at, created_at=row.created_at, last_login_at=row.last_login_at,
                locked_until=row.locked_until, has_api_token=row.api_token_hash is not None)


class AccountStore:
    def __init__(self, database: Database, *, session_hours: int = 12, session_idle_minutes: int = 120,
                 max_failures: int = 5, lock_minutes: int = 15):
        self.db = database
        self.session_hours = session_hours
        self.session_idle = timedelta(minutes=session_idle_minutes)
        self.max_failures = max_failures
        self.lock_minutes = lock_minutes

    # ----------------------------------------------------------------- users
    def count(self) -> int:
        with self.db.read_tx() as connection:
            return connection.execute(select(func.count()).select_from(schema.users)).scalar_one()

    def get(self, user_id: str) -> User | None:
        with self.db.read_tx() as connection:
            row = connection.execute(select(schema.users).where(schema.users.c.user_id == user_id)).first()
            return _user(row) if row else None

    def by_email(self, email: str) -> User | None:
        with self.db.read_tx() as connection:
            row = connection.execute(select(schema.users).where(
                schema.users.c.email == email.strip().lower())).first()
            return _user(row) if row else None

    def list(self) -> list[User]:
        with self.db.read_tx() as connection:
            return [_user(row) for row in connection.execute(
                select(schema.users).order_by(schema.users.c.role, schema.users.c.email))]

    def create(self, *, email: str, name: str, role: str, employee_id: str | None = None,
               password: str | None = None, must_change_password: bool = True,
               user_id: str | None = None, api_token: str | None = None,
               consent_version: str | None = None) -> tuple[User, str | None]:
        """Create a user; returns (user, generated temporary password or None)."""
        if role not in {"employee", "hr"}:
            raise AccountError("invalid_role", "Роль должна быть employee или hr")
        if role == "employee" and not employee_id:
            raise AccountError("employee_required", "Для сотрудника нужен профиль (employee_id)")
        if role == "hr":
            employee_id = employee_id or None
        name = (name or "").strip()
        if not name or len(name) > 255:
            raise AccountError("invalid_name", "Укажите имя (до 255 символов)")
        email = normalize_email(email)
        generated = None
        if password is None and api_token is None:
            password = generated = generate_password()
        if password is not None and generated is None:
            check_password_policy(password, email)
        now = utcnow()
        values = dict(
            user_id=user_id or uuid4().hex, email=email, name=name, role=role, employee_id=employee_id,
            password_hash=hash_password(password) if password is not None else None,
            api_token_hash=token_hash(api_token) if api_token else None,
            must_change_password=must_change_password and password is not None,
            is_active=True, failed_attempts=0, created_at=now, updated_at=now,
            password_changed_at=now if password is not None else None,
            consent_version=consent_version, consent_at=now if consent_version else None,
        )
        try:
            with self.db.write_tx() as connection:
                connection.execute(insert(schema.users).values(**values))
        except IntegrityError as exc:
            if self.by_email(email) is None and role == "employee":
                raise AccountError("employee_has_account", "У этого сотрудника уже есть учётная запись", 409) from exc
            raise AccountError("user_exists", "Пользователь с такой почтой уже существует", 409) from exc
        return self.get(values["user_id"]), generated

    def upsert_service_account(self, *, user_id: str, role: str, employee_id: str | None, token: str):
        """Bearer-token accounts for integrations and tests (no password, no consent screen)."""
        now = utcnow()
        email = f"{user_id.lower()}@service.invalid"
        with self.db.write_tx() as connection:
            existing = connection.execute(select(schema.users.c.user_id).where(
                schema.users.c.user_id == user_id)).first()
            values = dict(role=role, employee_id=employee_id, api_token_hash=token_hash(token),
                          is_active=True, updated_at=now)
            if existing:
                connection.execute(update(schema.users).where(schema.users.c.user_id == user_id).values(**values))
            else:
                connection.execute(insert(schema.users).values(
                    user_id=user_id, email=email, name=f"Service account {user_id}", password_hash=None,
                    must_change_password=False, failed_attempts=0, created_at=now, **values))

    def set_active(self, user_id: str, active: bool) -> User:
        with self.db.write_tx() as connection:
            result = connection.execute(update(schema.users).where(schema.users.c.user_id == user_id).values(
                is_active=active, updated_at=utcnow(), failed_attempts=0, locked_until=None))
            if result.rowcount == 0:
                raise AccountError("user_not_found", "Пользователь не найден", 404)
            if not active:
                connection.execute(delete(schema.sessions).where(schema.sessions.c.user_id == user_id))
        return self.get(user_id)

    def reset_password(self, user_id: str) -> tuple[User, str]:
        password = generate_password()
        with self.db.write_tx() as connection:
            result = connection.execute(update(schema.users).where(schema.users.c.user_id == user_id).values(
                password_hash=hash_password(password), must_change_password=True, failed_attempts=0,
                locked_until=None, updated_at=utcnow(), password_changed_at=utcnow()))
            if result.rowcount == 0:
                raise AccountError("user_not_found", "Пользователь не найден", 404)
            connection.execute(delete(schema.sessions).where(schema.sessions.c.user_id == user_id))
        return self.get(user_id), password

    def set_password(self, user_id: str, password: str, *, must_change_password: bool = True):
        """Operator action (CLI, smoke checks): set a password without knowing the old one."""
        with self.db.read_tx() as connection:
            email = connection.execute(select(schema.users.c.email).where(
                schema.users.c.user_id == user_id)).scalar_one_or_none()
        if email is None:
            raise AccountError("user_not_found", "Пользователь не найден", 404)
        check_password_policy(password, email)
        with self.db.write_tx() as connection:
            connection.execute(update(schema.users).where(schema.users.c.user_id == user_id).values(
                password_hash=hash_password(password), must_change_password=must_change_password,
                failed_attempts=0, locked_until=None, is_active=True, updated_at=utcnow(),
                password_changed_at=utcnow()))
            connection.execute(delete(schema.sessions).where(schema.sessions.c.user_id == user_id))

    def change_password(self, user_id: str, current: str, new: str, keep_session_hash: str | None):
        with self.db.read_tx() as connection:
            row = connection.execute(select(schema.users).where(schema.users.c.user_id == user_id)).first()
        if row is None or not verify_password(current, row.password_hash):
            raise AccountError("invalid_password", "Текущий пароль указан неверно", 400)
        if current == new:
            raise AccountError("weak_password", "Новый пароль должен отличаться от текущего")
        check_password_policy(new, row.email)
        with self.db.write_tx() as connection:
            connection.execute(update(schema.users).where(schema.users.c.user_id == user_id).values(
                password_hash=hash_password(new), must_change_password=False, updated_at=utcnow(),
                password_changed_at=utcnow()))
            # Sign out every other device after a password change.
            condition = schema.sessions.c.user_id == user_id
            if keep_session_hash:
                condition = and_(condition, schema.sessions.c.token_hash != keep_session_hash)
            connection.execute(delete(schema.sessions).where(condition))

    def record_consent(self, user_id: str, version: str):
        with self.db.write_tx() as connection:
            connection.execute(update(schema.users).where(schema.users.c.user_id == user_id).values(
                consent_version=version, consent_at=utcnow(), updated_at=utcnow()))

    def withdraw_consent(self, user_id: str):
        with self.db.write_tx() as connection:
            connection.execute(update(schema.users).where(schema.users.c.user_id == user_id).values(
                consent_version=None, consent_at=None, updated_at=utcnow()))

    # ---------------------------------------------------------- authentication
    def authenticate(self, email: str, password: str) -> tuple[User | None, str]:
        """Verify credentials with per-account lockout.

        Returns (user, "ok") on success. Every failure raises the same generic
        401 so the response never reveals whether an e-mail exists, is locked
        or disabled; the precise reason is returned via AccountError.reason for
        the audit log only.
        """
        email = (email or "").strip().lower()
        with self.db.read_tx() as connection:
            row = connection.execute(select(schema.users).where(schema.users.c.email == email)).first()

        def fail(reason: str):
            error = AccountError("invalid_credentials", GENERIC_LOGIN_ERROR, 401)
            error.reason = reason
            raise error

        if row is None or row.password_hash is None:
            verify_password(password, _DUMMY_HASH)
            fail("unknown_user")
        if row.locked_until and row.locked_until > utcnow():
            verify_password(password, _DUMMY_HASH)
            fail("locked")
        if not verify_password(password, row.password_hash):
            self._register_failure(row.user_id)
            fail("wrong_password")
        with self.db.write_tx() as connection:
            # Re-check under the write lock: parallel wrong guesses may have locked the
            # account while this (correct) password was being verified.
            current = connection.execute(select(schema.users.c.locked_until, schema.users.c.is_active).where(
                schema.users.c.user_id == row.user_id)).one()
            if current.locked_until and current.locked_until > utcnow():
                fail("locked")
            if not current.is_active:
                fail("disabled")
            connection.execute(update(schema.users).where(schema.users.c.user_id == row.user_id).values(
                failed_attempts=0, locked_until=None, last_login_at=utcnow()))
        return self.get(row.user_id), "ok"

    def _register_failure(self, user_id: str):
        """Atomic counter: concurrent wrong guesses cannot overwrite each other."""
        with self.db.write_tx() as connection:
            failures = connection.execute(update(schema.users).where(schema.users.c.user_id == user_id).values(
                failed_attempts=schema.users.c.failed_attempts + 1).returning(
                schema.users.c.failed_attempts)).scalar_one()
            if failures >= self.max_failures:
                connection.execute(update(schema.users).where(schema.users.c.user_id == user_id).values(
                    failed_attempts=0, locked_until=utcnow() + timedelta(minutes=self.lock_minutes)))

    def by_api_token(self, token: str) -> User | None:
        if not token:
            return None
        with self.db.read_tx() as connection:
            row = connection.execute(select(schema.users).where(
                schema.users.c.api_token_hash == token_hash(token))).first()
        if row is None or not row.is_active:
            return None
        return _user(row)

    # --------------------------------------------------------------- sessions
    def create_session(self, user_id: str, *, ip: str | None, user_agent: str | None) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(32)
        now = utcnow()
        expires = now + timedelta(hours=self.session_hours)
        with self.db.write_tx() as connection:
            connection.execute(delete(schema.sessions).where(schema.sessions.c.expires_at < now))
            connection.execute(insert(schema.sessions).values(
                token_hash=token_hash(token), user_id=user_id, created_at=now, expires_at=expires,
                last_seen_at=now, ip=(ip or "")[:64] or None, user_agent=(user_agent or "")[:255] or None))
        return token, expires

    def session_user(self, token: str) -> tuple[User, str] | None:
        """Return (user, token_hash) for a live session; enforces absolute and idle expiry."""
        if not token or len(token) > 128:
            return None
        hashed = token_hash(token)
        now = utcnow()
        with self.db.read_tx() as connection:
            row = connection.execute(select(
                schema.users, schema.sessions.c.expires_at.label("session_expires_at"),
                schema.sessions.c.last_seen_at.label("session_seen_at")).join(
                schema.sessions, schema.sessions.c.user_id == schema.users.c.user_id).where(
                schema.sessions.c.token_hash == hashed)).first()
        if row is None:
            return None
        if (row.session_expires_at <= now or row.session_seen_at + self.session_idle <= now
                or not row.is_active):
            self.revoke(token)
            return None
        if now - row.session_seen_at > timedelta(minutes=1):
            with self.db.write_tx() as connection:
                connection.execute(update(schema.sessions).where(
                    schema.sessions.c.token_hash == hashed).values(last_seen_at=now))
        return _user(row), hashed

    def revoke(self, token: str):
        with self.db.write_tx() as connection:
            connection.execute(delete(schema.sessions).where(schema.sessions.c.token_hash == token_hash(token)))

    def revoke_all(self, user_id: str):
        with self.db.write_tx() as connection:
            connection.execute(delete(schema.sessions).where(schema.sessions.c.user_id == user_id))
