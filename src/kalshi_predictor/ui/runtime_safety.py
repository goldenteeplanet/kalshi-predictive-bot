"""Small runtime safety observation that does not depend on database health."""

import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from kalshi_predictor.config import Settings


def create_runtime_safety_router(settings: Settings, *, session_mode: str) -> APIRouter:
    router = APIRouter()

    @router.get("/api/runtime-safety")
    def runtime_safety() -> JSONResponse:
        # Use the same resolved object as the main router. Do not reload settings,
        # acquire a session, inspect credentials, or invoke dashboard summaries.
        return JSONResponse(
            {
                "schema_version": 1,
                "process_id": os.getpid(),
                "ui_read_only": settings.ui_read_only,
                "execution_enabled": settings.execution_enabled,
                "execution_dry_run": settings.execution_dry_run,
                "execution_kill_switch": settings.execution_kill_switch,
                "configured_db_backend": settings.db_backend,
                "session_mode": session_mode,
                "scope": "UI_ROUTER_CONFIGURATION",
            },
            headers={"Cache-Control": "no-store"},
        )

    return router
