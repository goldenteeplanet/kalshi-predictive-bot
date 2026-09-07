from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bi_operator_runbook_simulator.py"
    spec = importlib.util.spec_from_file_location("phase4bi_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _steps(module, *, refusal_at: int | None = None):
    steps = []
    previous = "0" * 64
    for sequence, stage in enumerate(module.STAGES, start=1):
        if stage == "FINAL_REFUSAL_OR_HANDOFF":
            status = "FINAL_REFUSAL" if refusal_at is not None else "HANDOFF_NON_PRODUCTION"
        elif refusal_at is not None and sequence == refusal_at:
            status = "REFUSED"
        elif refusal_at is not None and sequence > refusal_at:
            status = "SKIPPED"
        else:
            status = "PASS"
        reasons = ["SAFE_REFUSAL"] if status == "REFUSED" else []
        step = {
            "sequence": sequence,
            "stage": stage,
            "status": status,
            "input_hash": previous,
            "reason_codes": reasons,
        }
        row = {
            **step,
            "reason_codes": sorted(set(reasons)),
            "production_mutation_performed": False,
            "execution_authorized": False,
        }
        row["step_hash"] = module.canonical_hash(row)
        previous = row["step_hash"]
        steps.append(step)
    return steps


def _fixture(tmp_path: Path, *, refusal_at: int | None = None):
    module = _module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    payload = {"schema": module.INPUT_SCHEMA, "steps": _steps(module, refusal_at=refusal_at)}
    payload["artifact_hash"] = module._hash(payload)
    path = tmp_path / "runbook.json"
    path.write_text(json.dumps(payload))
    return module, path


def _mutate(module, path: Path, mutate, *, rehash=True):
    payload = json.loads(path.read_text())
    mutate(payload)
    if rehash:
        payload["artifact_hash"] = module._hash(payload)
    path.write_text(json.dumps(payload))


def test_complete_safe_workflow_ends_in_nonproduction_handoff_after_rollback(tmp_path: Path):
    module, path = _fixture(tmp_path)
    transcript, proof = module.build(path, now=NOW)
    assert transcript["final_status"] == "HANDOFF_NON_PRODUCTION"
    assert [row["stage"] for row in transcript["steps"]] == list(module.STAGES)
    assert proof["authorization_before_simulation_verified"] is True
    assert proof["rollback_before_handoff_verified"] is True
    assert proof["handoff_is_non_production"] is True
    assert transcript["artifact_hash"] == module._hash(transcript)


@pytest.mark.parametrize("refusal_at", range(1, 8))
def test_refusal_at_every_intermediate_stage_skips_remaining_and_terminates(
    tmp_path: Path, refusal_at: int
):
    module, path = _fixture(tmp_path, refusal_at=refusal_at)
    transcript, proof = module.build(path, now=NOW)
    assert transcript["final_status"] == "FINAL_REFUSAL"
    assert all(row["status"] == "SKIPPED" for row in transcript["steps"][refusal_at:7])
    assert proof["handoff_is_non_production"] is False


def test_reordering_bypass_lineage_and_continuation_after_refusal_fail_closed(tmp_path: Path):
    cases = (
        (
            "reorder",
            lambda p: p["steps"].reverse(),
            "STAGE_COVERAGE_OR_ORDER_INVALID",
        ),
        (
            "lineage",
            lambda p: p["steps"][2].update(input_hash="0" * 64),
            "STEP_LINEAGE_INVALID",
        ),
        (
            "bypass",
            lambda p: p["steps"][2].update(status="SKIPPED"),
            "UNSAFE_STAGE_BYPASS",
        ),
    )
    for name, mutation, reason in cases:
        module, path = _fixture(tmp_path / name)
        _mutate(module, path, mutation)
        with pytest.raises(ValueError, match=reason):
            module.build(path, now=NOW)
    module, path = _fixture(tmp_path / "continue", refusal_at=3)
    _mutate(module, path, lambda p: p["steps"][3].update(status="PASS"))
    with pytest.raises(ValueError, match="UNSAFE_CONTINUATION_AFTER_REFUSAL"):
        module.build(path, now=NOW)


def test_handoff_after_refusal_and_invalid_final_status_fail_closed(tmp_path: Path):
    module, path = _fixture(tmp_path / "handoff", refusal_at=7)
    _mutate(
        module,
        path,
        lambda p: p["steps"][-1].update(status="HANDOFF_NON_PRODUCTION"),
    )
    with pytest.raises(ValueError, match="REFUSAL_NOT_TERMINAL"):
        module.build(path, now=NOW)
    module, path = _fixture(tmp_path / "final")
    _mutate(module, path, lambda p: p["steps"][-1].update(status="FINAL_REFUSAL"))
    with pytest.raises(ValueError, match="VALID_FLOW_MISSING_HANDOFF"):
        module.build(path, now=NOW)


def test_tampering_naive_time_and_static_nonexecuting_surface(tmp_path: Path):
    module, path = _fixture(tmp_path / "tamper")
    _mutate(module, path, lambda p: p.update(extra=True), rehash=False)
    with pytest.raises(ValueError, match="INPUT_SCHEMA_OR_HASH_INVALID"):
        module.build(path, now=NOW)
    module, path = _fixture(tmp_path / "time")
    with pytest.raises(ValueError, match="EVALUATION_TIMEZONE_MISSING"):
        module.build(path, now=datetime(2026, 8, 25, 12, 0))
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4bi_operator_runbook_simulator.py"
    ).read_text()
    assert "sqlite3" not in source
    assert "subprocess" not in source
    assert "--production-db" not in source
