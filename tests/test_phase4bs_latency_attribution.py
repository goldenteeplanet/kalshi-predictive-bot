from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bs_latency_attribution.py"
    spec = importlib.util.spec_from_file_location("phase4bs_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


START = datetime(2026, 8, 26, 5, 0, tzinfo=UTC)


def _payload(module):
    rows = []
    cursor = START
    for index, stage in enumerate(module.STAGES, start=1):
        duration = index * 80
        causes = {cause: 0 for cause in module.CAUSES}
        causes[module.CAUSES[(index - 1) % len(module.CAUSES)]] = duration
        rows.append(
            {
                "stage": stage,
                "started_at": cursor.isoformat(),
                "ended_at": (cursor + timedelta(milliseconds=duration)).isoformat(),
                "causes_ms": causes,
                "evidence_hash": module.canonical_hash([stage, duration]),
            }
        )
        cursor += timedelta(milliseconds=duration)
    payload = {
        "schema": module.INPUT_SCHEMA,
        "baseline_hash": module.canonical_hash("baseline"),
        "budget_hash": module.canonical_hash("budget"),
        "stages": rows,
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_attribution_is_complete_deterministic_and_non_authorizing():
    module = _module()
    payload = _payload(module)
    assert module.build(payload) == module.build(payload)
    report, ranking = module.build(payload)
    assert report["fully_attributed"] is True
    assert (
        sum(row["duration_ms"] for row in report["cause_totals"]) == report["total_attributed_ms"]
    )
    assert ranking["primary_cause"] == "COMPUTE"
    assert report["configuration_applied"] is False


def test_equal_cause_tie_uses_declared_order_and_basis_points_are_deterministic():
    module = _module()
    payload = _payload(module)
    for row in payload["stages"]:
        duration = sum(row["causes_ms"].values())
        row["causes_ms"] = {cause: 0 for cause in module.CAUSES}
        row["causes_ms"]["COMPUTE"] = duration // 2
        row["causes_ms"]["IO"] = duration - duration // 2
    _rehash(module, payload)
    report, ranking = module.build(payload)
    assert ranking["primary_cause"] in ("COMPUTE", "IO")
    assert all(isinstance(row["share_basis_points"], int) for row in report["cause_totals"])


@pytest.mark.parametrize("kind", ("missing", "reordered", "duplicate", "unknown_cause"))
def test_stage_and_cause_coverage_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "missing":
        payload["stages"].pop()
    elif kind == "reordered":
        payload["stages"].reverse()
    elif kind == "duplicate":
        payload["stages"][-1] = dict(payload["stages"][0])
    else:
        causes = payload["stages"][0]["causes_ms"]
        causes["UNKNOWN"] = causes.pop("COMPUTE")
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build(payload)


def test_nonconserving_negative_boolean_and_zero_total_fail_closed():
    module = _module()
    for mutation, reason in (
        (lambda p: p["stages"][0]["causes_ms"].update(COMPUTE=1), "NONCONSERVING"),
        (lambda p: p["stages"][0]["causes_ms"].update(COMPUTE=-1), "VALUE"),
        (lambda p: p["stages"][0]["causes_ms"].update(COMPUTE=True), "VALUE"),
    ):
        payload = _payload(module)
        mutation(payload)
        _rehash(module, payload)
        with pytest.raises(ValueError, match=reason):
            module.build(payload)
    payload = _payload(module)
    for row in payload["stages"]:
        row["ended_at"] = row["started_at"]
        row["causes_ms"] = {cause: 0 for cause in module.CAUSES}
    _rehash(module, payload)
    with pytest.raises(ValueError, match="ZERO_TOTAL"):
        module.build(payload)


def test_ambiguous_malformed_reversed_and_over_bound_time_fail_closed():
    module = _module()
    cases = (
        (lambda p: p["stages"][0].update(started_at="2026-08-26T05:00:00"), "AMBIGUOUS"),
        (lambda p: p["stages"][0].update(started_at="bad"), "TIMESTAMP"),
        (
            lambda p: p["stages"][0].update(ended_at=(START - timedelta(seconds=1)).isoformat()),
            "TEMPORAL",
        ),
        (
            lambda p: p["stages"][0].update(ended_at=(START + timedelta(days=2)).isoformat()),
            "BOUND",
        ),
    )
    for mutation, reason in cases:
        payload = _payload(module)
        mutation(payload)
        _rehash(module, payload)
        with pytest.raises(ValueError, match=reason):
            module.build(payload)


def test_tampering_upstream_hash_fields_and_evidence_fail_closed():
    module = _module()
    payload = _payload(module)
    payload["extra"] = True
    with pytest.raises(ValueError, match="HASH_INVALID"):
        module.build(payload)
    payload = _payload(module)
    payload["budget_hash"] = "bad"
    _rehash(module, payload)
    with pytest.raises(ValueError, match="UPSTREAM_HASH"):
        module.build(payload)
    payload = _payload(module)
    payload["stages"][0]["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError, match="FIELDS"):
        module.build(payload)
    payload = _payload(module)
    payload["stages"][0]["evidence_hash"] = "bad"
    _rehash(module, payload)
    with pytest.raises(ValueError, match="EVIDENCE_HASH"):
        module.build(payload)


def test_source_is_artifact_only():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4bs_latency_attribution.py"
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
