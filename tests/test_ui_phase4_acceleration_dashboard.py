from datetime import UTC, datetime
from pathlib import Path

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.schema import CanonicalEvaluation
from kalshi_predictor.phase4cd.evidence import (
    EvidenceSummaryCache,
    _SummaryCacheEntry,
    cached_evidence_dashboard,
    evidence_dashboard,
)
from sqlalchemy import event


def test_summary_evidence_skips_deep_reconciliation(tmp_path: Path, monkeypatch) -> None:
    def forbidden(_session):
        raise AssertionError("deep reconciliation must not run for the dashboard summary")

    monkeypatch.setattr("kalshi_predictor.phase4cd.prospective.prospective_status", forbidden)
    monkeypatch.setattr("kalshi_predictor.phase4cd.operations.handoff_funnel", forbidden)
    factory = get_session_factory(init_db(f"sqlite:///{tmp_path / 'evidence.db'}"))

    with factory() as session:
        payload = evidence_dashboard(session)

    assert payload["deep_diagnostics_included"] is False
    assert "prospective_paired_evidence" not in payload
    assert "prospective_handoff_funnel" not in payload


def test_acceleration_dashboard_is_navigable_and_describes_safety_boundary() -> None:
    root = Path(__file__).parents[1]
    base = (root / "src/kalshi_predictor/ui/templates/base.html").read_text()
    page = (root / "src/kalshi_predictor/ui/templates/evidence.html").read_text()

    assert 'href="/evidence">Acceleration' in base
    assert "Acceleration &amp; evidence" in page
    assert "Live capital blocked" in page
    assert "/api/evidence?deep=true" in page
    assert "Read-only observability" in page


def test_summary_cache_hit_and_exact_expiration_boundary(monkeypatch) -> None:
    calls = 0

    def build(_session):
        nonlocal calls
        calls += 1
        return _valid_payload()

    monkeypatch.setattr("kalshi_predictor.phase4cd.evidence.evidence_dashboard", build)
    cache = EvidenceSummaryCache()
    now = [100.0]

    first = cached_evidence_dashboard(None, cache=cache, clock=lambda: now[0], ttl_seconds=30)
    now[0] = 129.999
    hit = cached_evidence_dashboard(None, cache=cache, clock=lambda: now[0], ttl_seconds=30)
    now[0] = 130.0
    expired = cached_evidence_dashboard(None, cache=cache, clock=lambda: now[0], ttl_seconds=30)

    assert calls == 2
    assert first["cache_status"] == "MISS"
    assert hit["cache_status"] == "HIT"
    assert hit["cache_age_seconds"] == 29.999
    assert expired["cache_status"] == "MISS"


def test_malformed_cache_entry_is_rejected_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        "kalshi_predictor.phase4cd.evidence.evidence_dashboard",
        lambda _session: _valid_payload(),
    )
    cache = EvidenceSummaryCache()
    cache._entries["summary"] = _SummaryCacheEntry(5.0, {"generated_at": "tampered"})

    result = cached_evidence_dashboard(None, cache=cache, clock=lambda: 6.0)

    assert result["cache_status"] == "MISS"
    assert result["partial_data"] is True
    assert "MALFORMED_CACHE_ENTRY_REJECTED" in result["warnings"]


def test_sql_aggregation_preserves_lane_semantics(tmp_path: Path) -> None:
    factory = get_session_factory(init_db(f"sqlite:///{tmp_path / 'aggregate.db'}"))
    with factory() as session:
        session.add_all(
            [
                _evaluation("one", "HISTORICAL_REPLAY", "event-a", "model-a", "0.1"),
                _evaluation("two", "HISTORICAL_REPLAY", "event-a", "model-b", "0.3"),
                _evaluation("three", "SHADOW", "event-b", "model-a", "0.2"),
            ]
        )
        session.commit()
        commit = session.commit
        session.commit = lambda: (_ for _ in ()).throw(AssertionError("summary committed"))
        result = evidence_dashboard(session)
        session.commit = commit

    assert result["historical"]["evaluated"] == 2
    assert result["historical"]["independent_events"] == 1
    assert result["historical"]["models_evaluated"] == 2
    assert float(result["historical"]["brier"]) == 0.2
    assert result["shadow"]["evaluated"] == 1
    assert result["total_independent_evaluated_events"] == 2
    assert result["deep_diagnostics_included"] is False


def test_empty_summary_has_stable_schema_and_query_ceiling(tmp_path: Path) -> None:
    engine = init_db(f"sqlite:///{tmp_path / 'empty.db'}")
    factory = get_session_factory(engine)
    query_count = 0

    @event.listens_for(engine, "before_cursor_execute")
    def count_query(*_args) -> None:
        nonlocal query_count
        query_count += 1

    with factory() as session:
        result = evidence_dashboard(session)

    assert query_count <= 11
    assert result["historical"]["evaluated"] == 0
    assert result["guarded_paper"]["orders"] == 0
    assert {
        "generated_at",
        "query_duration_ms",
        "cache_status",
        "cache_age_seconds",
        "deep_diagnostics_included",
        "data_freshness",
        "partial_data",
        "warnings",
    }.issubset(result)
    assert result["partial_data"] is True


def _valid_payload() -> dict:
    return {
        "generated_at": "2026-08-27T00:00:00+00:00",
        "query_duration_ms": 1.0,
        "source_query_duration_ms": 1.0,
        "cache_status": "MISS",
        "cache_age_seconds": 0.0,
        "deep_diagnostics_included": False,
        "data_freshness": {"status": "AVAILABLE", "latest_source_at": None},
        "partial_data": False,
        "warnings": [],
        "historical": {},
        "shadow": {},
        "guarded_paper": {},
    }


def _evaluation(
    evaluation_id: str,
    lane: str,
    event: str,
    model: str,
    brier: str,
) -> CanonicalEvaluation:
    now = datetime.now(UTC)
    return CanonicalEvaluation(
        evaluation_id=evaluation_id,
        source_lane=lane,
        market_ticker=f"market-{evaluation_id}",
        event_ticker=event,
        series_ticker="series",
        model=model,
        model_version="v1",
        decision_timestamp=now,
        forecast_timestamp=now,
        forecast_probability="0.5",
        snapshot_timestamp=now,
        executable_price="0.5",
        spread="0.01",
        liquidity="100",
        gross_edge="0.1",
        fees="0",
        slippage="0",
        net_edge="0.1",
        risk_result="PAPER_ONLY",
        settlement_timestamp=now,
        settlement_result="yes",
        realized_or_simulated_pnl="1",
        brier_contribution=brier,
        log_loss_contribution="0.4",
        independent_event_id=event,
        correlation_cluster_id=event,
        provenance="{}",
        created_at=now,
    )
