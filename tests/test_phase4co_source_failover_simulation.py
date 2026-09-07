from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4co_source_failover_simulation.py"
    spec = importlib.util.spec_from_file_location("phase4co_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    first, second, third = "a" * 64, "b" * 64, "c" * 64
    payload = {
        "schema": module.INPUT_SCHEMA,
        "primary_source": "primary",
        "sources": [
            {"source_id": "primary", "priority": 0, "contract_hash": first},
            {"source_id": "alternate", "priority": 1, "contract_hash": second},
            {"source_id": "unproven", "priority": 2, "contract_hash": third},
        ],
        "equivalence_claims": [
            {
                "left_source": "primary",
                "right_source": "alternate",
                "left_contract_hash": first,
                "right_contract_hash": second,
                "schema_equivalent": True,
                "unit_equivalent": True,
                "timestamp_equivalent": True,
                "tolerance_proven": True,
            }
        ],
        "scenarios": [
            {"name": "healthy", "unavailable_sources": []},
            {"name": "primary-out", "unavailable_sources": ["primary"]},
            {"name": "only-unproven", "unavailable_sources": ["primary", "alternate"]},
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_healthy_failover_and_refusal_paths_are_distinct():
    module = _module()
    report = module.build_report(_payload(module))
    by_name = {row["name"]: row for row in report["scenarios"]}
    assert by_name["healthy"]["status"] == "PRIMARY"
    assert by_name["primary-out"]["status"] == "FAILOVER"
    assert by_name["primary-out"]["selected_source"] == "alternate"
    assert by_name["only-unproven"]["status"] == "REFUSE"
    assert by_name["only-unproven"]["selected_source"] is None
    assert report["runtime_failover_applied"] is False
    assert report["network_calls_performed"] == 0


@pytest.mark.parametrize(
    ("kind", "reason"),
    [("schema", "EQUIVALENCE_UNPROVEN"), ("hash", "CONTRACT_HASH_MISMATCH")],
)
def test_unproven_or_stale_equivalence_fails_closed(kind: str, reason: str):
    module = _module()
    payload = _payload(module)
    if kind == "schema":
        payload["equivalence_claims"][0]["schema_equivalent"] = False
    else:
        payload["equivalence_claims"][0]["right_contract_hash"] = "d" * 64
    _rehash(module, payload)
    row = module.build_report(payload)["scenarios"][1]
    assert row["status"] == "REFUSE"
    assert row["rejected_candidates"][0]["reason"] == reason


def test_priority_selects_first_proven_available_alternate():
    module = _module()
    payload = _payload(module)
    payload["sources"].append({"source_id": "later", "priority": 3, "contract_hash": "e" * 64})
    payload["equivalence_claims"].append(
        {
            "left_source": "primary",
            "right_source": "later",
            "left_contract_hash": "a" * 64,
            "right_contract_hash": "e" * 64,
            "schema_equivalent": True,
            "unit_equivalent": True,
            "timestamp_equivalent": True,
            "tolerance_proven": True,
        }
    )
    _rehash(module, payload)
    assert module.build_report(payload)["scenarios"][1]["selected_source"] == "alternate"


@pytest.mark.parametrize(
    "kind", ["sources", "source_fields", "source_id", "priority", "contract", "primary"]
)
def test_malformed_source_configuration_fails_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "sources":
        payload["sources"] = []
    elif kind == "source_fields":
        payload["sources"][0]["extra"] = True
    elif kind == "source_id":
        payload["sources"][1]["source_id"] = "primary"
    elif kind == "priority":
        payload["sources"][1]["priority"] = 0
    elif kind == "contract":
        payload["sources"][0]["contract_hash"] = "bad"
    else:
        payload["primary_source"] = "missing"
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


@pytest.mark.parametrize("kind", ["claim_fields", "claim_source", "claim_duplicate", "flag"])
def test_malformed_equivalence_claims_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "claim_fields":
        payload["equivalence_claims"][0]["extra"] = True
    elif kind == "claim_source":
        payload["equivalence_claims"][0]["right_source"] = "missing"
    elif kind == "claim_duplicate":
        payload["equivalence_claims"].append(dict(payload["equivalence_claims"][0]))
    else:
        payload["equivalence_claims"][0]["unit_equivalent"] = 1
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


@pytest.mark.parametrize("kind", ["empty", "fields", "name", "unknown", "duplicate"])
def test_malformed_scenarios_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "empty":
        payload["scenarios"] = []
    elif kind == "fields":
        payload["scenarios"][0]["extra"] = True
    elif kind == "name":
        payload["scenarios"][1]["name"] = "healthy"
    elif kind == "unknown":
        payload["scenarios"][0]["unavailable_sources"] = ["missing"]
    else:
        payload["scenarios"][0]["unavailable_sources"] = ["primary", "primary"]
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["scenarios"][0]["unavailable_sources"] = ["primary"]
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "failover.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_network_database_service_or_exchange_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4co_source_failover_simulation.py"
    ).read_text()
    for token in (
        "requests",
        "httpx",
        "sqlite3",
        "subprocess",
        "systemctl",
        "exchange_client",
        "/home/james",
    ):
        assert token not in source
