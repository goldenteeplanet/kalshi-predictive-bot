import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kalshi_predictor.ui import multiasset_progress_view as V
from kalshi_predictor.ui.positive_ev import create_router

NOW = datetime(2026, 9, 12, 5, 16, tzinfo=UTC)


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    # Portable functional fixture; trust refusal is tested separately.
    monkeypatch.setattr(V, "_trusted", lambda path: not path.is_symlink())
    payload = {
        "scope": "DEDUPLICATED_PROGRESS_WITH_SEPARATE_SOURCE_STRATA",
        "tool_sha256": V.TOOL_SHA,
        "execution_authority": False,
        "independent_event_n": None,
        "pooled_model_metrics": None,
        "at": NOW.isoformat(),
        "selected_events": [{
            "asset": "BTC", "event": "KXBTC-26SEP1201", "source": "original",
            "capture": "VERIFIED_PROSPECTIVE_CAPTURE", "decisions": 4,
            "outcome": "VERIFIED_OFFICIAL_EVALUATION", "score_rows": 20,
        }],
        "distinct_captured_events": 1,
        "selected_evaluated_events": 1,
        "selected_research_decisions": 4,
        "by_asset": {asset: int(asset == "BTC") for asset in V.ASSETS},
        "strata": {
            name: {"as_of": (NOW - timedelta(minutes=4)).isoformat()}
            for name in ("original", "supplement")
        },
    }
    path = tmp_path / "combined-cohort-progress-wave-0-20260912.json"
    path.write_text(json.dumps(payload))
    return tmp_path, path, payload


def test_progress_separates_captures_evaluations_and_paper(snapshot):
    directory, _, _ = snapshot
    result = V.read_progress(directory, now=NOW)
    assert result["available"] and not result["stale"]
    assert result["distinct_captured_events"] == 1
    assert result["selected_research_decisions"] == 4
    html = V.render_progress(result)
    assert "1 captured events" in html and "1 officially evaluated" in html
    assert "not paper settlements" in html and "not certified independent" in html


@pytest.mark.parametrize("field,value", [
    ("distinct_captured_events", 2), ("selected_research_decisions", True),
    ("selected_evaluated_events", 0), ("execution_authority", True),
    ("tool_sha256", "wrong"), ("independent_event_n", 1),
    ("at", "2099-01-01T00:00:00Z"), ("at", "2026-09-12T05:16:00"),
])
def test_invalid_audit_is_unavailable_not_zero(snapshot, field, value):
    directory, path, payload = snapshot
    payload[field] = value
    path.write_text(json.dumps(payload))
    result = V.read_progress(directory, now=NOW)
    assert result == {"available": False}
    assert "Counts are unknown" in V.render_progress(result)


def test_duplicate_events_cannot_inflate_progress(snapshot):
    directory, path, payload = snapshot
    payload["selected_events"] *= 2
    path.write_text(json.dumps(payload))
    assert not V.read_progress(directory, now=NOW)["available"]


@pytest.mark.parametrize("score_rows", [0, 16, 18, 20])
def test_verified_evaluation_accepts_available_model_score_count(snapshot, score_rows):
    directory, path, payload = snapshot
    payload["selected_events"][0]["score_rows"] = score_rows
    path.write_text(json.dumps(payload))
    result = V.read_progress(directory, now=NOW)
    assert result["available"]
    assert result["selected_evaluated_events"] == 1
    assert result["selected_research_decisions"] == 4


@pytest.mark.parametrize("score_rows", [-1, 21, True, "18", None, 18.0])
def test_invalid_model_score_count_remains_unavailable(snapshot, score_rows):
    directory, path, payload = snapshot
    payload["selected_events"][0]["score_rows"] = score_rows
    path.write_text(json.dumps(payload))
    assert V.read_progress(directory, now=NOW) == {"available": False}


def test_stale_source_retains_counts_with_explicit_age(snapshot):
    directory, _, _ = snapshot
    report = V.read_progress(directory, now=NOW + timedelta(hours=2))
    assert report["available"] and report["stale"]
    assert "Older saved audit" in V.render_progress(report)
    assert report["distinct_captured_events"] == 1


def test_untrusted_or_oversized_reports_fail_closed(snapshot, monkeypatch):
    directory, path, _ = snapshot
    path.write_bytes(b" " * (V.MAX_BYTES + 1))
    assert not V.read_progress(directory, now=NOW)["available"]
    monkeypatch.setattr(V, "_trusted", lambda path: False)
    assert not V.read_progress(directory, now=NOW)["available"]


def test_router_renders_configured_audit_without_order_surface(snapshot, monkeypatch):
    directory, _, _ = snapshot
    monkeypatch.setenv("POSITIVE_EV_MULTI_ASSET_PROGRESS_ROOT", str(directory))
    monkeypatch.delenv("POSITIVE_EV_COHORT_ROOT", raising=False)
    monkeypatch.delenv("POSITIVE_EV_ROUTED_EVENT_ROOT", raising=False)
    original = V.read_progress
    monkeypatch.setattr(V, "read_progress", lambda path: original(path, now=NOW))
    app = FastAPI()
    app.include_router(create_router())
    response = TestClient(app).get("/positive-ev")
    assert response.status_code == 200
    assert "id='multiasset-progress'" in response.text
    assert "1 captured events" in response.text
    assert "<form" not in response.text
