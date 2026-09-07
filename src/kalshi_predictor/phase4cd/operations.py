"""Operational controls for the isolated Phase 4I prospective research lane."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kalshi_predictor.crypto.semantics import EXACT_LINK, terms_from_link_payload
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    CryptoFeature,
    CryptoMarketLink,
    Market,
    MarketSnapshot,
    ProspectiveCaptureAlert,
    ProspectiveCaptureLease,
    ProspectiveCaptureRejection,
    ProspectiveHandoffCounter,
    ProspectiveHealthSnapshot,
    ProspectivePairedCapture,
    ProspectiveSnapshotHandoff,
    ProspectiveStatusLineage,
)
from kalshi_predictor.ingest.cycle_handoff import build_committed_cycle_metadata
from kalshi_predictor.phase4cd.prospective import (
    EXECUTABLE_MARKET_STATUSES,
    _hash,
    _json,
    capture_prospective_pairs,
)

LEASE_NAME = "phase4j-prospective-capture"


@dataclass(frozen=True)
class HealthReport:
    generated_at: str
    open_markets: int
    open_snapshots: int
    eligible_pairs: int
    exact_crypto_links: int
    two_sided_books: int
    one_sided_books: int
    max_market_seen_at: str | None
    max_snapshot_at: str | None
    max_feature_at: str | None
    findings: list[str]


def acquire_capture_lease(
    research: Session,
    *,
    owner_id: str,
    now: datetime | None = None,
    ttl_seconds: int = 120,
) -> bool:
    timestamp = now or datetime.now(UTC)
    research.execute(
        delete(ProspectiveCaptureLease).where(
            ProspectiveCaptureLease.lease_name == LEASE_NAME,
            ProspectiveCaptureLease.expires_at <= timestamp,
        )
    )
    existing = research.get(ProspectiveCaptureLease, LEASE_NAME)
    if existing is not None:
        if existing.owner_id != owner_id:
            research.rollback()
            return False
        existing.heartbeat_at = timestamp
        existing.expires_at = timestamp + timedelta(seconds=ttl_seconds)
        research.commit()
        return True
    research.add(
        ProspectiveCaptureLease(
            lease_name=LEASE_NAME,
            owner_id=owner_id,
            acquired_at=timestamp,
            heartbeat_at=timestamp,
            expires_at=timestamp + timedelta(seconds=ttl_seconds),
        )
    )
    try:
        research.commit()
    except IntegrityError:
        research.rollback()
        return False
    return True


def release_capture_lease(research: Session, *, owner_id: str) -> None:
    research.execute(
        delete(ProspectiveCaptureLease).where(
            ProspectiveCaptureLease.lease_name == LEASE_NAME,
            ProspectiveCaptureLease.owner_id == owner_id,
        )
    )
    research.commit()


def prospective_health_report(
    research: Session, source: Session, *, now: datetime | None = None
) -> HealthReport:
    """Diagnose capture inputs without reading settlements or forecast outcomes."""
    timestamp = now or datetime.now(UTC)
    open_markets = (
        source.scalar(
            select(func.count())
            .select_from(Market)
            .where(func.lower(Market.status).in_(EXECUTABLE_MARKET_STATUSES))
        )
        or 0
    )
    open_snapshots = (
        source.scalar(
            select(func.count())
            .select_from(MarketSnapshot)
            .where(func.lower(MarketSnapshot.status).in_(EXECUTABLE_MARKET_STATUSES))
        )
        or 0
    )
    eligible_filter = and_(
        func.lower(Market.status).in_(EXECUTABLE_MARKET_STATUSES),
        func.lower(MarketSnapshot.status).in_(EXECUTABLE_MARKET_STATUSES),
    )
    eligible_pairs = (
        source.scalar(
            select(func.count())
            .select_from(MarketSnapshot)
            .join(Market, Market.ticker == MarketSnapshot.ticker)
            .where(eligible_filter)
        )
        or 0
    )
    eligible_links = list(
        source.scalars(
            select(CryptoMarketLink)
            .select_from(CryptoMarketLink)
            .join(MarketSnapshot, MarketSnapshot.ticker == CryptoMarketLink.ticker)
            .join(Market, Market.ticker == MarketSnapshot.ticker)
            .where(eligible_filter)
            .order_by(CryptoMarketLink.ticker, CryptoMarketLink.detected_at.desc())
        )
    )
    exact_links = len(
        {
            link.ticker
            for link in eligible_links
            if (terms := terms_from_link_payload(link.symbol, link.raw_json)) is not None
            and terms.status == EXACT_LINK
        }
    )
    two_sided = (
        source.scalar(
            select(func.count())
            .select_from(MarketSnapshot)
            .join(Market, Market.ticker == MarketSnapshot.ticker)
            .where(
                eligible_filter,
                MarketSnapshot.best_yes_bid.is_not(None),
                MarketSnapshot.best_yes_ask.is_not(None),
            )
        )
        or 0
    )
    one_sided = (
        source.scalar(
            select(func.count())
            .select_from(MarketSnapshot)
            .join(Market, Market.ticker == MarketSnapshot.ticker)
            .where(
                eligible_filter,
                or_(
                    MarketSnapshot.best_yes_bid.is_not(None),
                    MarketSnapshot.best_yes_ask.is_not(None),
                ),
                or_(
                    MarketSnapshot.best_yes_bid.is_(None),
                    MarketSnapshot.best_yes_ask.is_(None),
                ),
            )
        )
        or 0
    )
    max_market = source.scalar(select(func.max(Market.last_seen_at)))
    max_snapshot = source.scalar(select(func.max(MarketSnapshot.captured_at)))
    max_feature = source.scalar(select(func.max(CryptoFeature.generated_at)))
    findings: list[str] = []
    if open_markets == 0:
        findings.append("NO_OPEN_MARKETS_IN_SOURCE")
    if open_snapshots == 0:
        findings.append("NO_OPEN_SNAPSHOTS_IN_SOURCE")
    if open_markets and not open_snapshots:
        findings.append("SNAPSHOT_COLLECTION_GAP")
    if max_market and timestamp - _aware(max_market) > timedelta(minutes=30):
        findings.append("MARKET_WRITER_STALE")
    if max_snapshot and timestamp - _aware(max_snapshot) > timedelta(minutes=30):
        findings.append("SNAPSHOT_WRITER_STALE")
    if max_feature and timestamp - _aware(max_feature) > timedelta(minutes=30):
        findings.append("FEATURE_WRITER_STALE")
    if eligible_pairs and exact_links == 0:
        findings.append("NO_EXACT_CRYPTO_LINKS_FOR_ELIGIBLE_SNAPSHOTS")
    report = HealthReport(
        generated_at=timestamp.isoformat(),
        open_markets=open_markets,
        open_snapshots=open_snapshots,
        eligible_pairs=eligible_pairs,
        exact_crypto_links=exact_links,
        two_sided_books=two_sided,
        one_sided_books=one_sided,
        max_market_seen_at=_iso(max_market),
        max_snapshot_at=_iso(max_snapshot),
        max_feature_at=_iso(max_feature),
        findings=findings,
    )
    payload = asdict(report)
    research.merge(
        ProspectiveHealthSnapshot(
            health_id=_hash(payload),
            generated_at=timestamp,
            source_max_market_seen_at=max_market,
            source_max_snapshot_at=max_snapshot,
            source_max_feature_at=max_feature,
            open_markets=open_markets,
            open_snapshots=open_snapshots,
            eligible_pairs=eligible_pairs,
            exact_crypto_links=exact_links,
            two_sided_books=two_sided,
            one_sided_books=one_sided,
            findings_json=_json(findings),
        )
    )
    research.commit()
    return report


def capture_status_lineage(
    research: Session,
    source: Session,
    *,
    now: datetime | None = None,
    sample_limit: int = 100,
) -> dict[str, Any]:
    """Freeze the latest catalog-cycle status boundary without exchange or source writes."""
    timestamp = now or datetime.now(UTC)
    watermark = source.scalar(select(func.max(MarketSnapshot.captured_at)))
    if watermark is None:
        payload: dict[str, Any] = {
            "watermark": None,
            "fetched_inventory": 0,
            "raw_active_or_open": 0,
            "normalized_active_or_open": 0,
            "filtered_inactive": 0,
            "missing_snapshot": 0,
            "snapshot_active_or_open": 0,
            "status_mismatch": 0,
            "strict_pair_eligible": 0,
            "loss_counts": {"EMPTY_CATALOG_CYCLE": 1},
            "samples": [],
        }
        _persist_status_lineage(research, payload, timestamp)
        return payload
    cycle_rows = list(
        source.execute(
            select(Market, MarketSnapshot)
            .join(MarketSnapshot, MarketSnapshot.ticker == Market.ticker)
            .where(MarketSnapshot.captured_at == watermark)
            .order_by(Market.ticker)
        )
    )
    samples: list[dict[str, Any]] = []
    losses: dict[str, int] = {}
    raw_active = normalized_active = snapshot_active = eligible = mismatch = missing = 0
    for market, snapshot in cycle_rows:
        raw = decode_json(market.raw_json)
        raw_status = str(raw.get("status") or "").strip().lower()
        normalized_status = str(market.status or "").strip().lower()
        snapshot_status = str(snapshot.status or "").strip().lower() if snapshot else None
        raw_ok = raw_status in EXECUTABLE_MARKET_STATUSES
        normalized_ok = normalized_status in EXECUTABLE_MARKET_STATUSES
        snapshot_ok = snapshot_status in EXECUTABLE_MARKET_STATUSES
        raw_active += int(raw_ok)
        normalized_active += int(normalized_ok)
        snapshot_active += int(snapshot_ok)
        missing += int(snapshot is None)
        mismatch += int(snapshot is not None and snapshot_status != normalized_status)
        eligible += int(normalized_ok and snapshot_ok)
        if not normalized_ok:
            reason = "NORMALIZED_INACTIVE"
        elif snapshot is None:
            reason = "MISSING_SNAPSHOT_IN_CYCLE"
        elif not snapshot_ok:
            reason = "SNAPSHOT_INACTIVE"
        elif raw_status != normalized_status:
            reason = "RAW_NORMALIZED_MISMATCH"
        else:
            reason = "STRICT_STATUS_ELIGIBLE"
        losses[reason] = losses.get(reason, 0) + 1
        if len(samples) < sample_limit:
            samples.append(
                {
                    "ticker": market.ticker,
                    "catalog_watermark": _iso(watermark),
                    "raw_status": raw_status,
                    "raw_market_hash": _hash(raw),
                    "normalized_status": normalized_status,
                    "snapshot_id": snapshot.id if snapshot else None,
                    "snapshot_timestamp": _iso(snapshot.captured_at) if snapshot else None,
                    "snapshot_status": snapshot_status,
                    "reason": reason,
                }
            )
    payload = {
        "watermark": _iso(watermark),
        "fetched_inventory": len(cycle_rows),
        "raw_active_or_open": raw_active,
        "normalized_active_or_open": normalized_active,
        "filtered_inactive": len(cycle_rows) - normalized_active,
        "missing_snapshot": missing,
        "snapshot_active_or_open": snapshot_active,
        "status_mismatch": mismatch,
        "strict_pair_eligible": eligible,
        "loss_counts": losses,
        "samples": samples,
    }
    _persist_status_lineage(research, payload, timestamp)
    return payload


def _persist_status_lineage(
    research: Session, payload: dict[str, Any], timestamp: datetime
) -> None:
    bundle_hash = _hash(payload)
    research.merge(
        ProspectiveStatusLineage(
            lineage_id=_hash([timestamp, bundle_hash]),
            generated_at=timestamp,
            fetched_inventory=int(payload["fetched_inventory"]),
            raw_active_or_open=int(payload["raw_active_or_open"]),
            normalized_active_or_open=int(payload["normalized_active_or_open"]),
            filtered_inactive=int(payload["filtered_inactive"]),
            missing_snapshot=int(payload["missing_snapshot"]),
            snapshot_active_or_open=int(payload["snapshot_active_or_open"]),
            status_mismatch=int(payload["status_mismatch"]),
            strict_pair_eligible=int(payload["strict_pair_eligible"]),
            loss_counts_json=_json(payload["loss_counts"]),
            samples_json=_json(payload["samples"]),
            sample_bundle_hash=bundle_hash,
        )
    )
    for reason, count in dict(payload["loss_counts"]).items():
        if reason == "STRICT_STATUS_ELIGIBLE" or int(count) <= 0:
            continue
        alert_payload = {"reason": reason, "count": int(count), "bundle_hash": bundle_hash}
        research.merge(
            ProspectiveCaptureAlert(
                alert_id=_hash(["STATUS_LINEAGE", alert_payload]),
                run_id=None,
                capture_id=None,
                alert_type=f"STATUS_LINEAGE_{reason}",
                severity="WARNING",
                observed_value=str(count),
                threshold_value="0",
                details_json=_json(alert_payload),
                created_at=timestamp,
            )
        )
    research.commit()


def persist_capture_alerts(
    research: Session,
    *,
    run_id: str,
    latency_threshold_ms: int = 30_000,
    now: datetime | None = None,
) -> int:
    timestamp = now or datetime.now(UTC)
    alerts: list[dict[str, Any]] = []
    captures = list(
        research.scalars(
            select(ProspectivePairedCapture).where(ProspectivePairedCapture.run_id == run_id)
        )
    )
    for capture in captures:
        for metric, raw_value in json.loads(capture.latency_json).items():
            value = int(raw_value)
            if value > latency_threshold_ms:
                alerts.append(
                    {
                        "capture_id": capture.capture_id,
                        "type": f"LATENCY_{metric.upper()}",
                        "value": value,
                        "threshold": latency_threshold_ms,
                    }
                )
    for rejection in research.scalars(
        select(ProspectiveCaptureRejection).where(ProspectiveCaptureRejection.run_id == run_id)
    ):
        if rejection.reason in {
            "FEATURE_SOURCE_AFTER_CUTOFF",
            "SOURCE_AFTER_FEATURE",
            "PAIR_TIMESTAMP_MISMATCH",
        }:
            alerts.append(
                {
                    "capture_id": None,
                    "rejection_id": rejection.rejection_id,
                    "type": "NON_CAUSAL_ORDERING_REJECTED",
                    "value": rejection.reason,
                    "threshold": "zero_skew",
                }
            )
    for item in alerts:
        alert_id = _hash([run_id, item])
        research.merge(
            ProspectiveCaptureAlert(
                alert_id=alert_id,
                run_id=run_id,
                capture_id=item["capture_id"],
                alert_type=item["type"],
                severity="CRITICAL"
                if item["type"] == "NON_CAUSAL_ORDERING_REJECTED"
                else "WARNING",
                observed_value=str(item["value"]),
                threshold_value=str(item["threshold"]),
                details_json=_json(item),
                created_at=timestamp,
            )
        )
    research.commit()
    return len(alerts)


def run_capture_scheduler(
    research: Session,
    source: Session,
    *,
    owner_id: str,
    cycles: int = 1,
    batch_limit: int = 250,
    interval_seconds: float = 0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if not acquire_capture_lease(research, owner_id=owner_id):
        return {"status": "LEASE_HELD", "cycles": 0, "captured": 0}
    captured_before = (
        research.scalar(select(func.count()).select_from(ProspectivePairedCapture)) or 0
    )
    completed = 0
    try:
        for index in range(cycles):
            result = capture_prospective_pairs(research, source, limit=batch_limit, resume=True)
            persist_capture_alerts(research, run_id=result.run_id)
            completed += 1
            acquire_capture_lease(research, owner_id=owner_id)
            if interval_seconds and index + 1 < cycles:
                sleep(interval_seconds)
    finally:
        release_capture_lease(research, owner_id=owner_id)
    captured_after = (
        research.scalar(select(func.count()).select_from(ProspectivePairedCapture)) or 0
    )
    return {
        "status": "COMPLETE",
        "cycles": completed,
        "captured": captured_after - captured_before,
        "bounded_batch_limit": batch_limit,
        "gh2_imported": False,
    }


def receive_latest_snapshot_handoff(
    research: Session,
    source: Session,
    *,
    now: datetime | None = None,
    freshness_seconds: int = 300,
    cycle_window_seconds: int = 5,
    expected_cycle_hash: str | None = None,
    artifact_generated_at: datetime | None = None,
    expected_cycle_watermark: datetime | None = None,
) -> dict[str, Any]:
    """Detect a committed source cycle and persist metadata only in the research lane."""
    timestamp = now or datetime.now(UTC)
    metadata = build_committed_cycle_metadata(
        source,
        cycle_window_seconds=cycle_window_seconds,
        cycle_watermark=expected_cycle_watermark,
    )
    if metadata is None:
        return {"status": "NO_COMMITTED_SNAPSHOT", "handoff_id": None}
    if expected_cycle_hash is not None and metadata["cycle_hash"] != expected_cycle_hash:
        return {"status": "ARTIFACT_SOURCE_MISMATCH", "handoff_id": None}
    cycle_end = _aware(datetime.fromisoformat(metadata["cycle_watermark"]))
    rows = list(
        source.scalars(
            select(MarketSnapshot)
            .where(
                MarketSnapshot.captured_at >= cycle_end - timedelta(seconds=cycle_window_seconds),
                MarketSnapshot.captured_at <= cycle_end,
            )
            .order_by(MarketSnapshot.captured_at, MarketSnapshot.ticker, MarketSnapshot.id)
        )
    )
    cycle_hash = str(metadata["cycle_hash"])
    handoff_id = _hash([cycle_end, cycle_hash])
    existing = research.get(ProspectiveSnapshotHandoff, handoff_id)
    if existing is not None:
        if existing.status == "ACCEPTED":
            _record_handoff_counter(research, handoff_id, "RETRY", timestamp, {})
            research.commit()
            return {
                "status": "RECEIVED",
                "handoff_id": handoff_id,
                "cycle_hash": cycle_hash,
                "cycle_watermark": cycle_end.isoformat(),
                "snapshots": existing.snapshot_count,
                "active_snapshots": existing.active_snapshot_count,
                "latency_ms": existing.latency_ms,
            }
        _record_handoff_counter(research, handoff_id, "DUPLICATE", timestamp, {})
        research.commit()
        return {"status": "DUPLICATE", "handoff_id": handoff_id, "cycle_hash": cycle_hash}
    active = sum(str(row.status or "").lower() in EXECUTABLE_MARKET_STATUSES for row in rows)
    latency_ms = max(int((timestamp - cycle_end).total_seconds() * 1000), 0)
    artifact_timestamp = _aware(artifact_generated_at) if artifact_generated_at else None
    commit_to_artifact_ms = (
        max(int((artifact_timestamp - cycle_end).total_seconds() * 1000), 0)
        if artifact_timestamp
        else None
    )
    artifact_to_acceptance_ms = (
        max(int((timestamp - artifact_timestamp).total_seconds() * 1000), 0)
        if artifact_timestamp
        else None
    )
    status = "EXPIRED" if latency_ms > freshness_seconds * 1000 else "RECEIVED"
    research.add(
        ProspectiveSnapshotHandoff(
            handoff_id=handoff_id,
            cycle_watermark=cycle_end,
            cycle_hash=cycle_hash,
            status=status,
            received_at=timestamp,
            snapshot_count=len(rows),
            active_snapshot_count=active,
            latency_ms=latency_ms,
            details_json=_json(
                {
                    "cycle_window_seconds": cycle_window_seconds,
                    "freshness_seconds": freshness_seconds,
                    "manifest_count": metadata["snapshot_count"],
                    "commit_to_artifact_ms": commit_to_artifact_ms,
                    "artifact_to_acceptance_ms": artifact_to_acceptance_ms,
                }
            ),
        )
    )
    if latency_ms >= 240_000:
        alert_type = "HANDOFF_LATENCY_EXPIRED" if status == "EXPIRED" else "HANDOFF_LATENCY_WARNING"
        research.merge(
            ProspectiveCaptureAlert(
                alert_id=_hash([handoff_id, alert_type]),
                run_id=None,
                capture_id=None,
                alert_type=alert_type,
                severity="CRITICAL" if status == "EXPIRED" else "WARNING",
                observed_value=str(latency_ms),
                threshold_value="240000",
                details_json=_json({"handoff_id": handoff_id, "latency_ms": latency_ms}),
                created_at=timestamp,
            )
        )
    _record_handoff_counter(research, handoff_id, status, timestamp, {"latency_ms": latency_ms})
    research.commit()
    return {
        "status": status,
        "handoff_id": handoff_id,
        "cycle_hash": cycle_hash,
        "cycle_watermark": cycle_end.isoformat(),
        "snapshots": len(rows),
        "active_snapshots": active,
        "latency_ms": latency_ms,
        "commit_to_artifact_ms": commit_to_artifact_ms,
        "artifact_to_acceptance_ms": artifact_to_acceptance_ms,
    }


def receive_snapshot_handoff_artifact(
    research: Session,
    source: Session,
    *,
    artifact_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "ARTIFACT_INVALID", "handoff_id": None}
    if payload.get("schema") != "kalshi.snapshot-cycle-handoff.v1":
        return {"status": "ARTIFACT_INVALID", "handoff_id": None}
    try:
        artifact_generated_at = datetime.fromisoformat(str(payload["generated_at"]))
        cycle_watermark = datetime.fromisoformat(str(payload["cycle_watermark"]))
    except (KeyError, TypeError, ValueError):
        return {"status": "ARTIFACT_INVALID", "handoff_id": None}
    return receive_latest_snapshot_handoff(
        research,
        source,
        now=now,
        cycle_window_seconds=int(payload.get("cycle_window_seconds") or 5),
        expected_cycle_hash=str(payload.get("cycle_hash") or ""),
        artifact_generated_at=artifact_generated_at,
        expected_cycle_watermark=cycle_watermark,
    )


def run_latest_handoff(
    research: Session,
    source: Session,
    *,
    owner_id: str,
    batch_limit: int = 250,
    now: datetime | None = None,
    artifact_path: Path | None = None,
) -> dict[str, Any]:
    timestamp = now or datetime.now(UTC)
    received = (
        receive_snapshot_handoff_artifact(
            research, source, artifact_path=artifact_path, now=timestamp
        )
        if artifact_path is not None
        else receive_latest_snapshot_handoff(research, source, now=timestamp)
    )
    if received["status"] != "RECEIVED":
        return received | {"captured": 0}
    handoff = research.get(ProspectiveSnapshotHandoff, received["handoff_id"])
    assert handoff is not None
    if not acquire_capture_lease(research, owner_id=owner_id, now=timestamp):
        return received | {"status": "LEASE_HELD", "captured": 0}
    try:
        handoff.status = "ACCEPTED"
        handoff.accepted_at = timestamp
        _record_handoff_counter(research, handoff.handoff_id, "ACCEPTED", timestamp, {})
        research.commit()
        while True:
            result = capture_prospective_pairs(
                research,
                source,
                limit=batch_limit,
                resume=True,
                now=timestamp,
                max_snapshot_age_seconds=300,
                cycle_watermark=_aware(handoff.cycle_watermark),
            )
            if result.deferred == 0:
                break
            _record_handoff_counter(
                research,
                handoff.handoff_id,
                "DEFERRED",
                datetime.now(UTC),
                {"count": result.deferred},
            )
            research.commit()
        persist_capture_alerts(research, run_id=result.run_id, now=timestamp)
        handoff.status = "COMPLETED"
        completed_at = datetime.now(UTC)
        handoff.completed_at = completed_at
        details = json.loads(handoff.details_json)
        details["acceptance_to_persistence_ms"] = max(
            int((completed_at - _aware(handoff.accepted_at or timestamp)).total_seconds() * 1000),
            0,
        )
        handoff.details_json = _json(details)
        _record_handoff_counter(
            research,
            handoff.handoff_id,
            "COMPLETED",
            completed_at,
            {"captured": result.captured, "rejected": result.rejected},
        )
        research.commit()
        return received | {
            "status": "COMPLETED",
            "captured": result.captured,
            "rejected": result.rejected,
            "run_id": result.run_id,
        }
    except Exception:
        research.rollback()
        raise
    finally:
        release_capture_lease(research, owner_id=owner_id)


def handoff_funnel(research: Session) -> dict[str, Any]:
    rows = list(research.scalars(select(ProspectiveHandoffCounter)))
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.disposition] = counts.get(row.disposition, 0) + 1
    handoffs = list(research.scalars(select(ProspectiveSnapshotHandoff)))
    return {
        "counts": counts,
        "handoffs": len(handoffs),
        "mean_latency_ms": (
            sum(row.latency_ms for row in handoffs) / len(handoffs) if handoffs else None
        ),
        "fresh_active_snapshots": sum(
            row.active_snapshot_count for row in handoffs if row.status != "EXPIRED"
        ),
    }


def _record_handoff_counter(
    research: Session,
    handoff_id: str,
    disposition: str,
    timestamp: datetime,
    details: dict[str, Any],
) -> None:
    sequence = (
        research.scalar(
            select(func.count())
            .select_from(ProspectiveHandoffCounter)
            .where(ProspectiveHandoffCounter.handoff_id == handoff_id)
        )
        or 0
    )
    research.add(
        ProspectiveHandoffCounter(
            counter_id=_hash([handoff_id, disposition, sequence]),
            handoff_id=handoff_id,
            disposition=disposition,
            recorded_at=timestamp,
            details_json=_json(details),
        )
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return _aware(value).isoformat() if value else None
