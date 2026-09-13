from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kalshi_predictor.ui import research_journals as R
from kalshi_predictor.ui import routed_single_event as U
from kalshi_predictor.ui.positive_ev import create_router


def test_missing_root_is_unverified_readonly_no_creation(tmp_path):
    base, control = tmp_path / "absent", tmp_path / "control"
    result = U.read_single_event(base, control, now=U.START - timedelta(seconds=1))
    assert result["events"] == 0 and result["decisions"] == 0
    assert result["slots"][0]["status"] == "UNVERIFIED"
    assert not base.exists() and not control.exists()


def test_distinct_sections_preserve_existing_five_and_escape(monkeypatch, tmp_path):
    monkeypatch.setenv("POSITIVE_EV_COHORT_ROOT", str(tmp_path))
    monkeypatch.setenv("POSITIVE_EV_CONTROL_ROOT", str(tmp_path))
    monkeypatch.setenv("POSITIVE_EV_ROUTED_EVENT_ROOT", str(tmp_path))
    monkeypatch.setenv("POSITIVE_EV_ROUTED_EVENT_CONTROL_ROOT", str(tmp_path))
    monkeypatch.setattr(R, "read_cohort", lambda *a: dict(slots=[], decisions=20, events=5))
    monkeypatch.setattr(
        U,
        "read_single_event",
        lambda *a: dict(
            slots=[dict(slot=0, status="SCHEDULED", rows=[], event="<script>")],
            decisions=0,
            events=0,
        ),
    )
    app = FastAPI()
    app.include_router(create_router())
    with TestClient(app) as client:
        response = client.get("/positive-ev")
    assert response.status_code == 200
    text = response.text
    assert "20 durable decisions; 5 temporal events" in text
    assert (
        "Separate routed research event" in text
        and "0 durable decisions; 0 temporal events" in text
    )
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert (
        text.index("Prospective CF research journals")
        < text.index("Separate routed research event")
        < text.index("id='research-snapshot'")
    )
