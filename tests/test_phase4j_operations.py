import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kalshi_predictor.data.schema import (
    Base,
    Market,
    MarketSnapshot,
    PaperFill,
    PaperOrder,
    ProspectiveCaptureAlert,
    ProspectiveCaptureRejection,
    ProspectiveCaptureRun,
    ProspectivePairedCapture,
    ProspectiveStatusLineage,
)
from kalshi_predictor.ingest.cycle_handoff import write_committed_cycle_artifact
from kalshi_predictor.phase4cd.operations import (
    acquire_capture_lease,
    capture_status_lineage,
    handoff_funnel,
    persist_capture_alerts,
    prospective_health_report,
    receive_latest_snapshot_handoff,
    receive_snapshot_handoff_artifact,
    release_capture_lease,
    run_capture_scheduler,
    run_latest_handoff,
)
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session


def _sessions() -> tuple[Session, Session]:
    research_engine = create_engine("sqlite:///:memory:")
    source_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(research_engine)
    Base.metadata.create_all(source_engine)
    return Session(research_engine), Session(source_engine)


def test_health_distinguishes_stale_market_and_snapshot_gap() -> None:
    research, source = _sessions()
    now = datetime(2026, 8, 24, tzinfo=UTC)
    source.add(
        Market(
            ticker="T1",
            event_ticker="E1",
            status="open",
            raw_json="{}",
            first_seen_at=now - timedelta(hours=2),
            last_seen_at=now - timedelta(hours=2),
        )
    )
    source.commit()
    report = prospective_health_report(research, source, now=now)
    assert report.open_markets == 1
    assert report.open_snapshots == 0
    assert "SNAPSHOT_COLLECTION_GAP" in report.findings
    assert "MARKET_WRITER_STALE" in report.findings


