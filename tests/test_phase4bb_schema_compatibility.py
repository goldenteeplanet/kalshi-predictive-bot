from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bb_schema_compatibility.py"
    spec = importlib.util.spec_from_file_location("phase4bb_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
SUPPORTED = "phase4ac.fixture.v1"


def _artifact(module, **overrides):
    artifact = {"schema": SUPPORTED, "name": "fixture", "count": 1, "optional": True}
    artifact.update(overrides)
    artifact["artifact_hash"] = module._hash(artifact)
    return artifact


def _fixture(tmp_path: Path, artifacts=None, policies=None):
    module = _module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    policies = policies or [
        {
            "schema": SUPPORTED,
            "required_fields": {
                "schema": "string",
                "name": "string",
                "count": "integer",
                "artifact_hash": "string",
            },
            "optional_fields": {"optional": "boolean"},
            "allow_additional_fields": False,
            "canonicalization": "RFC8785_SORTED_KEYS_V1",
            "hash_field": "artifact_hash",
        }
    ]
    payload = {
        "schema": module.INPUT_SCHEMA,
        "policies": policies,
        "artifacts": artifacts or [_artifact(module)],
    }
    payload["artifact_hash"] = module._hash(payload)
    path = tmp_path / "input.json"
    path.write_text(json.dumps(payload))
    return module, path


def test_exact_supported_schema_and_types_pass(tmp_path: Path):
    module, path = _fixture(tmp_path)
    report, migration = module.build(path, now=NOW)
    assert report["all_artifacts_compatible"] is True
    assert report["supported_schema_count"] == 1
    assert migration["proposal_count"] == 0
    assert migration["existing_artifacts_rewritten"] is False
    assert report["artifact_hash"] == module._hash(report)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda a: a.pop("name"), "MISSING_FIELD:name"),
        (lambda a: a.update(count="one"), "TYPE_CHANGED:count"),
        (lambda a: a.update(optional=None), "TYPE_CHANGED:optional"),
        (lambda a: a.update(added="field"), "ADDED_FIELD:added"),
    ],
)
def test_missing_added_and_type_changes_are_reported(tmp_path: Path, mutate, reason: str):
    module = _module()
    artifact = _artifact(module)
    mutate(artifact)
    artifact["artifact_hash"] = module._hash(artifact)
    module, path = _fixture(tmp_path, artifacts=[artifact])
    report, _ = module.build(path, now=NOW)
    assert reason in report["rows"][0]["reason_codes"]
    assert report["all_artifacts_compatible"] is False


def test_unknown_and_forward_versions_get_deterministic_new_artifact_proposals(tmp_path: Path):
    module = _module()
    artifacts = [
        {"schema": "phase4ac.fixture.v0", "name": "old"},
        {"schema": "phase4ac.fixture.v2", "name": "future"},
        {"schema": "phase4zz.unknown.v1"},
    ]
    module, path = _fixture(tmp_path, artifacts=artifacts)
    report, migration = module.build(path, now=NOW)
    assert all("UNKNOWN_OR_FORWARD_SCHEMA_VERSION" in row["reason_codes"] for row in report["rows"])
    assert migration["proposal_count"] == 2
    assert all(p["target_schema"] == SUPPORTED for p in migration["proposals"])
    assert all(p["action"] == "CREATE_NEW_TEMPORARY_ARTIFACT" for p in migration["proposals"])


def test_canonical_hash_change_and_tampering_fail_closed(tmp_path: Path):
    module = _module()
    artifact = _artifact(module)
    artifact["name"] = "changed-without-rehash"
    module, path = _fixture(tmp_path / "hash", artifacts=[artifact])
    report, _ = module.build(path, now=NOW)
    assert "CANONICAL_HASH_MISMATCH" in report["rows"][0]["reason_codes"]
    module, path = _fixture(tmp_path / "input")
    payload = json.loads(path.read_text())
    payload["extra"] = True
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="INPUT_SCHEMA_OR_HASH_INVALID"):
        module.build(path, now=NOW)


def test_policy_duplication_invalid_types_and_canonicalization_fail_closed(tmp_path: Path):
    module, path = _fixture(tmp_path / "duplicate")
    payload = json.loads(path.read_text())
    payload["policies"].append(dict(payload["policies"][0]))
    payload["artifact_hash"] = module._hash(payload)
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="DUPLICATE_POLICY"):
        module.build(path, now=NOW)
    for field, value, reason in (
        ("required_fields", {"name": "impossible"}, "POLICY_TYPE_INVALID"),
        ("canonicalization", "changed", "CANONICALIZATION_POLICY_INVALID"),
    ):
        module, path = _fixture(tmp_path / reason)
        payload = json.loads(path.read_text())
        payload["policies"][0][field] = value
        payload["artifact_hash"] = module._hash(payload)
        path.write_text(json.dumps(payload))
        with pytest.raises(ValueError, match=reason):
            module.build(path, now=NOW)


def test_naive_time_and_static_no_rewrite_surface(tmp_path: Path):
    module, path = _fixture(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="EVALUATION_TIMEZONE_MISSING"):
        module.build(path, now=datetime(2026, 8, 25, 12, 0))
    assert path.read_bytes() == before
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4bb_schema_compatibility.py"
    ).read_text()
    assert "sqlite3" not in source
    assert "--production-db" not in source
    assert "os.replace" in source
