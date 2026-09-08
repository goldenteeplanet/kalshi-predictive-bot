import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

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
