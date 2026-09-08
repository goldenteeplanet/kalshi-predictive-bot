from __future__ import annotations

import copy
import json

import pytest

from scripts.local.phase4le_schema_compatibility import (
    CONTRACTS,
    assess_compatibility,
    assess_serialized,
)


def _ledger():
    return {
        "schema": "phase4lb.commit-evidence-ledger.v1",
        "verdict": "PASS",
        "receipt_count": 4,
        "errors": [],
        "first_phase": "4KX",
        "last_phase": "4LA",
        "ledger_sha256": "a" * 64,
        "tip_receipt_sha256": "b" * 64,
        "safety": {"read_only_validation": True},
    }


def test_supported_minor_extension_passes() -> None:
    base = _ledger()
    candidate = copy.deepcopy(base)
    candidate["schema"] = "phase4lb.commit-evidence-ledger.v1.1"
    candidate["extensions"] = {"dejoia.display_hint": {"label": "verified"}}
    assert assess_compatibility("ledger", base, candidate)["verdict"] == "PASS"


@pytest.mark.parametrize("contract", sorted(CONTRACTS))
def test_all_contract_families_are_bound(contract: str) -> None:
    assert CONTRACTS[contract]["family"].startswith("phase4")
    assert "schema" in CONTRACTS[contract]["required"]


def test_unknown_major_and_minor_rollback_refuse() -> None:
    base = _ledger()
    candidate = copy.deepcopy(base)
    candidate["schema"] = "phase4lb.commit-evidence-ledger.v2"
    assert "UNSUPPORTED_MAJOR_VERSION" in assess_compatibility("ledger", base, candidate)["errors"]
    base["schema"] = "phase4lb.commit-evidence-ledger.v1.1"
    candidate["schema"] = "phase4lb.commit-evidence-ledger.v1"
    assert (
        "UNSUPPORTED_OR_ROLLBACK_MINOR_VERSION"
        in assess_compatibility("ledger", base, candidate)["errors"]
    )


def test_required_removal_and_type_change_refuse() -> None:
    base = _ledger()
    missing = copy.deepcopy(base)
    missing.pop("receipt_count")
    assert (
        "MISSING_REQUIRED_FIELD:receipt_count"
        in assess_compatibility("ledger", base, missing)["errors"]
    )
    changed = copy.deepcopy(base)
    changed["receipt_count"] = "4"
    assert (
        "REQUIRED_FIELD_TYPE_CHANGED:receipt_count"
        in assess_compatibility("ledger", base, changed)["errors"]
    )


def test_immutable_semantic_weakening_refuses() -> None:
    base = _ledger()
    candidate = copy.deepcopy(base)
    candidate["verdict"] = "WARN"
    assert (
        "IMMUTABLE_FIELD_CHANGED:verdict"
        in assess_compatibility("ledger", base, candidate)["errors"]
    )


def test_conflicting_extension_and_alias_refuse() -> None:
    base = _ledger()
    candidate = copy.deepcopy(base)
    candidate["new_top_level"] = True
    candidate["extensions"] = {"Vendor.Flag": 1, "vendor.flag": 2}
    errors = assess_compatibility("ledger", base, candidate)["errors"]
    assert "CONFLICTING_TOP_LEVEL_EXTENSION:new_top_level" in errors
    assert "INVALID_OR_DUPLICATE_EXTENSION_ALIAS" in errors


@pytest.mark.parametrize(
    "capability",
    ["database_write", "service_control", "network_access", "writer_lock", "order_capability"],
)
def test_capability_expansion_refuses(capability: str) -> None:
    base = _ledger()
    candidate = copy.deepcopy(base)
    candidate["extensions"] = {"dejoia.capabilities": {capability: True}}
    errors = assess_compatibility("ledger", base, candidate)["errors"]
    assert any(error.startswith("FORBIDDEN_CAPABILITY:") for error in errors)


def test_noncanonical_serialization_refuses() -> None:
    base = _ledger()
    candidate = copy.deepcopy(base)
    pretty = json.dumps(candidate, indent=2)
    canonical = json.dumps(base, sort_keys=True, separators=(",", ":"))
    assert "NON_CANONICAL_SERIALIZATION" in assess_serialized("ledger", canonical, pretty)["errors"]
