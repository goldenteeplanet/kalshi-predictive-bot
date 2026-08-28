from __future__ import annotations

import copy

import pytest

from scripts.local.phase4le_schema_compatibility import CONTRACTS
from scripts.local.phase4lf_migration_simulator import (
    MIGRATION_EXTENSION,
    downgrade_projection,
    migrate,
    simulate_migration,
)


def _value(expected: type):
    return {str: "PASS", int: 1, list: [], dict: {}}[expected]


def _fixture(contract_name: str):
    contract = CONTRACTS[contract_name]
    payload = {field: _value(expected) for field, expected in contract["required"].items()}
    payload["schema"] = f"{contract['family']}.v1"
    if "verdict" in payload:
        payload["verdict"] = "PASS"
    if "errors" in payload:
        payload["errors"] = []
    if "owned_paths" in payload:
        payload["owned_paths"] = ["docs/safe.md"]
    if "safety_verdict" in payload:
        payload["safety_verdict"] = "PASS"
    return payload


@pytest.mark.parametrize("contract_name", sorted(CONTRACTS))
def test_all_contracts_migrate_idempotently_and_round_trip(contract_name: str) -> None:
    result = simulate_migration(contract_name, _fixture(contract_name))
    assert result["verdict"] == "PASS"
    assert result["idempotent"] is True
    assert result["round_trip_equal"] is True


def test_legacy_hash_identity_is_preserved() -> None:
    payload = _fixture("ledger")
    payload["ledger_sha256"] = "a" * 64
    migrated = migrate("ledger", payload)
    assert migrated["ledger_sha256"] == payload["ledger_sha256"]
    assert migrated["extensions"][MIGRATION_EXTENSION]["legacy_hashes_preserved"] is True


def test_unknown_field_and_capability_extension_refuse() -> None:
    payload = _fixture("ledger")
    payload["unexpected"] = True
    assert simulate_migration("ledger", payload)["verdict"] == "REFUSE"
    payload = _fixture("ledger")
    payload["extensions"] = {"vendor.capability": {"order_capability": True}}
    errors = simulate_migration("ledger", payload)["errors"]
    assert any("FORBIDDEN_CAPABILITY" in error for error in errors)


def test_conflicting_migration_extension_refuses() -> None:
    payload = _fixture("ledger")
    payload["extensions"] = {MIGRATION_EXTENSION: {"migration": "other"}}
    assert any(
        error.startswith("MIGRATION_REFUSED:")
        for error in simulate_migration("ledger", payload)["errors"]
    )


def test_downgrade_refuses_required_v11_semantics() -> None:
    candidate = migrate("ledger", _fixture("ledger"))
    candidate["extensions"]["vendor.required"] = {"required_for_v1_1": True}
    with pytest.raises(ValueError, match="conceal required"):
        downgrade_projection("ledger", candidate)


def test_hash_or_immutable_drift_refuses() -> None:
    payload = _fixture("ledger")
    payload["ledger_sha256"] = "a" * 64
    candidate = migrate("ledger", payload)
    candidate["ledger_sha256"] = "b" * 64
    second = copy.deepcopy(candidate)
    result = simulate_migration("ledger", second)
    assert result["verdict"] == "REFUSE"


def test_unknown_contract_and_malformed_evidence_refuse() -> None:
    assert simulate_migration("unknown", {})["verdict"] == "REFUSE"
    assert simulate_migration("ledger", "bad")["verdict"] == "REFUSE"
