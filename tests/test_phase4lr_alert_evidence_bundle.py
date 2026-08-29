from __future__ import annotations

import copy
import hashlib
import json

import pytest

from scripts.local.phase4lr_alert_evidence_bundle import build_manifest, verify_bundle


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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


def test_full_chain_builds_in_topological_schema_order() -> None:
    artifacts = list(reversed(_artifacts()))
    result = build_manifest(artifacts)
    assert result["verdict"] == "PASS"
    assert result["topological_order"] == [item[0] for item in SCHEMAS]
    assert result["privacy"]["artifact_bodies_embedded"] is False


def test_offline_rebuild_verifies_exactly_and_deterministically() -> None:
    artifacts = _artifacts()
    manifest = build_manifest(artifacts)
    first = verify_bundle(manifest, artifacts)
    assert first == verify_bundle(manifest, artifacts)
    assert first["verdict"] == "PASS"


@pytest.mark.parametrize(
    "mutation,error",
    [
        (lambda items: items[0]["content"].update(schema="unknown"), "SCHEMA_NOT_ALLOWLISTED"),
        (lambda items: items[0].update(name="../secret"), "NAME_INVALID"),
        (lambda items: items[1].update(name=items[0]["name"]), "DUPLICATE_NAME"),
        (
            lambda items: items[1]["content"].update(schema=items[0]["content"]["schema"]),
            "DUPLICATE_SCHEMA",
        ),
        (lambda items: items[0]["content"].update(evidence="changed"), "HASH_MISMATCH"),
    ],
)
def test_unknown_duplicate_and_tampered_artifacts_refuse(mutation, error: str) -> None:
    artifacts = _artifacts()
    mutation(artifacts)
    assert any(error in item for item in build_manifest(artifacts)["errors"])


def test_missing_dependency_and_cycle_refuse() -> None:
    missing = _artifacts()
    missing[0]["depends_on"] = ["absent"]
    assert any("MISSING_DEPENDENCY" in item for item in build_manifest(missing)["errors"])
    cycle = _artifacts()
    cycle[0]["depends_on"] = [cycle[-1]["name"]]
    assert "DEPENDENCY_CYCLE" in build_manifest(cycle)["errors"]


@pytest.mark.parametrize(
    "value", ["postgresql://user:pass@host/db", "/mnt/c/private", "C:\\Users\\name"]
)
def test_sensitive_urls_and_runtime_paths_refuse(value: str) -> None:
    artifacts = _artifacts()
    artifacts[0]["content"]["evidence"] = value
    artifacts[0]["content"]["classification_sha256"] = _digest(
        {
            key: item
            for key, item in artifacts[0]["content"].items()
            if key != "classification_sha256"
        }
    )
    assert any("SENSITIVE_CONTENT_REJECTED" in item for item in build_manifest(artifacts)["errors"])


def test_size_count_and_dependency_shape_limits_refuse() -> None:
    oversized = _artifacts()
    oversized[0]["content"]["evidence"] = "x" * 66_000
    oversized[0]["content"]["classification_sha256"] = _digest(
        {
            key: item
            for key, item in oversized[0]["content"].items()
            if key != "classification_sha256"
        }
    )
    assert any("SIZE_LIMIT_EXCEEDED" in item for item in build_manifest(oversized)["errors"])
    assert "ARTIFACT_COUNT_OUT_OF_BOUNDS" in build_manifest([])["errors"]
    malformed = _artifacts()
    malformed[0]["depends_on"] = [1]
    assert any("DEPENDENCIES_INVALID" in item for item in build_manifest(malformed)["errors"])


def test_manifest_tampering_or_artifact_substitution_fails_verification() -> None:
    artifacts = _artifacts()
    manifest = build_manifest(artifacts)
    tampered = copy.deepcopy(manifest)
    tampered["artifact_count"] = 0
    assert verify_bundle(tampered, artifacts)["verdict"] == "REFUSE"
    substituted = copy.deepcopy(artifacts)
    substituted[-1]["content"]["evidence"] = "different"
    assert verify_bundle(manifest, substituted)["verdict"] == "REFUSE"


def test_bundle_has_no_external_or_action_capability() -> None:
    safety = verify_bundle(build_manifest(_artifacts()), _artifacts())["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
