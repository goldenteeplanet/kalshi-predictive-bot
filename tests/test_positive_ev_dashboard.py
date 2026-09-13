import json
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kalshi_predictor.ui.positive_ev import create_router, read_research, render_research


def test_missing_research_is_unknown_and_has_no_authority(tmp_path):
    html = render_research(read_research(tmp_path / "absent"))
    assert "Unknown" in html
    assert "cannot submit orders" in html
    assert "Stale or unavailable" in html


def test_future_and_stale_report_not_current_and_values_escaped(tmp_path):
    now = datetime(2026, 9, 10, tzinfo=UTC)
    path = tmp_path / "report.json"
    for delta in (timedelta(seconds=1), timedelta(minutes=-16)):
        path.write_text(json.dumps({
            "schema": "independent-research-v1", "generated_at": (now + delta).isoformat(),
            "rows": [{"ticker": "<script>alert(1)</script>"}],
        }))
        report = read_research(path, now=now)
        assert not report["fresh"]
        assert "<script>" not in render_research(report)


def test_malformed_report_fails_closed(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"schema": "independent-research-v1", "rows": [1]}))
    assert read_research(path)["rows"] == []


def test_route_is_read_only_and_displays_missing_costs(tmp_path, monkeypatch):
    path = tmp_path / "current.json"
    path.write_text(json.dumps({
        "schema": "independent-research-v1", "generated_at": datetime.now(UTC).isoformat(),
        "rows": [{"ticker": "BTC-RESEARCH", "net_ev": None}],
    }))
    monkeypatch.setenv("POSITIVE_EV_REPORT_PATH", str(path))
    app = FastAPI()
    app.include_router(create_router())
    with TestClient(app) as client:
        response = client.get("/positive-ev")
        assert response.status_code == 200
        assert "BTC-RESEARCH" in response.text and "Unknown" in response.text
        assert client.post("/positive-ev").status_code == 405
