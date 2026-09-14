import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_paper_dashboard_weather_current import evidence  # noqa: F401

from kalshi_predictor.overnight_paper.dashboard import create_router as create_paper_router
from kalshi_predictor.ui.data_sources import create_router, source_snapshot


def test_inventory_alias_does_not_become_proof_of_value_or_rights(tmp_path):
    (tmp_path / "credential_readiness.json").write_text(
        json.dumps([{"configured": True, "aliases": ["BLA API Key.txt", "oddpool api key.txt"]}])
    )
    result = source_snapshot(tmp_path, tmp_path / "no-captures")
    rows = {row["provider_name"]: row for row in result["providers"]}
    assert rows["BLS"]["credential_notice"] == "LEGACY_BLA_ALIAS_ACCEPTED"
    assert rows["BLS"]["health_status"] == "API_FAILURE"
    assert rows["BLS"]["brier_delta"] is None
    assert rows["BLS"]["api_value_score"] is None
    assert rows["BLS"]["payment_recommendation"] == "DO_NOT_PAY_YET"
    assert rows["ODDPOOL"]["health_status"] == "RIGHTS_PENDING"


def test_malformed_inventory_is_unavailable_without_echoing_contents(tmp_path):
    (tmp_path / "credential_readiness.json").write_text('{"unexpected":"private detail"}')
    result = source_snapshot(tmp_path, tmp_path / "no-captures")
    assert result["inventory_status"] == "UNAVAILABLE"
    assert "private detail" not in str(result)


def test_routes_work_without_network_or_database(tmp_path):
    app = FastAPI()
    app.include_router(create_router(tmp_path, tmp_path / "no-captures"))
    with TestClient(app) as client:
        result = client.get("/api/data-sources")
        assert result.status_code == 200
        assert result.json()["research_only"] is True
        html = client.get("/data-sources")
        assert html.status_code == 200
        assert "No paired real-event evaluations" in html.text
        assert "RIGHTS_PENDING" in html.text


def test_nws_attempt_uses_same_readonly_verified_ledger(evidence, tmp_path, monkeypatch):  # noqa: F811
    path, now, preparation = evidence
    # An explicit persisted no-forecast result, not a count inferred from refusal text.
    preparation.update(records={}, decision=None)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE overnight_sprint_cycles SET payload=? WHERE id='weather-preparation:test'",
            (json.dumps(preparation),),
        )
    monkeypatch.setenv("OVERNIGHT_PAPER_DB", str(path))
    before = path.read_bytes()
    app = FastAPI()
    app.include_router(create_router(tmp_path, tmp_path / "no-captures"))
    app.include_router(create_paper_router())
    with TestClient(app) as client:
        sources = client.get("/api/data-sources").json()
        paper = client.get("/api/paper-live").json()
        weather = sources["weather_attempt"]
        for key in (
            "weather_evidence_at",
            "weather_provider_updated_at",
            "weather_provider_generated_at",
            "weather_source_state",
            "weather_last_attempt_blockers",
        ):
            assert weather[key] == paper[key]
        assert weather["weather_source_state"] == "STALE"
        assert weather["weather_evidence_at"] == now.isoformat()
        assert weather["weather_last_attempt_blockers"] == ["PROVIDER_CLOCK_STALE"]
        assert weather["weather_actual_forecast_count"] == 0
        nws = next(x for x in sources["providers"] if x["provider_name"] == "NWS")
        assert nws["health_status"] == "STALE"
        assert nws["forecast_count"] == 0
        assert nws["api_value_score"] is None and nws["paper_pnl_delta"] is None
        html = client.get("/data-sources").text
        assert "Latest same-ledger weather attempt" in html
        assert "PROVIDER_CLOCK_STALE" in html
        assert weather["weather_provider_updated_at"] in html
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure", ["hash", "missing"])
def test_weather_evidence_missing_or_invalid_never_claims_fresh(evidence, tmp_path, failure):  # noqa: F811
    path, _, preparation = evidence
    if failure == "hash":
        preparation["original_sources"][0]["sha256"] = "0" * 64
        with sqlite3.connect(path) as db:
            db.execute(
                "UPDATE overnight_sprint_cycles SET payload=? WHERE id='weather-preparation:test'",
                (json.dumps(preparation),),
            )
    else:
        path = tmp_path / "missing.db"
    result = source_snapshot(tmp_path, tmp_path / "no-captures", path)
    weather = result["weather_attempt"]
    assert weather["weather_provider_updated_at"] is None
    assert weather["weather_actual_forecast_count"] is None
    nws = next(x for x in result["providers"] if x["provider_name"] == "NWS")
    assert nws["health_status"] not in {"READY_FRESH", "READY_REUSED_FRESH"}
    if failure == "hash":
        assert weather["verification_error"] == "PAPER_DASHBOARD_EVIDENCE_INVALID"
    else:
        assert not path.exists()
