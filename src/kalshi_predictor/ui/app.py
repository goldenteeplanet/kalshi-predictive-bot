import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy.exc import OperationalError

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.ui.routes import create_router


logger = logging.getLogger("kalshi_predictor.ui.audit")
_SAFE_PATH = re.compile(r"[^A-Za-z0-9_./-]")


def create_app(
    *,
    session_factory: object | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    hardened = os.environ.get("UI_SECURITY_HARDENED", "false").lower() == "true"
    app = FastAPI(
        title="Kalshi Predictive Bot Decision UI",
        description="Local demo-only review UI. No production live trading.",
        docs_url=None if hardened else "/docs",
        redoc_url=None if hardened else "/redoc",
        openapi_url=None if hardened else "/openapi.json",
    )
    allowed_hosts = [
        host.strip() for host in os.environ.get(
            "UI_ALLOWED_HOSTS",
            "127.0.0.1,localhost,testserver,kalshi-bot-01.taile570d1.ts.net,100.81.127.97",
        ).split(",") if host.strip()
    ]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.middleware("http")
    async def close_idle_local_connections(request: Request, call_next: Any) -> Any:
        started = time.monotonic()
        request_id = secrets.token_hex(8)
        safe_path = _SAFE_PATH.sub("_", request.url.path[:200])
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - audit failed requests without leaking inputs.
            logger.exception(
                "ui_audit request_id=%s method=%s path=%s status=500 latency_ms=%d",
                request_id, request.method, safe_path, int((time.monotonic() - started) * 1000),
            )
            raise
        if request.url.path != "/api/dashboard/v1/stream":
            response.headers["Connection"] = "close"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'; "
            "object-src 'none'; img-src 'self' data:; connect-src 'self'; "
            "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'"
        )
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "ui_audit request_id=%s method=%s path=%s status=%d latency_ms=%d",
            request_id, request.method, safe_path, response.status_code,
            int((time.monotonic() - started) * 1000),
        )
        return response

    @app.exception_handler(OperationalError)
    async def database_operational_error(
        request: Request,
        exc: OperationalError,
    ) -> HTMLResponse:
        del request
        if _is_database_busy(exc):
            return HTMLResponse(
                _busy_database_html(),
                status_code=503,
            )
        return HTMLResponse(
            "<h1>Database error</h1><p>A database error occurred.</p>",
            status_code=500,
        )

    app.include_router(
        create_router(
            session_factory=session_factory,  # type: ignore[arg-type]
            settings=settings or get_settings(),
        )
    )
    # Starlette otherwise assembles this lazily inside the first request.
    # Finalize it here so concurrent cold readers all see the same route stack.
    app.middleware_stack = app.build_middleware_stack()
    return app


class LazyApp:
    def __init__(self) -> None:
        self._app: FastAPI | None = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if self._app is None:
            self._app = create_app()
        await self._app(scope, receive, send)


app = LazyApp()


def _is_database_busy(exc: OperationalError) -> bool:
    return "database is locked" in str(exc).lower()


def _busy_database_html() -> str:
    return """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Database Busy</title>
    <link rel="stylesheet" href="/static/styles.css">
  </head>
  <body>
    <main>
      <section class="section-band">
        <h1>Database is busy</h1>
        <p>Database is busy. Try refreshing in a few seconds.</p>
      </section>
    </main>
  </body>
</html>
""".strip()
