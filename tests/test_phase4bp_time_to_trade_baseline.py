from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bp_time_to_trade_baseline.py"
    spec = importlib.util.spec_from_file_location("phase4bp_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 26, 5, 0, tzinfo=UTC)


def _payload(module, traces: int = 3):
    observations = []
    for trace_number in range(traces):
        cursor = NOW - timedelta(minutes=10 - trace_number)
        for stage_number, stage in enumerate(module.STAGES, start=1):
            ended = cursor + timedelta(milliseconds=stage_number * 100)
            observations.append(
                {
                    "trace_id": f"trace-{trace_number}",
                    "stage": stage,
                    "started_at": cursor.isoformat(),
                    "ended_at": ended.isoformat(),
                    "evidence_hash": module.canonical_hash([trace_number, stage]),
                }
            )
            cursor = ended
    payload = {"schema": module.INPUT_SCHEMA, "observations": observations}
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_baseline_is_deterministic_complete_and_non_authorizing():
    module = _module()
    payload = _payload(module)
    assert module.build(payload, now=NOW) == module.build(payload, now=NOW)
    report, bottleneck = module.build(payload, now=NOW)
    assert report["trace_count"] == 3
    assert [row["stage"] for row in report["stage_summaries"]] == list(module.STAGES)
    assert bottleneck["primary_bottleneck_stage"] == module.STAGES[-1]
    assert report["production_records_created"] == 0
    assert bottleneck["execution_authorized"] is False


def test_nearest_rank_percentile_boundaries():
    module = _module()
    assert module._percentile([1], 95) == 1
    assert module._percentile(list(range(1, 101)), 50) == 50
    assert module._percentile(list(range(1, 101)), 95) == 95
    assert module._percentile(list(range(1, 101)), 99) == 99


@pytest.mark.parametrize("kind", ("missing", "duplicate", "reordered", "unknown"))
def test_stage_coverage_and_identity_fail_closed(kind: str):
    module = _module()
    payload = _payload(module, traces=1)
    if kind == "missing":
        payload["observations"].pop()
    elif kind == "duplicate":
        payload["observations"].append(dict(payload["observations"][0]))
    elif kind == "reordered":
        payload["observations"][0], payload["observations"][1] = (
            payload["observations"][1],
            payload["observations"][0],
        )
    else:
        payload["observations"][0]["stage"] = "UNKNOWN"
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build(payload, now=NOW)


def test_overlap_negative_duration_and_duration_bound_fail_closed():
    module = _module()
    for name, mutation, reason in (
        (
            "negative",
            lambda row: row.update(ended_at=(NOW - timedelta(hours=1)).isoformat()),
            "TEMPORAL",
        ),
        (
            "overlap",
            lambda row: row.update(started_at=(NOW - timedelta(hours=1)).isoformat()),
            "TEMPORAL",
        ),
        (
            "bound",
            lambda row: row.update(ended_at=(NOW + timedelta(hours=2)).isoformat()),
            "DURATION",
        ),
    ):
        payload = _payload(module, traces=1)
        mutation(payload["observations"][1 if name == "overlap" else 0])
        _rehash(module, payload)
        with pytest.raises(ValueError, match=reason):
            module.build(payload, now=NOW)


def test_stale_future_naive_and_malformed_time_fail_closed():
    module = _module()
    payload = _payload(module)
    with pytest.raises(ValueError, match="STALE"):
        module.build(payload, now=NOW + timedelta(days=2))
    with pytest.raises(ValueError, match="FUTURE"):
        module.build(payload, now=NOW - timedelta(days=1))
    with pytest.raises(ValueError, match="EVALUATION_TIMEZONE"):
        module.build(payload, now=NOW.replace(tzinfo=None))
    payload["observations"][0]["started_at"] = "not-a-time"
    _rehash(module, payload)
    with pytest.raises(ValueError, match="TIMESTAMP"):
        module.build(payload, now=NOW)


def test_tampering_fields_bad_evidence_and_empty_fail_closed():
    module = _module()
    payload = _payload(module)
    payload["extra"] = True
    with pytest.raises(ValueError, match="HASH_INVALID"):
        module.build(payload, now=NOW)
    payload = _payload(module)
    payload["observations"][0]["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError, match="FIELDS"):
        module.build(payload, now=NOW)
    payload = _payload(module)
    payload["observations"][0]["evidence_hash"] = "bad"
    _rehash(module, payload)
    with pytest.raises(ValueError, match="EVIDENCE_HASH"):
        module.build(payload, now=NOW)
    payload = {"schema": module.INPUT_SCHEMA, "observations": []}
    payload["artifact_hash"] = module._hash(payload)
    with pytest.raises(ValueError, match="MISSING"):
        module.build(payload, now=NOW)


def test_source_has_no_database_service_network_or_exchange_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4bp_time_to_trade_baseline.py"
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
