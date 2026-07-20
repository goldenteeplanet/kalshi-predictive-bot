import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_forecast
from kalshi_predictor.data.schema import (
    CryptoFeature,
    Market,
    MarketSnapshot,
    RuntimeProvenanceEvent,
)
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.provenance.diagnostics import (
    build_market_decision_trace,
    build_provenance_diagnostics,
    build_provenance_drift_alerts,
)
from kalshi_predictor.ui.app import create_app


def test_prov11_bounded_diagnostics_verify_exact_chain(tmp_path, monkeypatch) -> None:
    session_factory = _session_factory(tmp_path)
    _disable_memory_capture(monkeypatch)
    report = _prov10_report(tmp_path)
    with session_factory() as session:
        _seed_chain(session)
        session.commit()
        payload = build_provenance_diagnostics(
            session, event_limit=10, prov10_report=report, execution_enabled=False
        )

    assert payload["status"] == "HEALTHY"
    assert payload["summary"]["total_events"] == 2
    assert payload["summary"]["events_verified"] == 2
    assert payload["summary"]["chain_valid"] is True
    assert payload["scheduler_certification"]["cycles_passed"] == 3
    assert payload["database_writes"] == 0


def test_prov11_detects_digest_tampering_and_execution_guard(tmp_path, monkeypatch) -> None:
    session_factory = _session_factory(tmp_path)
    _disable_memory_capture(monkeypatch)
    with session_factory() as session:
        _seed_chain(session)
        event = session.query(RuntimeProvenanceEvent).first()
        event.raw_json = json.dumps({"tampered": True})
        session.commit()
        payload = build_provenance_diagnostics(
            session, event_limit=10, execution_enabled=True
        )

    assert payload["status"] == "BLOCKED"
    assert payload["summary"]["failures"]["DIGEST_INVALID"] == 1
    assert payload["execution_enabled"] is True


