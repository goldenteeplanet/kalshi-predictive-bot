from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.read_model_consumer import (
    ReadModelConsumerContract,
    ReadModelContractError,
    consume_bytes,
    consume_path,
    consume_payload,
)

NOW = datetime(2026, 8, 27, tzinfo=UTC)
CONTRACT = ReadModelConsumerContract(max_age_seconds=30, max_artifact_bytes=4096)


def test_valid_contract_returns_immutable_view() -> None:
    view = consume_payload(_payload(NOW), contract=CONTRACT, now=NOW + timedelta(seconds=29))
    assert view.source_watermark == "paper_pnl:140680"
    assert view.age_seconds == 29
    assert view.evidence_lane == {"evaluated": 1}


def test_empty_and_malformed_input_fail_closed() -> None:
    with pytest.raises(ReadModelContractError, match="ARTIFACT_EMPTY"):
        consume_bytes(b"", contract=CONTRACT, now=NOW)
    with pytest.raises(ReadModelContractError, match="ARTIFACT_MALFORMED"):
        consume_bytes(b"{", contract=CONTRACT, now=NOW)


def test_exact_freshness_boundary_is_stale() -> None:
    with pytest.raises(ReadModelContractError, match="ARTIFACT_STALE"):
        consume_payload(_payload(NOW), contract=CONTRACT, now=NOW + timedelta(seconds=30))


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (
            lambda p: p.update(schema_version="phase4fm-evidence-read-model-v2"),
            "SCHEMA_INCOMPATIBLE",
        ),
        (lambda p: p.update(source_watermark="forecasts:9"), "WATERMARK_INCOMPATIBLE"),
        (lambda p: p["evidence_lanes"].pop("GUARDED_PAPER"), "REQUIRED_LANE_MISSING"),
        (lambda p: p.pop("realized_pnl"), "ARTIFACT_FIELDS_INVALID"),
    ],
)
def test_partial_or_incompatible_input_fails_closed(change, reason: str) -> None:
    payload = _payload(NOW)
    change(payload)
    with pytest.raises(ReadModelContractError, match=reason):
        consume_payload(payload, contract=CONTRACT, now=NOW)


def test_payload_and_manifest_tampering_fail_closed() -> None:
    payload = _payload(NOW)
    payload["paper_order_count"] = 205
    with pytest.raises(ReadModelContractError, match="PAYLOAD_HASH_MISMATCH"):
        consume_payload(payload, contract=CONTRACT, now=NOW)

    payload = _payload(NOW)
    payload["manifest_hash"] = "0" * 64
    with pytest.raises(ReadModelContractError, match="MANIFEST_HASH_MISMATCH"):
        consume_payload(payload, contract=CONTRACT, now=NOW)


def test_bounded_file_read_rejects_oversize_before_read(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "large.json"
    path.write_bytes(b"x" * 4097)
    read_called = False

    def forbidden_read(_self: Path) -> bytes:
        nonlocal read_called
        read_called = True
        raise AssertionError("oversize artifact must not be read")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read)
    with pytest.raises(ReadModelContractError, match="ARTIFACT_SIZE_EXCEEDED"):
        consume_path(path, contract=CONTRACT, now=NOW)
    assert read_called is False


def test_consumer_never_invokes_production_mutation_methods(tmp_path: Path) -> None:
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(_payload(NOW)), encoding="utf-8")

    class MutationSurface:
        def commit(self) -> None:
            raise AssertionError("consumer called commit")

        def execute(self, *_args) -> None:
            raise AssertionError("consumer called execute")

    surface = MutationSurface()
    view = consume_path(path, contract=CONTRACT, now=NOW)
    assert view.paper_order_count == 204
    assert surface is not None


def _payload(generated_at: datetime) -> dict:
    payload = {
        "schema_version": "phase4fm-evidence-read-model-v1",
        "generated_at": generated_at.isoformat(),
        "source_database_identity_hash": "a" * 64,
        "source_watermark": "paper_pnl:140680",
        "guarded_paper_settled": 203,
        "realized_pnl": "0",
        "paper_order_count": 204,
        "evidence_lanes": {"GUARDED_PAPER": {"evaluated": 1}},
        "previous_artifact_hash": None,
    }
    payload["payload_hash"] = _hash(payload)
    payload["manifest_hash"] = _hash(
        {
            "schema_version": payload["schema_version"],
            "payload_hash": payload["payload_hash"],
            "previous_artifact_hash": None,
            "source_watermark": payload["source_watermark"],
        }
    )
    return payload


def _hash(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
