from __future__ import annotations

import copy
import hashlib
import json

from scripts.local.phase4ls_offline_verifier_mutation_audit import run_audit

SCHEMAS = [
    (
        "phase4ll-drift",
        "phase4ll.runtime-snapshot-drift-classification.v1",
        "classification_sha256",
    ),
    ("phase4lm-ledger", "phase4lm.runtime-observation-ledger.v1", "ledger_sha256"),
    ("phase4ln-decision", "phase4ln.alert-state-contract.v1", "decision_sha256"),
    ("phase4lo-envelope", "phase4lo.alert-delivery-envelope.v1", "envelope_sha256"),
    ("phase4lp-lifecycle", "phase4lp.alert-lifecycle.v1", "lifecycle_sha256"),
    ("phase4lq-retention", "phase4lq.alert-retention-contract.v1", "retention_sha256"),
]


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _artifacts():
    result = []
    previous = None
    for name, schema, hash_field in SCHEMAS:
        content = {"schema": schema, "verdict": "PASS", "evidence": name}
        content[hash_field] = _digest(content)
        result.append(
            {"name": name, "content": content, "depends_on": [previous] if previous else []}
        )
        previous = name
    return result


def test_complete_audit_is_deterministic_and_passes() -> None:
    first = run_audit(_artifacts())
    assert first == run_audit(_artifacts())
    assert first["verdict"] == "PASS"
    assert first["coverage"]["schema_count"] == 6


def test_all_unsafe_mutations_are_refused_with_signatures() -> None:
    result = run_audit(_artifacts())
    assert result["coverage"]["mutation_count"] == 22
    assert result["coverage"]["unsafe_mutations_refused"] == 22
    assert all(len(row["signature_sha256"]) == 64 for row in result["mutations"])


def test_every_schema_has_current_previous_and_next_compatibility_rows() -> None:
    rows = run_audit(_artifacts())["compatibility"]
    assert len(rows) == 18
    assert sum(row["observed"] == "PASS" for row in rows) == 6
    assert sum(row["observed"] == "REFUSE" for row in rows) == 12


def test_input_reordering_is_canonically_equivalent() -> None:
    result = run_audit(list(reversed(_artifacts())))
    assert result["verdict"] == "PASS"
    assert result["coverage"]["canonicalization_order_invariant"] is True


def test_missing_schema_chain_refuses_even_when_subset_manifest_is_valid() -> None:
    artifacts = _artifacts()[:-1]
    result = run_audit(artifacts)
    assert result["verdict"] == "REFUSE"
    assert "COMPLETE_SCHEMA_CHAIN_REQUIRED" in result["errors"]


def test_corrupt_baseline_refuses() -> None:
    artifacts = _artifacts()
    artifacts[0]["content"]["evidence"] = "tampered"
    result = run_audit(artifacts)
    assert result["verdict"] == "REFUSE"
    assert "BASELINE_NOT_PASSING" in result["errors"]


def test_signature_is_stable_for_same_mutation() -> None:
    first = {row["name"]: row["signature_sha256"] for row in run_audit(_artifacts())["mutations"]}
    second = {row["name"]: row["signature_sha256"] for row in run_audit(_artifacts())["mutations"]}
    assert first == second


def test_audit_does_not_mutate_caller_input() -> None:
    artifacts = _artifacts()
    original = copy.deepcopy(artifacts)
    run_audit(artifacts)
    assert artifacts == original


def test_harness_has_no_external_or_action_capability() -> None:
    safety = run_audit(_artifacts())["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
