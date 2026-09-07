from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bt_freshness_propagation.py"
    spec = importlib.util.spec_from_file_location("phase4bt_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 26, 5, 0, tzinfo=UTC)


def _payload(module, *, age_seconds=30, current=True):
    rows, previous = [], None
    data_as_of = NOW - timedelta(seconds=age_seconds)
    for index, stage in enumerate(module.STAGES):
        row = {
            "stage": stage,
            "produced_at": (NOW - timedelta(seconds=len(module.STAGES) - index)).isoformat(),
            "data_as_of": data_as_of.isoformat(),
            "max_age_seconds": 60,
            "reported_current": current,
            "predecessor_hash": previous,
        }
        row["row_hash"] = module._hash(row, "row_hash")
        previous = row["row_hash"]
        rows.append(row)
    payload = {"schema": module.INPUT_SCHEMA, "stages": rows}
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rechain(module, payload):
    previous = None
    for row in payload["stages"]:
        row["predecessor_hash"] = previous
        row["row_hash"] = module._hash(row, "row_hash")
        previous = row["row_hash"]
    payload["artifact_hash"] = module._hash(payload)


def test_fresh_chain_is_deterministic_complete_and_non_authorizing():
    module = _module()
    payload = _payload(module)
    assert module.build(payload, now=NOW) == module.build(payload, now=NOW)
    audit, verdict = module.build(payload, now=NOW)
    assert verdict["verdict"] == "FRESHNESS_PROPAGATION_VALID"
    assert audit["finding_count"] == 0
    assert all(row["evaluation_fresh"] for row in audit["stages"])
    assert audit["production_records_created"] == 0


def test_stale_but_current_and_fresh_but_stale_are_detected():
    module = _module()
    _, verdict = module.build(_payload(module, age_seconds=61), now=NOW)
    assert verdict["stale_current_contradiction_count"] == len(module.STAGES)
    assert verdict["advancement_allowed"] is False
    audit, verdict = module.build(_payload(module, current=False), now=NOW)
    assert {row["finding"] for row in audit["findings"]} == {"FRESH_BUT_REPORTED_STALE"}
    assert verdict["advancement_allowed"] is False


def test_exact_freshness_boundary_is_inclusive():
    module = _module()
    audit, verdict = module.build(_payload(module, age_seconds=60), now=NOW)
    assert audit["finding_count"] == 0
    assert verdict["advancement_allowed"] is True


@pytest.mark.parametrize("kind", ("missing", "reordered", "duplicate", "lineage"))
def test_coverage_order_duplicate_and_lineage_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "missing":
        payload["stages"].pop()
        _rechain(module, payload)
    elif kind == "reordered":
        payload["stages"].reverse()
        _rechain(module, payload)
    elif kind == "duplicate":
        payload["stages"][-1] = dict(payload["stages"][0])
        _rechain(module, payload)
    else:
        payload["stages"][1]["predecessor_hash"] = "a" * 64
        payload["stages"][1]["row_hash"] = module._hash(payload["stages"][1], "row_hash")
        payload["artifact_hash"] = module._hash(payload)
    with pytest.raises(ValueError):
        module.build(payload, now=NOW)


def test_freshness_regression_future_data_future_production_and_naive_fail_closed():
    module = _module()
    cases = (
        lambda p: p["stages"][1].update(data_as_of=(NOW - timedelta(minutes=2)).isoformat()),
        lambda p: p["stages"][0].update(data_as_of=(NOW + timedelta(seconds=1)).isoformat()),
        lambda p: p["stages"][0].update(produced_at=(NOW + timedelta(seconds=1)).isoformat()),
        lambda p: p["stages"][0].update(data_as_of="2026-08-26T04:59:00"),
    )
    for mutation in cases:
        payload = _payload(module)
        mutation(payload)
        _rechain(module, payload)
        with pytest.raises(ValueError):
            module.build(payload, now=NOW)
    with pytest.raises(ValueError, match="EVALUATION_TIMEZONE"):
        module.build(_payload(module), now=NOW.replace(tzinfo=None))


@pytest.mark.parametrize("maximum", (-1, 604_801, 1.5, True))
def test_max_age_bound_and_type_fail_closed(maximum):
    module = _module()
    payload = _payload(module)
    payload["stages"][0]["max_age_seconds"] = maximum
    _rechain(module, payload)
    with pytest.raises(ValueError, match="MAX_AGE"):
        module.build(payload, now=NOW)


def test_tampering_fields_row_hash_and_current_type_fail_closed():
    module = _module()
    payload = _payload(module)
    payload["extra"] = True
    with pytest.raises(ValueError, match="HASH_INVALID"):
        module.build(payload, now=NOW)
    payload = _payload(module)
    payload["stages"][0]["extra"] = True
    _rechain(module, payload)
    with pytest.raises(ValueError, match="FIELDS"):
        module.build(payload, now=NOW)
    payload = _payload(module)
    payload["stages"][0]["row_hash"] = "bad"
    payload["artifact_hash"] = module._hash(payload)
    with pytest.raises(ValueError, match="ROW_HASH"):
        module.build(payload, now=NOW)
    payload = _payload(module)
    payload["stages"][0]["reported_current"] = 1
    _rechain(module, payload)
    with pytest.raises(ValueError, match="REPORTED_CURRENT"):
        module.build(payload, now=NOW)


def test_source_is_artifact_only():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4bt_freshness_propagation.py"
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
