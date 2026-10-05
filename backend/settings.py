"""Runtime configuration from environment variables (12-factor style).

Every option has a safe default for local development. Production values are
documented in docs/DEPLOY.md and .env.example; secrets are never read from files
inside the repository.
"""

from dataclasses import dataclass, field
import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BACKEND_DIR / "runtime"
# Same file name as the pre-SQL version, so existing local progress is migrated in place.
DEFAULT_SQLITE_PATH = RUNTIME_DIR / "state.sqlite3"


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _optional_bool(name: str) -> bool | None:
    value = os.environ.get(name, "").strip().lower()
    if not value or value == "auto":
        return None
    return value in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int = 0) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    number = int(value)
    if number < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return number


def _list(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def sqlite_url(path: str | Path) -> str:
    return "sqlite:///" + str(Path(path).resolve())


def normalize_database_url(url: str) -> str:
    """Accept postgres:// and postgresql:// forms from hosting panels; use the psycopg 3 driver."""
    url = url.strip()
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def database_url_from_env(state_path: str | Path | None = None) -> str:
    """Explicit argument wins, then CAREER_QUEST_DATABASE_URL, then a local SQLite file."""
    if state_path is not None:
        return normalize_database_url(str(state_path)) if "://" in str(state_path) else sqlite_url(state_path)
    url = os.environ.get("CAREER_QUEST_DATABASE_URL", "").strip()
    if url:
        return normalize_database_url(url)
    legacy = os.environ.get("CAREER_QUEST_STATE_PATH", "").strip()
    return sqlite_url(legacy or DEFAULT_SQLITE_PATH)


@dataclass
class Settings:
    environment: str = "development"
    # Completion policy: "instant" applies an employee's completion at once (demo);
    # "approval" stores a request that HR must approve before skills change.
    completion_mode: str = "instant"
    # None = automatic: Secure flag when the request arrived over HTTPS (directly or via a trusted proxy).
    cookie_secure: bool | None = None
    session_hours: int = 12
    session_idle_minutes: int = 120
    login_max_failures: int = 5
    login_lock_minutes: int = 15
    login_ip_limit_per_15min: int = 30
    ai_daily_limit: int = 20
    snapshot_keep: int = 20
    enable_docs: bool = False
    metrics_token: str | None = None
    cors_origins: list[str] = field(default_factory=list)
    trusted_origins: list[str] = field(default_factory=list)
    max_json_body_bytes: int = 1024 * 1024
    max_upload_body_bytes: int = 22 * 1024 * 1024
    consent_version: str = "2026-10"
    openai_model: str = "gpt-4o-mini"
    bootstrap_email: str | None = None
    bootstrap_password: str | None = None
    bootstrap_name: str = "HR administrator"

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        environment = os.environ.get("CAREER_QUEST_ENV", "development").strip().lower() or "development"
        mode = os.environ.get("CAREER_QUEST_COMPLETION_MODE", "").strip().lower()
        if not mode:
            mode = "approval" if environment == "production" else "instant"
        if mode not in {"instant", "approval"}:
            raise ValueError("CAREER_QUEST_COMPLETION_MODE must be instant or approval")
        settings = cls(
            environment=environment,
            completion_mode=mode,
            cookie_secure=_optional_bool("CAREER_QUEST_COOKIE_SECURE"),
            session_hours=_int("CAREER_QUEST_SESSION_HOURS", 12, 1),
            session_idle_minutes=_int("CAREER_QUEST_SESSION_IDLE_MINUTES", 120, 5),
            login_max_failures=_int("CAREER_QUEST_LOGIN_MAX_FAILURES", 5, 1),
            login_lock_minutes=_int("CAREER_QUEST_LOGIN_LOCK_MINUTES", 15, 1),
            login_ip_limit_per_15min=_int("CAREER_QUEST_LOGIN_IP_LIMIT", 30, 1),
            ai_daily_limit=_int("CAREER_QUEST_AI_DAILY_LIMIT", 20, 0),
            snapshot_keep=_int("CAREER_QUEST_SNAPSHOT_KEEP", 20, 1),
            enable_docs=_bool("CAREER_QUEST_ENABLE_DOCS", environment != "production"),
            metrics_token=os.environ.get("CAREER_QUEST_METRICS_TOKEN") or None,
            cors_origins=_list("CAREER_QUEST_CORS_ORIGINS"),
            trusted_origins=_list("CAREER_QUEST_TRUSTED_ORIGINS"),
            consent_version=os.environ.get("CAREER_QUEST_CONSENT_VERSION", "2026-10").strip() or "2026-10",
            openai_model=os.environ.get("CAREER_QUEST_OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
            bootstrap_email=os.environ.get("CAREER_QUEST_BOOTSTRAP_HR_EMAIL") or None,
            bootstrap_password=os.environ.get("CAREER_QUEST_BOOTSTRAP_HR_PASSWORD") or None,
            bootstrap_name=os.environ.get("CAREER_QUEST_BOOTSTRAP_HR_NAME", "HR administrator"),
        )
        for key, value in overrides.items():
            if value is not None:
                setattr(settings, key, value)
        return settings
