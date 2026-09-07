from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bu_timestamp_precision.py"
    spec = importlib.util.spec_from_file_location("phase4bu_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


START = datetime(2026, 8, 26, 5, 0, tzinfo=UTC)


def _payload(module):
    rows, wall_cursor, monotonic_cursor = [], START, 1_000_000_000
    for index, stage in enumerate(module.STAGES, start=1):
        duration_ns = index * 1_000_000
        ended = wall_cursor + timedelta(microseconds=duration_ns // 1000)
        rows.append({
            "stage": stage,
            "wall_started_at": wall_cursor.isoformat(timespec="microseconds"),
            "wall_ended_at": ended.isoformat(timespec="microseconds"),
            "monotonic_started_ns": monotonic_cursor,
            "monotonic_ended_ns": monotonic_cursor + duration_ns,
            "declared_fraction_digits": 6,
            "evidence_hash": module.canonical_hash([stage, duration_ns]),
        })
        wall_cursor, monotonic_cursor = ended, monotonic_cursor + duration_ns
    payload = {"schema": module.INPUT_SCHEMA, "samples": rows}
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_harmonization_is_deterministic_complete_and_non_authorizing():
    module = _module()
    payload = _payload(module)
    assert module.build(payload) == module.build(payload)
    harmonized, proof = module.build(payload)
    assert all(row["wall_started_at_utc"].endswith("Z") for row in harmonized["samples"])
    assert all(row["fraction_digits"] == 6 for row in harmonized["samples"])
    assert proof["wall_monotonic_agreement"] is True
    assert harmonized["production_records_created"] == 0


def test_non_utc_offset_is_canonicalized_without_duration_change():
    module = _module()
    payload = _payload(module)
    row = payload["samples"][0]
    row["wall_started_at"] = "2026-08-26T00:00:00.000000-05:00"
    row["wall_ended_at"] = "2026-08-26T00:00:00.001000-05:00"
    _rehash(module, payload)
    harmonized, _ = module.build(payload)
    assert harmonized["samples"][0]["wall_started_at_utc"] == "2026-08-26T05:00:00.000000Z"


@pytest.mark.parametrize("kind", ("missing", "reordered", "duplicate"))
def test_stage_coverage_and_order_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "missing":
        payload["samples"].pop()
    elif kind == "reordered":
        payload["samples"].reverse()
    else:
        payload["samples"][-1] = dict(payload["samples"][0])
    _rehash(module, payload)
    with pytest.raises(ValueError, match="COVERAGE_OR_ORDER"):
        module.build(payload)


@pytest.mark.parametrize(
    ("value", "declared"),
    (
        ("2026-08-26T05:00:00.000Z", 6),
        ("2026-08-26T05:00:00.000000Z", 3),
        ("2026-08-26T05:00:00Z", 6),
    ),
)
def test_truncation_and_precision_mismatch_fail_closed(value: str, declared: int):
    module = _module()
    payload = _payload(module)
    payload["samples"][0]["wall_started_at"] = value
    payload["samples"][0]["declared_fraction_digits"] = declared
    _rehash(module, payload)
    with pytest.raises(ValueError, match="PRECISION"):
        module.build(payload)


def test_timezone_format_clock_and_sequence_regression_fail_closed():
    module = _module()
    cases = (
        lambda p: p["samples"][0].update(wall_started_at="2026-08-26T05:00:00.000000"),
        lambda p: p["samples"][0].update(wall_started_at="bad"),
        lambda p: p["samples"][0].update(
            wall_ended_at=(START - timedelta(seconds=1)).isoformat(timespec="microseconds")
        ),
        lambda p: p["samples"][0].update(monotonic_ended_ns=0),
        lambda p: p["samples"][1].update(monotonic_started_ns=0),
    )
    for mutation in cases:
        payload = _payload(module)
        mutation(payload)
        _rehash(module, payload)
        with pytest.raises(ValueError):
            module.build(payload)


def test_duration_mismatch_bound_and_monotonic_types_fail_closed():
    module = _module()
    cases = (
        lambda p: p["samples"][0].update(
            monotonic_ended_ns=p["samples"][0]["monotonic_ended_ns"] + 1
        ),
        lambda p: p["samples"][0].update(monotonic_ended_ns=module.MAX_DURATION_NS + 1_000_000_000),
        lambda p: p["samples"][0].update(monotonic_started_ns=True),
        lambda p: p["samples"][0].update(monotonic_started_ns=-1),
    )
    for mutation in cases:
        payload = _payload(module)
        mutation(payload)
        _rehash(module, payload)
        with pytest.raises(ValueError):
            module.build(payload)


def test_tampering_fields_and_evidence_fail_closed():
    module = _module()
    payload = _payload(module)
    payload["extra"] = True
    with pytest.raises(ValueError, match="HASH_INVALID"):
        module.build(payload)
    payload = _payload(module)
    payload["samples"][0]["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError, match="FIELDS"):
        module.build(payload)
    payload = _payload(module)
    payload["samples"][0]["evidence_hash"] = "bad"
    _rehash(module, payload)
    with pytest.raises(ValueError, match="EVIDENCE_HASH"):
        module.build(payload)


def test_source_is_artifact_only():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4bu_timestamp_precision.py"
    ).read_text()
    for token in (
        "sqlite3",
        "subprocess",
        "requests",
        "systemctl",
        "exchange_client",
        "/home/james",
    ):
        assert token not in source
