"""Actual acquisition, preparation, assembly rejection and owned monitoring chain."""

import json
from pathlib import Path

import httpx
import pytest
import test_overnight_activation as fixtures
from sqlalchemy import text
from test_paper_release_preparation import original_inputs

from kalshi_predictor.overnight_paper import discovery, weather_driver
from kalshi_predictor.overnight_paper.runtime_owner import acquire_runtime_owner

baseline_template = fixtures.baseline_template
prepared = fixtures.prepared


def configured(prepared):
    return prepared["settings"].model_copy(
        update={
            "dynamic_position_sizing_mode": "shadow",
            "advanced_risk_engine_mode": "shadow",
            "weather_v2_knyc_observation_enabled": False,
        }
    )


def install_public(prepared, monkeypatch, *, stale=False, fail=False):
    ticker, sources, _ = original_inputs(stale=stale)
    originals = {json.loads(s.payload)["url"]: json.loads(s.payload)["body"] for s in sources}
    calls = []
    client = httpx.Client

    def handler(request):
        calls.append(str(request.url))
        assert request.method == "GET"
        assert "authorization" not in request.headers
        with pytest.raises(ValueError, match="ALREADY_ACTIVE"):
            with acquire_runtime_owner(prepared["database_path"]):
                pytest.fail("driver lost owner across acquisition/monitor")
        url = str(request.url)
        if url.endswith("/markets/BTC-TEST"):
            shadow = prepared["shadow_payload"]
            return httpx.Response(
                200,
                json={
                    "market": {
                        "ticker": shadow["ticker"],
                        "event_ticker": shadow["event_ticker"],
                        "series_ticker": shadow["series_ticker"],
                        "close_time": shadow["close_time"],
                        "status": "active",
                    }
                },
            )
        if fail:
            return httpx.Response(503, json={"error": "fixture unavailable"})
        return httpx.Response(200, json=originals[url])

    monkeypatch.setattr(discovery.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        discovery.httpx,
        "Client",
        lambda **kwargs: client(**{**kwargs, "transport": httpx.MockTransport(handler)}),
    )
    return ticker, calls


def run(prepared, tmp_path, ticker, **kwargs):
    return weather_driver.run_weather_driver(
        session_factory=prepared["session_factory"],
        database_path=prepared["database_path"],
        archive_root=tmp_path / "capture",
        ticker=ticker,
        settings=configured(prepared),
        repository=Path(__file__).resolve().parents[1],
        code_sha="a" * 40,
        authorization=prepared["authorization"],
        objective_bytes=prepared["objective_bytes"],
        release=kwargs.pop("release", None),
        **kwargs,
    )


@pytest.mark.parametrize("stale", [True, False])
def test_live_public_data_reaches_real_kernel_and_exact_durable_rejection(
    prepared,
    tmp_path,
    monkeypatch,
    stale,
):
    ticker, calls = install_public(prepared, monkeypatch, stale=stale)
    result = run(prepared, tmp_path, ticker)
    assert result.state == result.supervisor.state == "STOPPED"
    assert result.generation == result.supervisor.generation
    assert any(url.endswith("/orderbook") for url in calls) is not stale
    assert calls[-1].endswith("/markets/BTC-TEST")
    with prepared["session_factory"]() as session:
        record = json.loads(
            session.execute(
                text("SELECT payload FROM overnight_sprint_cycles WHERE id=:id"),
                {"id": result.preparation_checkpoint},
            ).scalar_one()
        )
        driver = json.loads(
            session.execute(
                text("SELECT payload FROM overnight_sprint_cycles WHERE id=:id"),
                {"id": result.driver_checkpoint},
            ).scalar_one()
        )
        assert len(record["original_sources"]) == (6 if stale else 7)
        assert driver["assembly_blockers"] == list(result.assembly_blockers)
        assert session.execute(text("SELECT count(*) FROM paper_orders")).scalar_one() == 0
        count = session.execute(
            text("SELECT count(*) FROM forecasts WHERE ticker=:ticker"), {"ticker": ticker}
        ).scalar_one()
    if stale:
        assert result.preparation_state == "BLOCKED"
        assert count == 0
    else:
        assert result.preparation_state == "COMPUTED_UNQUALIFIED", result.assembly_blockers
        assert result.assembly_blockers == ("FROZEN_ORIGINAL_MODEL_REQUIRED",)
        assert record["records"]["risk"]["action"] == "ALLOW"
        assert record["records"]["sizing"]["proposed_contracts"] == 1
        assert count == 1
    with acquire_runtime_owner(prepared["database_path"]):
        pass


def test_acquisition_failure_preserved_and_existing_shadow_still_monitored(
    prepared, tmp_path, monkeypatch
):
    ticker, calls = install_public(prepared, monkeypatch, fail=True)
    result = run(prepared, tmp_path, ticker)
    assert result.preparation_checkpoint is None
    assert result.preparation_state == "ACQUISITION_NOT_COMPLETED"
    assert result.assembly_blockers
    assert len(calls) == 2 and calls[-1].endswith("/markets/BTC-TEST")
    assert result.supervisor.cycles_completed == 1


def test_no_serialized_model_is_invented_when_missing(prepared, tmp_path, monkeypatch):
    ticker, _ = install_public(prepared, monkeypatch)
    result = run(prepared, tmp_path, ticker, entries_enabled=False)
    assert result.assembly_blockers == ("FROZEN_ORIGINAL_MODEL_REQUIRED",)
    assert not result.supervisor.entries_enabled
    with prepared["session_factory"]() as session:
        assert session.execute(text("SELECT count(*) FROM paper_orders")).scalar_one() == 0


def test_entries_require_concrete_release_before_acquisition(prepared, tmp_path, monkeypatch):
    ticker, calls = install_public(prepared, monkeypatch)
    with pytest.raises(ValueError, match="ENTRY_RELEASE_EVIDENCE_REQUIRED"):
        run(prepared, tmp_path, ticker, entries_enabled=True)
    assert calls == []
