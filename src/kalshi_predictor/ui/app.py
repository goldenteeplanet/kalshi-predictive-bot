from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.ui.routes import create_router


def create_app(
    *,
    session_factory: object | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Kalshi Predictive Bot Decision UI",
        description="Local demo-only review UI. No production live trading.",
    )
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.middleware("http")
    async def close_idle_local_connections(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        if request.url.path != "/api/dashboard/v1/stream":
            response.headers["Connection"] = "close"
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
