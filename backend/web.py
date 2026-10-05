"""Serve the built web app and its API together: python -m backend.web."""

from contextlib import asynccontextmanager
import os
from pathlib import Path
import re

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles

from backend.api.app import create_app as create_api
from backend.api.security import HardeningMiddleware

SPA_PAGES = re.compile(r"(?:login|register|privacy|consent|account(?:/[a-z-]+)?|hr(?:/[a-z-]+)?"
                       r"|employee/[^/]+(?:/history)?)/?")


class FrontendFiles(StaticFiles):
    """Use the SPA entrypoint only for known frontend page routes; cache hashed assets forever."""

    async def get_response(self, path, scope):
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code == 404 and SPA_PAGES.fullmatch(path.replace("\\", "/")):
                response = await super().get_response("index.html", scope)
            else:
                raise
        normalized = path.replace("\\", "/")
        if normalized.startswith("assets/") and response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers.setdefault("Cache-Control", "no-cache")
        return response


def create_app(frontend_dir: str | Path | None = None, **api_options) -> FastAPI:
    frontend = Path(frontend_dir or os.environ.get(
        "CAREER_QUEST_FRONTEND_DIR", Path(__file__).resolve().parents[1] / "frontend" / "dist"))
    if not (frontend / "index.html").is_file():
        raise RuntimeError(f"Frontend build missing in {frontend}; run npm run build in frontend first")
    api = create_api(**api_options)

    @asynccontextmanager
    async def lifespan(application):
        # Mounted applications do not receive startup/shutdown automatically.
        async with api.router.lifespan_context(api):
            yield

    application = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    application.add_middleware(GZipMiddleware, minimum_size=1024)
    # Security headers for static pages; API responses already carry them (setdefault keeps one copy).
    settings = api.state.settings
    application.add_middleware(HardeningMiddleware, access_log=False, max_json_body=settings.max_json_body_bytes,
                               max_upload_body=settings.max_upload_body_bytes)
    application.state.api = api
    application.mount("/api", api)
    application.mount("/", FrontendFiles(directory=frontend, html=True))
    return application


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.web:create_app", factory=True, host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8000")), proxy_headers=True,
                forwarded_allow_ips=os.environ.get("CAREER_QUEST_FORWARDED_ALLOW_IPS", "127.0.0.1"),
                server_header=False, log_level=os.environ.get("CAREER_QUEST_UVICORN_LOG_LEVEL", "warning"),
                workers=int(os.environ.get("WEB_CONCURRENCY", "1")))
