from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4br_latency_budget.py"
    spec = importlib.util.spec_from_file_location("phase4br_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    classes = (
        "EXTERNAL_DELAY",
        "SOFT_BUDGET",
        "HARD_DEADLINE",
        "SOFT_BUDGET",
        "HARD_DEADLINE",
        "HARD_DEADLINE",
        "EXPECTED_WAIT",
        "HARD_DEADLINE",
        "SOFT_BUDGET",
    )
    rows = [
        {
            "node": node,
            "budget_class": classes[index],
            "budget_ms": (index + 1) * 1000,
            "observed_p95_ms": (index + 1) * 900,
            "rationale_hash": module.canonical_hash([node, classes[index]]),
        }
        for index, node in enumerate(module.NODES)
    ]
    payload = {
        "schema": module.INPUT_SCHEMA,
        "baseline_hash": module.canonical_hash("baseline"),
        "dag_hash": module.canonical_hash("dag"),
        "budgets": rows,
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_budget_is_complete_deterministic_and_non_authorizing():
    module = _module()
    payload = _payload(module)
    assert module.build(payload) == module.build(payload)
    budget, compliance = module.build(payload)
    assert set(budget["class_coverage"]) == set(module.BUDGET_CLASSES)
    assert compliance["breach_count"] == 0
    assert compliance["advancement_allowed"] is True
    assert budget["configuration_applied"] is False


def test_exact_boundary_soft_breach_and_hard_breach():
    module = _module()
    payload = _payload(module)
    payload["budgets"][0]["observed_p95_ms"] = payload["budgets"][0]["budget_ms"]
    payload["budgets"][1]["observed_p95_ms"] = payload["budgets"][1]["budget_ms"] + 1
    payload["budgets"][2]["observed_p95_ms"] = payload["budgets"][2]["budget_ms"] + 1
    _rehash(module, payload)
    _, compliance = module.build(payload)
    assert [row["state"] for row in compliance["rows"][:3]] == [
        "WITHIN_BUDGET",
        "BUDGET_BREACH",
        "HARD_DEADLINE_BREACH",
    ]
    assert compliance["advancement_allowed"] is False


@pytest.mark.parametrize("kind", ("missing", "reordered", "duplicate", "unknown_class"))
def test_coverage_order_duplicate_and_class_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "missing":
        payload["budgets"].pop()
    elif kind == "reordered":
        payload["budgets"].reverse()
    elif kind == "duplicate":
        payload["budgets"][-1] = dict(payload["budgets"][0])
    else:
        payload["budgets"][0]["budget_class"] = "UNKNOWN"
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build(payload)


@pytest.mark.parametrize(
    "field,value",
    (
        ("budget_ms", 0),
        ("budget_ms", 86_400_001),
        ("observed_p95_ms", -1),
        ("observed_p95_ms", True),
    ),
)
def test_latency_bound_and_type_fail_closed(field: str, value):
    module = _module()
    payload = _payload(module)
    payload["budgets"][0][field] = value
    _rehash(module, payload)
    with pytest.raises(ValueError, match="LATENCY"):
        module.build(payload)


def test_missing_class_tampering_hashes_and_fields_fail_closed():
    module = _module()
    payload = _payload(module)
    for row in payload["budgets"]:
        if row["budget_class"] == "EXPECTED_WAIT":
            row["budget_class"] = "SOFT_BUDGET"
    _rehash(module, payload)
    with pytest.raises(ValueError, match="CLASS_COVERAGE"):
        module.build(payload)
    payload = _payload(module)
    payload["extra"] = True
    with pytest.raises(ValueError, match="HASH_INVALID"):
        module.build(payload)
    payload = _payload(module)
    payload["baseline_hash"] = "bad"
    _rehash(module, payload)
    with pytest.raises(ValueError, match="UPSTREAM_HASH"):
        module.build(payload)
    payload = _payload(module)
    payload["budgets"][0]["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError, match="FIELDS"):
        module.build(payload)


def test_source_is_artifact_only():
    source = (Path(__file__).parents[1] / "scripts/local/phase4br_latency_budget.py").read_text()
    for token in (
        "sqlite3",
        "subprocess",
        "requests",
        "systemctl",
        "exchange_client",
        "/home/james",
    ):
        assert token not in source
