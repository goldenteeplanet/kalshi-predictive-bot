import os
from unittest.mock import Mock

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from kalshi_predictor.config import Settings
from kalshi_predictor.ui.runtime_safety import register_runtime_safety_route


def test_runtime_safety_uses_cached_flags_and_excludes_credentials(monkeypatch):
    settings = Settings(
        _env_file=None,
        ui_read_only=True,
        execution_enabled=False,
        execution_dry_run=True,
        kalshi_api_key_id="MUST_NOT_APPEAR",
        kalshi_private_key_path="PRIVATE_PATH_MUST_NOT_APPEAR",
    )
    app = FastAPI()
    router = APIRouter()
    register_runtime_safety_route(router, settings, session_mode="injected_unknown")
    app.include_router(router)
    monkeypatch.setenv("UI_READ_ONLY", "false")
    with TestClient(app) as client:
        response = client.get("/api/runtime-safety")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "schema_version": 1,
            "process_id": os.getpid(),
            "ui_read_only": True,
            "execution_enabled": False,
            "execution_dry_run": True,
            "execution_kill_switch": settings.execution_kill_switch,
            "configured_db_backend": settings.db_backend,
            "session_mode": "injected_unknown",
            "scope": "UI_ROUTER_CONFIGURATION",
        }
        assert "MUST_NOT_APPEAR" not in response.text
        assert client.post("/api/runtime-safety").status_code == 405


def test_main_router_safety_does_not_open_session_or_build_dashboards(monkeypatch):
    from kalshi_predictor.ui import routes

    monkeypatch.setattr(routes, "load_shell_status_context", lambda **kwargs: {})
    session_factory = Mock(side_effect=AssertionError("SESSION_MUST_NOT_OPEN"))
    app = FastAPI()
    app.include_router(
        routes.create_router(session_factory=session_factory, settings=Settings(_env_file=None))
    )
    for name in (
        "build_learning_dashboard",
        "database_status_card",
        "memory_health",
        "paper_liquidity_plan",
        "advanced_risk_card",
    ):
        monkeypatch.setattr(
            routes, name, Mock(side_effect=AssertionError("DASHBOARD_MUST_NOT_RUN"))
        )
    with TestClient(app) as client:
        response = client.get("/api/runtime-safety")
        assert response.status_code == 200
        assert response.json()["session_mode"] == "injected_unknown"
    session_factory.assert_not_called()


@pytest.mark.parametrize("read_only", [True, False])
def test_reports_session_mode_selected_at_router_construction(monkeypatch, read_only):
    from kalshi_predictor.ui import routes

    settings = Settings(_env_file=None, ui_read_only=read_only)
    monkeypatch.setattr(routes, "load_shell_status_context", lambda **kwargs: {})
    engine = object()
    monkeypatch.setattr(routes, "make_sqlite_read_only_engine", lambda: engine)
    monkeypatch.setattr(routes, "init_db", lambda: engine)
    factory = Mock(side_effect=AssertionError("SESSION_MUST_NOT_OPEN"))
    monkeypatch.setattr(routes, "get_session_factory", lambda value: factory)
    app = FastAPI()
    app.include_router(routes.create_router(settings=settings))
    # A later object change must not relabel an already constructed engine.
    settings.ui_read_only = not read_only
    with TestClient(app) as client:
        result = client.get("/api/runtime-safety").json()
    assert result["ui_read_only"] is not read_only
    assert result["session_mode"] == ("read_only_sqlite" if read_only else "read_write")
    factory.assert_not_called()
