from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bk_emergency_playbook.py"
    spec = importlib.util.spec_from_file_location("phase4bk_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _fixture(tmp_path: Path):
    module = _module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    scenarios = [
        {
            "sequence": sequence,
            "scenario": scenario,
            "evidence_hashes": [module.canonical_hash({"scenario": scenario})],
        }
        for sequence, scenario in enumerate(module.SCENARIOS, start=1)
    ]
    payload = {"schema": module.INPUT_SCHEMA, "scenarios": scenarios}
    payload["artifact_hash"] = module._hash(payload)
    path = tmp_path / "scenarios.json"
    path.write_text(json.dumps(payload))
    return module, path


def _mutate(module, path: Path, mutate, *, rehash=True):
    payload = json.loads(path.read_text())
    mutate(payload)
    if rehash:
        payload["artifact_hash"] = module._hash(payload)
    path.write_text(json.dumps(payload))


def test_complete_playbook_is_declarative_offline_and_fail_closed(tmp_path: Path):
    module, path = _fixture(tmp_path)
    playbook, proof = module.build(path, now=NOW)
    assert playbook["scenario_count"] == len(module.SCENARIOS) == 9
    assert all(row["terminal_state"].startswith("SAFE_REFUSAL") for row in playbook["rows"])
    assert all(
        row["declarative_actions"] == list(module.ACTIONS[row["scenario"]])
        for row in playbook["rows"]
    )
    assert proof["offline_recovery_only"] is True
    assert proof["kill_commands_present"] is False
    assert playbook["artifact_hash"] == module._hash(playbook)


@pytest.mark.parametrize("scenario", _module().SCENARIOS)
def test_each_emergency_scenario_has_two_nonexecuting_actions(tmp_path: Path, scenario: str):
    module, path = _fixture(tmp_path)
    playbook, _ = module.build(path, now=NOW)
    row = next(item for item in playbook["rows"] if item["scenario"] == scenario)
    assert len(row["declarative_actions"]) == 2
    assert row["commands_present"] is False
    assert row["service_controls_present"] is False
    assert row["database_actions_present"] is False


def test_missing_reordered_unknown_sequence_and_evidence_fail_closed(tmp_path: Path):
    cases = (
        ("missing", lambda p: p["scenarios"].pop(), "SCENARIO_COVERAGE_OR_ORDER_INVALID"),
        ("reordered", lambda p: p["scenarios"].reverse(), "SCENARIO_COVERAGE_OR_ORDER_INVALID"),
        (
            "unknown",
            lambda p: p["scenarios"][0].update(scenario="UNKNOWN"),
            "SCENARIO_COVERAGE_OR_ORDER_INVALID",
        ),
        (
            "sequence",
            lambda p: p["scenarios"][0].update(sequence=2),
            "SCENARIO_SEQUENCE_INVALID",
        ),
        (
            "evidence",
            lambda p: p["scenarios"][0].update(evidence_hashes=[]),
            "EVIDENCE_HASH_INVALID",
        ),
    )
    for name, mutation, reason in cases:
        module, path = _fixture(tmp_path / name)
        _mutate(module, path, mutation)
        with pytest.raises(ValueError, match=reason):
            module.build(path, now=NOW)


def test_tampering_naive_time_and_static_no_emergency_commands(tmp_path: Path):
    module, path = _fixture(tmp_path / "tamper")
    _mutate(module, path, lambda p: p.update(extra=True), rehash=False)
    with pytest.raises(ValueError, match="INPUT_SCHEMA_OR_HASH_INVALID"):
        module.build(path, now=NOW)
    module, path = _fixture(tmp_path / "time")
    with pytest.raises(ValueError, match="EVALUATION_TIMEZONE_MISSING"):
        module.build(path, now=datetime(2026, 8, 25, 12, 0))
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4bk_emergency_playbook.py"
    ).read_text()
    assert "systemctl" not in source
    assert "subprocess" not in source
    assert "sqlite3" not in source
    assert "kill " not in source.lower()
    assert "--production-db" not in source
