"""HTTP hardening: security headers, request IDs with JSON logs, metrics, body limits, rate limits."""

from collections import defaultdict, deque
import json
import logging
import re
import sys
import threading
import time
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException

LOGGER = logging.getLogger("career_quest")
REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

# The SPA loads nothing from third parties; inline styles are needed by the chart library.
APP_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
           "font-src 'self' data:; img-src 'self' data: blob:; "
           "connect-src 'self'; worker-src 'self'; manifest-src 'self'; frame-ancestors 'none'; "
           "base-uri 'self'; form-action 'self'; object-src 'none'")
# Swagger UI (development only) loads its bundle from jsDelivr.
DOCS_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https:; "
            "connect-src 'self'; frame-ancestors 'none'; object-src 'none'")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
                   "level": record.levelname.lower(), "logger": record.name, "message": record.getMessage()}
        for key, value in getattr(record, "fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO"):
    root = logging.getLogger("career_quest")
    root.setLevel(level)
    if any(getattr(handler, "_career_quest", False) for handler in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._career_quest = True
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False


def log(level: int, message: str, **fields):
    LOGGER.log(level, message, extra={"fields": fields})


class Metrics:
    """Tiny Prometheus-compatible counters; one instance per process."""

    def __init__(self):
        self.lock = threading.Lock()
        self.requests = defaultdict(int)
        self.duration_sum = 0.0
        self.duration_count = 0
        self.started = time.time()

    def observe(self, method: str, status: int, seconds: float):
        with self.lock:
            self.requests[(method, f"{status // 100}xx")] += 1
            self.duration_sum += seconds
            self.duration_count += 1

    def render(self, extra: dict[str, float] | None = None) -> str:
        lines = ["# TYPE career_quest_http_requests_total counter"]
        with self.lock:
            for (method, status), value in sorted(self.requests.items()):
                lines.append(f'career_quest_http_requests_total{{method="{method}",status="{status}"}} {value}')
            lines += ["# TYPE career_quest_http_request_duration_seconds summary",
                      f"career_quest_http_request_duration_seconds_sum {self.duration_sum:.6f}",
                      f"career_quest_http_request_duration_seconds_count {self.duration_count}",
                      "# TYPE career_quest_uptime_seconds gauge",
                      f"career_quest_uptime_seconds {time.time() - self.started:.0f}"]
        for name, value in (extra or {}).items():
            lines += [f"# TYPE {name} gauge", f"{name} {value}"]
        return "\n".join(lines) + "\n"


class HardeningMiddleware:
    """Pure ASGI middleware: request ID, access log, metrics, headers and body limits."""

    def __init__(self, app, *, metrics: Metrics | None = None, max_json_body: int = 1024 * 1024,
                 max_upload_body: int = 22 * 1024 * 1024, upload_paths: tuple[str, ...] = ("/dataset/import",),
                 access_log: bool = True, docs_paths: frozenset[str] = frozenset()):
        self.app = app
        self.metrics = metrics
        self.max_json_body = max_json_body
        self.max_upload_body = max_upload_body
        self.upload_paths = upload_paths
        self.access_log = access_log
        self.docs_paths = docs_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope["headers"]}
        incoming = headers.get("x-request-id", "")
        request_id = incoming if REQUEST_ID.fullmatch(incoming) else uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        path = scope.get("path", "")
        limit = self.max_upload_body if path.endswith(self.upload_paths) else self.max_json_body
        started = time.perf_counter()
        status_holder = {"status": 500}
        https = scope.get("scheme") == "https" or headers.get("x-forwarded-proto") == "https"
        root = scope.get("root_path", "")
        full_path = path if path.startswith(root) else root + path
        docs = full_path in self.docs_paths

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                status_holder["started"] = True
                response_headers = MutableHeaders(scope=message)
                response_headers.setdefault("X-Request-ID", request_id)
                response_headers.setdefault("X-Content-Type-Options", "nosniff")
                response_headers.setdefault("X-Frame-Options", "DENY")
                response_headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                response_headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
                response_headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
                response_headers.setdefault("Content-Security-Policy", DOCS_CSP if docs else APP_CSP)
                if https:
                    response_headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
                if "server" in response_headers:
                    del response_headers["server"]
                content_type = response_headers.get("content-type", "")
                if "application/json" in content_type:
                    response_headers.setdefault("Cache-Control", "no-store")
            await send(message)

        declared = headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            return await self._reject(scope, send_wrapper, limit)

        received = {"bytes": 0}

        async def receive_wrapper():
            message = await receive()
            if message["type"] == "http.request":
                received["bytes"] += len(message.get("body", b""))
                if received["bytes"] > limit:
                    raise _BodyTooLarge(limit)
            return message

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except _BodyTooLarge:
            if status_holder.get("started"):
                raise
            await self._reject(scope, send_wrapper, limit)
        finally:
            elapsed = time.perf_counter() - started
            if self.metrics is not None:
                self.metrics.observe(scope.get("method", "GET"), status_holder["status"], elapsed)
            if self.access_log:
                log(logging.INFO, "request", request_id=request_id, method=scope.get("method"),
                    path=full_path, status=status_holder["status"],
                    duration_ms=round(elapsed * 1000, 1))

    @staticmethod
    async def _reject(scope, send, limit):
        body = json.dumps({"error": "payload_too_large",
                           "message": f"Request body exceeds {limit // 1024} KiB"}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


class _BodyTooLarge(HTTPException):
    """Subclass of HTTPException so FastAPI's body parser re-raises it as a 413 response."""

    def __init__(self, limit: int = 0):
        super().__init__(413, detail={"error": "payload_too_large",
                                      "message": f"Request body exceeds {limit // 1024} KiB"})


class RateLimiter:
    """Sliding-window limiter per key (client IP). In-memory, per process."""

    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self.lock = threading.Lock()
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            queue = self.hits[key]
            while queue and queue[0] <= now - self.window:
                queue.popleft()
            if len(queue) >= self.limit:
                return False
            queue.append(now)
            if len(self.hits) > 10000:  # bound memory under address scanning
                for stale in [k for k, q in self.hits.items() if not q or q[-1] <= now - self.window][:5000]:
                    self.hits.pop(stale, None)
            return True
