import logging

import pytest
from fastapi.testclient import TestClient

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.ui.app import create_app


@pytest.fixture
def session_factory(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'security.db'}")
    try:
        yield get_session_factory(engine)
    finally:
        engine.dispose()


def test_security_headers_and_no_store(session_factory) -> None:
    with TestClient(create_app(session_factory=session_factory, settings=Settings())) as client:
        response = client.get("/system/progress")
    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert len(response.headers["x-request-id"]) == 16


def test_audit_log_excludes_query_and_sensitive_value(caplog, session_factory) -> None:
    caplog.set_level(logging.INFO, logger="kalshi_predictor.ui.audit")
    with TestClient(create_app(session_factory=session_factory, settings=Settings())) as client:
        response = client.get("/system/progress?token=do-not-log")
    assert response.status_code == 200
    audit = "\n".join(
        record.getMessage() for record in caplog.records if "ui_audit" in record.getMessage()
    )
    assert "path=/system/progress" in audit
    assert "do-not-log" not in audit
    assert "token" not in audit


def test_hardened_mode_disables_schema_surfaces(monkeypatch, session_factory) -> None:
    monkeypatch.setenv("UI_SECURITY_HARDENED", "true")
    with TestClient(create_app(session_factory=session_factory, settings=Settings())) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_untrusted_host_is_rejected(monkeypatch, session_factory) -> None:
    monkeypatch.setenv("UI_ALLOWED_HOSTS", "testserver,kalshi-bot-01.taile570d1.ts.net")
    with TestClient(create_app(session_factory=session_factory, settings=Settings())) as client:
        response = client.get("/system/progress", headers={"host": "evil.example"})
    assert response.status_code == 400
