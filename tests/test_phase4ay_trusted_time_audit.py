from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ay_trusted_time_audit.py"
    spec = importlib.util.spec_from_file_location("phase4ay_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _fixture(tmp_path: Path, *, skew: int = 0, regression: int = 0):
    module = _module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    entries = []
    for sequence, phase in enumerate(module.REQUIRED_PHASES, start=1):
        at = NOW - timedelta(minutes=len(module.REQUIRED_PHASES) - sequence + 1)
        payload = {
            "schema": f"fixture.{phase.lower()}.v1",
            "evaluated_at": at.isoformat(),
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        }
        payload["artifact_hash"] = module._hash(payload)
        entries.append(
            {
                "sequence": sequence,
                "phase": phase,
                "payload": payload,
                "hash_field": "artifact_hash",
                "timestamp_fields": [
                    {"path": "evaluated_at", "role": "EVENT", "primary": True},
                    {"path": "expires_at", "role": "DEADLINE"},
                ],
            }
        )
    catalog = {
        "schema": module.CATALOG_SCHEMA,
        "trusted_now": NOW.isoformat(),
        "max_future_skew_seconds": skew,
        "max_clock_regression_seconds": regression,
        "expiration_semantics": module.EXPIRATION_SEMANTICS,
        "entries": entries,
    }
    catalog["artifact_hash"] = module._hash(catalog)
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog))
    return module, path


def _mutate(module, path: Path, mutate, *, rehash_entry: int | None = None, rehash=True):
    payload = json.loads(path.read_text())
    mutate(payload)
    if rehash_entry is not None:
        item = payload["entries"][rehash_entry]["payload"]
        item["artifact_hash"] = module._hash(item)
    if rehash:
        payload["artifact_hash"] = module._hash(payload)
    path.write_text(json.dumps(payload))


def test_complete_phase_span_normalizes_and_passes(tmp_path: Path):
    module, path = _fixture(tmp_path)
    audit, proof = module.build(path, now=NOW)
    assert audit["phase_coverage"] == list(module.REQUIRED_PHASES)
    assert audit["time_semantics_valid"] is True
    assert audit["reason_codes"] == []
    assert proof["expiration_equality_is_expired"] is True
    assert audit["artifact_hash"] == module._hash(audit)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-08-25T07:00:00-05:00",
        "2024-02-29T12:00:00+00:00",
        "2026-11-01T01:30:00-05:00",
        "2026-11-01T01:30:00-06:00",
    ],
)
def test_offset_dst_and_leap_day_timestamps_are_canonicalized(tmp_path: Path, timestamp: str):
    module, path = _fixture(tmp_path, skew=300)
    _mutate(
        module,
        path,
        lambda p: p["entries"][0]["payload"].update(evaluated_at=timestamp),
        rehash_entry=0,
    )
    audit, _ = module.build(path, now=NOW)
    assert audit["rows"][0]["primary_timestamp_utc"].endswith("+00:00")


def test_future_dating_honors_exact_skew_boundary(tmp_path: Path):
    module, path = _fixture(tmp_path / "equal", skew=30)
    _mutate(
        module,
        path,
        lambda p: p["entries"][-1]["payload"].update(
            evaluated_at=(NOW + timedelta(seconds=30)).isoformat()
        ),
        rehash_entry=-1,
    )
    audit, _ = module.build(path, now=NOW)
    assert "FUTURE_DATED_ARTIFACT" not in audit["reason_codes"]
    module, path = _fixture(tmp_path / "over", skew=30)
    _mutate(
        module,
        path,
        lambda p: p["entries"][-1]["payload"].update(
            evaluated_at=(NOW + timedelta(seconds=30, microseconds=1)).isoformat()
        ),
        rehash_entry=-1,
    )
    audit, _ = module.build(path, now=NOW)
    assert "FUTURE_DATED_ARTIFACT" in audit["reason_codes"]


def test_clock_regression_honors_exact_allowance(tmp_path: Path):
    module, path = _fixture(tmp_path / "equal", regression=10)
    catalog = json.loads(path.read_text())
    previous = datetime.fromisoformat(catalog["entries"][0]["payload"]["evaluated_at"])
    _mutate(
        module,
        path,
        lambda p: p["entries"][1]["payload"].update(
            evaluated_at=(previous - timedelta(seconds=10)).isoformat()
        ),
        rehash_entry=1,
    )
    audit, _ = module.build(path, now=NOW)
    assert "CLOCK_REGRESSION" not in audit["reason_codes"]
    module, path = _fixture(tmp_path / "over", regression=10)
    catalog = json.loads(path.read_text())
    previous = datetime.fromisoformat(catalog["entries"][0]["payload"]["evaluated_at"])
    _mutate(
        module,
        path,
        lambda p: p["entries"][1]["payload"].update(
            evaluated_at=(previous - timedelta(seconds=10, microseconds=1)).isoformat()
        ),
        rehash_entry=1,
    )
    audit, _ = module.build(path, now=NOW)
    assert "CLOCK_REGRESSION" in audit["reason_codes"]


def test_naive_timestamp_tampering_and_missing_phase_fail_closed(tmp_path: Path):
    module, path = _fixture(tmp_path / "naive")
    _mutate(
        module,
        path,
        lambda p: p["entries"][0]["payload"].update(evaluated_at="2026-08-25T12:00:00"),
        rehash_entry=0,
    )
    with pytest.raises(ValueError, match="TIMEZONE_MISSING"):
        module.build(path, now=NOW)
    module, path = _fixture(tmp_path / "tamper")
    _mutate(module, path, lambda p: p["entries"][0]["payload"].update(extra=True))
    with pytest.raises(ValueError, match="ENTRY_HASH_INVALID"):
        module.build(path, now=NOW)
    module, path = _fixture(tmp_path / "missing")
    _mutate(module, path, lambda p: p["entries"].pop())
    with pytest.raises(ValueError, match="PHASE_COVERAGE_OR_ORDER_INVALID"):
        module.build(path, now=NOW)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("max_future_skew_seconds", 301, "FUTURE_SKEW_INVALID"),
        ("max_clock_regression_seconds", -1, "REGRESSION_ALLOWANCE_INVALID"),
        ("expiration_semantics", "inclusive", "EXPIRATION_SEMANTICS_INVALID"),
        ("trusted_now", "2026-08-25T12:00:01+00:00", "TRUSTED_NOW_MISMATCH"),
    ],
)
def test_policy_and_trusted_time_mismatches_fail_closed(
    tmp_path: Path, field: str, value, reason: str
):
    module, path = _fixture(tmp_path)
    _mutate(module, path, lambda p: p.update({field: value}))
    with pytest.raises(ValueError, match=reason):
        module.build(path, now=NOW)


def test_static_auditor_is_artifact_only():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4ay_trusted_time_audit.py"
    ).read_text()
    assert "sqlite3" not in source
    assert "--production-db" not in source
    assert "systemctl" not in source
