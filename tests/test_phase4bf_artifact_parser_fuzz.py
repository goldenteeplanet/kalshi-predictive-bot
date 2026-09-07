from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bf_artifact_parser_fuzz.py"
    spec = importlib.util.spec_from_file_location("phase4bf_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _fixture(tmp_path: Path):
    module = _module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    targets = [
        {
            "phase": phase,
            "schema": f"phase{phase.lower()}.fuzz-target.v1",
            "cli": f"scripts/local/phase{phase.lower()}_tool.py",
            "hash_field": "artifact_hash",
        }
        for phase in module.REQUIRED_PHASES
    ]
    payload = {
        "schema": module.INPUT_SCHEMA,
        "value": json.dumps(targets, sort_keys=True),
        "relative_path": "catalog/phase4bf-targets.json",
        "evaluated_at": NOW.isoformat(),
    }
    payload["artifact_hash"] = module._hash(payload)
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(payload))
    return module, path, targets


def test_all_phase_targets_and_mutation_classes_are_safe(tmp_path: Path):
    module, path, targets = _fixture(tmp_path)
    report, proof = module.build(path, now=NOW)
    assert report["target_count"] == len(module.REQUIRED_PHASES) == len(targets)
    assert report["case_count"] == len(targets) * len(module.CASES)
    assert report["all_cases_safe"] is True
    assert proof["duplicate_keys_rejected"] is True
    assert proof["bounded_resource_use"] is True
    assert report["artifact_hash"] == module._hash(report)


@pytest.mark.parametrize("case", _module().CASES)
def test_each_fuzz_case_has_expected_fail_closed_or_canonical_acceptance(case: str):
    module = _module()
    data, should_accept = module._case("phase4ac.fixture.v1", "artifact_hash", case)
    if should_accept:
        parsed = module.parse(
            data, expected_schema="phase4ac.fixture.v1", hash_field="artifact_hash"
        )
        assert "☃" in parsed["value"]
    else:
        with pytest.raises(ValueError):
            module.parse(data, expected_schema="phase4ac.fixture.v1", hash_field="artifact_hash")


def test_artifact_byte_limit_is_checked_before_json_parse():
    module = _module()
    data = b"{" + b" " * module.MAX_BYTES + b"}"
    with pytest.raises(ValueError, match="ARTIFACT_SIZE_LIMIT_EXCEEDED"):
        module.parse(data, expected_schema="phase4ac.fixture.v1", hash_field="artifact_hash")


def test_missing_reordered_duplicate_and_invalid_targets_fail_closed(tmp_path: Path):
    for case in ("missing", "reordered", "duplicate", "invalid"):
        module, path, targets = _fixture(tmp_path / case)
        if case == "missing":
            targets.pop()
        elif case == "reordered":
            targets.reverse()
        elif case == "duplicate":
            targets[1]["schema"] = targets[0]["schema"]
        else:
            targets[0]["hash_field"] = "unknown"
        payload = json.loads(path.read_text())
        payload["value"] = json.dumps(targets, sort_keys=True)
        payload["artifact_hash"] = module._hash(payload)
        path.write_text(json.dumps(payload))
        with pytest.raises(ValueError):
            module.build(path, now=NOW)


def test_catalog_tampering_naive_time_and_static_no_external_capability(tmp_path: Path):
    module, path, _ = _fixture(tmp_path / "tamper")
    payload = json.loads(path.read_text())
    payload["value"] += " "
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        module.build(path, now=NOW)
    module, path, _ = _fixture(tmp_path / "time")
    with pytest.raises(ValueError, match="EVALUATION_TIMEZONE_MISSING"):
        module.build(path, now=datetime(2026, 8, 25, 12, 0))
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4bf_artifact_parser_fuzz.py"
    ).read_text()
    assert "sqlite3" not in source
    assert "subprocess" not in source
    assert "urllib" not in source and "requests" not in source