def test_active_is_executable_status_without_admitting_closed() -> None:
    research, source = _sessions()
    now = datetime(2026, 8, 24, tzinfo=UTC)
    for ticker, status in (("ACTIVE", "active"), ("CLOSED", "closed")):
        source.add(
            Market(
                ticker=ticker,
                event_ticker=f"E-{ticker}",
                status=status,
                raw_json=json.dumps({"status": status}),
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        source.add(
            MarketSnapshot(
                ticker=ticker,
                captured_at=now,
                status=status,
                raw_market_json=json.dumps({"status": status}),
            )
        )
    source.commit()
    health = prospective_health_report(research, source, now=now)
    lineage = capture_status_lineage(research, source, now=now)
    assert health.eligible_pairs == 1
    assert lineage["fetched_inventory"] == 2
    assert lineage["strict_pair_eligible"] == 1
    assert lineage["loss_counts"] == {
        "STRICT_STATUS_ELIGIBLE": 1,
        "NORMALIZED_INACTIVE": 1,
    }
    assert research.scalar(select(func.count()).select_from(ProspectiveStatusLineage)) == 1
    assert (
        research.scalar(
            select(func.count())
            .select_from(ProspectiveCaptureAlert)
            .where(ProspectiveCaptureAlert.alert_type == "STATUS_LINEAGE_NORMALIZED_INACTIVE")
        )
        == 1
    )


def test_single_writer_lease_excludes_competitor_and_can_expire() -> None:
    research, _ = _sessions()
    now = datetime(2026, 8, 24, tzinfo=UTC)
    assert acquire_capture_lease(research, owner_id="one", now=now, ttl_seconds=10)
    assert not acquire_capture_lease(research, owner_id="two", now=now)
    assert acquire_capture_lease(research, owner_id="two", now=now + timedelta(seconds=11))
    release_capture_lease(research, owner_id="two")


def test_scheduler_is_bounded_idempotent_and_paper_isolated() -> None:
    research, source = _sessions()
    before = (
        source.scalar(select(func.count()).select_from(PaperOrder)),
        source.scalar(select(func.count()).select_from(PaperFill)),
    )
    result = run_capture_scheduler(research, source, owner_id="test", cycles=2, batch_limit=1)
    rerun = run_capture_scheduler(research, source, owner_id="test", cycles=1, batch_limit=1)
    after = (
        source.scalar(select(func.count()).select_from(PaperOrder)),
        source.scalar(select(func.count()).select_from(PaperFill)),
    )
    assert result["cycles"] == 2
    assert rerun["captured"] == 0
    assert result["gh2_imported"] is False
    assert before == after == (0, 0)


def test_latency_and_noncausal_rejection_alerts_are_idempotent() -> None:
    research, _ = _sessions()
    now = datetime(2026, 8, 24, tzinfo=UTC)
    run = ProspectiveCaptureRun(
        run_id="r",
        status="EXHAUSTED",
        scanned=1,
        captured=1,
        rejected=1,
        rejection_counts_json="{}",
        config_json="{}",
        started_at=now,
        updated_at=now,
    )
    research.add(run)
    research.add(
        ProspectivePairedCapture(
            capture_id="c",
            run_id="r",
            ticker="T",
            event_ticker="E",
            snapshot_id=1,
            snapshot_timestamp=now,
            snapshot_hash="s",
            feature_ids_json="{}",
            feature_hashes_json="{}",
            source_observations_json="{}",
            market_probability="0.5",
            crypto_probability="0.5",
            model_versions_json="{}",
            bundle_hash="b",
            latency_json=json.dumps({"source_to_feature_ms": 40000}),
            persisted_at=now,
        )
    )
    research.add(
        ProspectiveCaptureRejection(
            rejection_id="x",
            run_id="r",
            snapshot_id=2,
            ticker="T2",
            event_ticker="E2",
            snapshot_timestamp=now,
            reason="SOURCE_AFTER_FEATURE",
            details_json="{}",
            created_at=now,
        )
    )
    research.commit()
    assert persist_capture_alerts(research, run_id="r", now=now) == 2
    assert persist_capture_alerts(research, run_id="r", now=now) == 2
    assert research.scalar(select(func.count()).select_from(ProspectiveCaptureAlert)) == 2


def test_handoff_received_duplicate_and_expired_are_reason_coded() -> None:
    research, source = _sessions()
    now = datetime(2026, 8, 24, 12, tzinfo=UTC)
    source.add(
        MarketSnapshot(
            ticker="FRESH",
            captured_at=now - timedelta(seconds=2),
            status="active",
            raw_market_json='{"status":"active"}',
        )
    )
    source.commit()
    first = receive_latest_snapshot_handoff(research, source, now=now)
    duplicate = receive_latest_snapshot_handoff(research, source, now=now)
    assert first["status"] == "RECEIVED"
    assert first["active_snapshots"] == 1
    assert duplicate["status"] == "DUPLICATE"
    assert handoff_funnel(research)["counts"] == {"RECEIVED": 1, "DUPLICATE": 1}

    research2, source2 = _sessions()
    source2.add(
        MarketSnapshot(
            ticker="STALE",
            captured_at=now - timedelta(minutes=6),
            status="active",
            raw_market_json='{"status":"active"}',
        )
    )
    source2.commit()
    expired = receive_latest_snapshot_handoff(research2, source2, now=now)
    assert expired["status"] == "EXPIRED"
    assert (
        research2.scalar(
            select(func.count())
            .select_from(ProspectiveCaptureAlert)
            .where(ProspectiveCaptureAlert.alert_type == "HANDOFF_LATENCY_EXPIRED")
        )
        == 1
    )
    assert run_latest_handoff(research2, source2, owner_id="test", now=now)["captured"] == 0


def test_handoff_runner_is_bounded_and_exactly_once(monkeypatch) -> None:
    from kalshi_predictor.phase4cd.prospective import CaptureResult

    research, source = _sessions()
    now = datetime(2026, 8, 24, 12, tzinfo=UTC)
    source.add(
        MarketSnapshot(
            ticker="T",
            captured_at=now,
            status="active",
            raw_market_json='{"status":"active"}',
        )
    )
    source.commit()
    observed: dict[str, object] = {}

    def fake_capture(research, source, **kwargs):
        observed.update(kwargs)
        return CaptureResult("run", "EXHAUSTED", 1, 1, 0, {}, 1, 0)

    monkeypatch.setattr(
        "kalshi_predictor.phase4cd.operations.capture_prospective_pairs", fake_capture
    )
    first = run_latest_handoff(research, source, owner_id="one", batch_limit=7, now=now)
    retry = run_latest_handoff(research, source, owner_id="one", batch_limit=7, now=now)
    assert first["status"] == "COMPLETED"
    assert first["captured"] == 1
    assert retry["status"] == "DUPLICATE"
    assert observed["limit"] == 7
    assert observed["cycle_watermark"] == now
    assert handoff_funnel(research)["counts"] == {
        "RECEIVED": 1,
        "ACCEPTED": 1,
        "COMPLETED": 1,
        "DUPLICATE": 1,
    }


def test_atomic_generic_artifact_is_validated_before_research_consumption(
    tmp_path: Path,
) -> None:
    research, source = _sessions()
    now = datetime(2026, 8, 24, 12, tzinfo=UTC)
    source.add(
        MarketSnapshot(
            ticker="T",
            captured_at=now,
            status="active",
            raw_market_json='{"status":"active"}',
        )
    )
    source.commit()
    artifact = tmp_path / "cycle.json"
    payload = write_committed_cycle_artifact(source, output_path=artifact)
    assert payload is not None
    assert artifact.is_file()
    accepted = receive_snapshot_handoff_artifact(research, source, artifact_path=artifact, now=now)
    assert accepted["status"] == "RECEIVED"

    source.add(
        MarketSnapshot(
            ticker="NEW",
            captured_at=now + timedelta(seconds=10),
            status="active",
            raw_market_json='{"status":"active"}',
        )
    )
    source.commit()
    newer_cycle_does_not_invalidate = receive_snapshot_handoff_artifact(
        research, source, artifact_path=artifact, now=now + timedelta(seconds=10)
    )
    assert newer_cycle_does_not_invalidate["status"] == "DUPLICATE"

    original = source.scalar(select(MarketSnapshot).where(MarketSnapshot.ticker == "T"))
    assert original is not None
    original.raw_market_json = '{"status":"tampered"}'
    source.commit()
    mismatch = receive_snapshot_handoff_artifact(
        research, source, artifact_path=artifact, now=now + timedelta(seconds=10)
    )
    assert mismatch["status"] == "ARTIFACT_SOURCE_MISMATCH"
