import logging

from fastapi.testclient import TestClient

from kalshi_predictor.ui.app import create_app


def test_security_headers_and_no_store() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/system/progress")
    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert len(response.headers["x-request-id"]) == 16


def test_audit_log_excludes_query_and_sensitive_value(caplog) -> None:
    caplog.set_level(logging.INFO, logger="kalshi_predictor.ui.audit")
    with TestClient(create_app()) as client:
        response = client.get("/system/progress?token=do-not-log")
    assert response.status_code == 200
    audit = "\n".join(record.getMessage() for record in caplog.records if "ui_audit" in record.getMessage())
    assert "path=/system/progress" in audit
    assert "do-not-log" not in audit
    assert "token" not in audit


def test_hardened_mode_disables_schema_surfaces(monkeypatch) -> None:
    monkeypatch.setenv("UI_SECURITY_HARDENED", "true")
    with TestClient(create_app()) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_untrusted_host_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("UI_ALLOWED_HOSTS", "testserver,kalshi-bot-01.taile570d1.ts.net")
    with TestClient(create_app()) as client:
        response = client.get("/system/progress", headers={"host": "evil.example"})
    assert response.status_code == 400