def test_prov11_dashboard_preview_is_flag_guarded(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    session_factory = _session_factory(tmp_path)
    _disable_memory_capture(monkeypatch)
    _prov10_report(Path("."))
    with session_factory() as session:
        _seed_chain(session)
        session.commit()

    disabled = TestClient(create_app(
        session_factory=session_factory,
        settings=Settings(prov11_dashboard_preview_enabled=False),
    ))
    assert disabled.get("/system/provenance").status_code == 404

    enabled = TestClient(create_app(
        session_factory=session_factory,
        settings=Settings(prov11_dashboard_preview_enabled=True),
    ))
    page = enabled.get("/system/provenance")
    api = enabled.get("/api/provenance/diagnostics")
    system = enabled.get("/system")
    assert page.status_code == 200
    assert "Runtime Provenance Diagnostics" in page.text
    assert api.status_code == 200 and api.json()["read_only"] is True
    assert "Runtime Provenance" in system.text


def test_prov12_exact_market_trace_is_complete_and_read_only(tmp_path, monkeypatch) -> None:
    session_factory = _session_factory(tmp_path)
    _disable_memory_capture(monkeypatch)
    now = datetime(2026, 7, 17, 5, 0, tzinfo=UTC)
    with session_factory() as session:
        _seed_chain(session)
        session.commit()
        trace = build_market_decision_trace(
            session, "prov11", now=now + timedelta(minutes=5), stale_after_minutes=60
        )

    assert trace["ticker"] == "PROV11"
    assert trace["status"] == "HEALTHY"
    assert [stage["stage"] for stage in trace["stages"]] == [
        "FORECAST_CREATED", "RANKING_CREATED",
    ]
    assert all(stage["digest_valid"] for stage in trace["stages"])
    assert all(stage["feature_exists"] for stage in trace["stages"])
    assert trace["database_writes"] == 0


def test_prov12_drift_alerts_detect_stale_and_missing_ranking(tmp_path, monkeypatch) -> None:
    session_factory = _session_factory(tmp_path)
    _disable_memory_capture(monkeypatch)
    now = datetime(2026, 7, 17, 5, 0, tzinfo=UTC)
    with session_factory() as session:
        _seed_chain(session)
        ranking_event = session.query(RuntimeProvenanceEvent).filter_by(
            stage="RANKING_CREATED"
        ).one()
        session.delete(ranking_event)
        session.commit()
        alerts = build_provenance_drift_alerts(
            session, ticker_limit=10, now=now + timedelta(hours=2), stale_after_minutes=60
        )

    row = alerts["rows"][0]
    assert row["ticker"] == "PROV11"
    assert "TRACE_STALE" in row["alerts"]
    assert "RANKING_STAGE_MISSING" in row["alerts"]
    assert alerts["summary"]["alert_tickers"] == 1


def test_prov12_routes_are_separately_flag_guarded(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    session_factory = _session_factory(tmp_path)
    _disable_memory_capture(monkeypatch)
    _prov10_report(Path("."))
    with session_factory() as session:
        _seed_chain(session)
        session.commit()
    disabled = TestClient(create_app(
        session_factory=session_factory,
        settings=Settings(
            prov11_dashboard_preview_enabled=True,
            prov12_decision_trace_preview_enabled=False,
        ),
    ))
    assert disabled.get("/system/provenance/PROV11").status_code == 404
    enabled = TestClient(create_app(
        session_factory=session_factory,
        settings=Settings(
            prov11_dashboard_preview_enabled=True,
            prov12_decision_trace_preview_enabled=True,
        ),
    ))
    assert enabled.get("/system/provenance/PROV11").status_code == 200
    assert enabled.get("/api/provenance/traces/PROV11").status_code == 200
    assert enabled.get("/api/provenance/drift-alerts").status_code == 200
    assert "Per-Market Drift Preview" in enabled.get("/system/provenance").text


def _session_factory(tmp_path: Path):
    engine = init_db(f"sqlite:///{tmp_path / 'prov11.db'}")
    return get_session_factory(engine)


def _disable_memory_capture(monkeypatch) -> None:
    import kalshi_predictor.memory.capture as capture
    monkeypatch.setattr(capture, "capture_forecast_created", lambda *args, **kwargs: None)
    monkeypatch.setattr(capture, "capture_market_ranking", lambda *args, **kwargs: None)


def _seed_chain(session) -> None:
    now = datetime(2026, 7, 17, 5, 0, tzinfo=UTC)
    session.add(Market(
        ticker="PROV11", raw_json="{}", first_seen_at=now, last_seen_at=now
    ))
    session.add(CryptoFeature(
        id=12, symbol="BTC", source="test", generated_at=now,
        window_minutes=60, trend_direction="flat", raw_json="{}", created_at=now,
    ))
    session.flush()
    snapshot = MarketSnapshot(ticker="PROV11", captured_at=now, raw_market_json="{}")
    session.add(snapshot)
    session.flush()
    forecast = insert_forecast(session, {
        "ticker": "PROV11", "forecasted_at": now, "model_name": "crypto_v2",
        "yes_probability": Decimal("0.60"), "feature_json": {
            "crypto_feature_id": 12,
            "source_observation_ref": {"table": "crypto_prices", "id": 9},
        },
    }, market_snapshot_id=snapshot.id, attribution_enabled=True)
    insert_market_ranking(session, {
        "ticker": "PROV11", "forecast_model": "crypto_v2",
        "forecast_id": forecast.id, "market_snapshot_id": snapshot.id,
        "ranked_at": now, "raw_json": {},
    }, attribution_enabled=True)


def _prov10_report(root: Path) -> Path:
    path = root / "reports/phase_prov10/prov10_full_scheduler_cycle_certification.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "status": "PASSED", "cycles_passed": 3, "cycles_required": 3,
        "cycles": [{"new_oom": False} for _ in range(3)],
    }), encoding="utf-8")
    return path
