import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.evidence_read_model import (
    EvidenceReadModelError,
    build_read_model,
    load_read_model,
    load_ui_read_model,
    publish_atomic,
)


def test_valid_artifact_round_trip(tmp_path: Path) -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    payload = _payload(now)
    path = tmp_path / "summary.json"
    publish_atomic(path, payload)
    assert load_read_model(path, now=now + timedelta(seconds=29), max_age_seconds=30) == payload


def test_exact_staleness_boundary_fails_closed(tmp_path: Path) -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    path = tmp_path / "summary.json"
    publish_atomic(path, _payload(now))
    with pytest.raises(EvidenceReadModelError, match="READ_MODEL_STALE"):
        load_read_model(path, now=now + timedelta(seconds=30), max_age_seconds=30)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda row: row.update(realized_pnl="999"), "PAYLOAD_HASH_MISMATCH"),
        (lambda row: row.update(manifest_hash="0" * 64), "MANIFEST_HASH_MISMATCH"),
        (lambda row: row.pop("source_watermark"), "REQUIRED_FIELD_MISSING"),
        (lambda row: row.update(schema_version="future"), "SCHEMA_UNSUPPORTED"),
    ],
)
def test_tampering_and_schema_fail_closed(tmp_path: Path, mutation, reason: str) -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    payload = copy.deepcopy(_payload(now))
    mutation(payload)
    path = tmp_path / "summary.json"
    path.write_text(__import__("json").dumps(payload))
    with pytest.raises(EvidenceReadModelError, match=reason):
        load_read_model(path, now=now, max_age_seconds=30)


def test_future_dated_artifact_fails_closed(tmp_path: Path) -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    path = tmp_path / "summary.json"
    publish_atomic(path, _payload(now + timedelta(seconds=1)))
    with pytest.raises(EvidenceReadModelError, match="FUTURE_DATED"):
        load_read_model(path, now=now, max_age_seconds=30)


def test_ui_adapter_is_disabled_by_default() -> None:
    assert (
        load_ui_read_model(
            None,
            now=datetime(2026, 8, 27, tzinfo=UTC),
            max_age_seconds=30,
            allow_database_fallback=False,
        )
        is None
    )


def test_ui_adapter_reports_explicit_database_fallback(tmp_path: Path) -> None:
    result = load_ui_read_model(
        tmp_path / "missing.json",
        now=datetime(2026, 8, 27, tzinfo=UTC),
        max_age_seconds=30,
        allow_database_fallback=True,
    )
    assert result["fallback_used"] is True
    assert result["read_model_source"] == "DATABASE_FALLBACK_REQUIRED"


def _payload(now: datetime) -> dict:
    return build_read_model(
        generated_at=now,
        source_database_identity_hash="a" * 64,
        source_watermark="paper_pnl:140680",
        guarded_paper_settled=203,
        realized_pnl="0",
        paper_order_count=204,
        evidence_lanes={"GUARDED_PAPER": {"evaluated": 1}},
    )
