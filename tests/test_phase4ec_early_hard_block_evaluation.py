from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ec_early_hard_block_evaluation.py"
    spec = importlib.util.spec_from_file_location("phase4ec_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidate(identifier="candidate-1"):
    return {
        "candidate_id": identifier,
        "hard_blocks": [
            {
                "block_id": "kill_switch",
                "priority": 0,
                "decisive": True,
                "evidence_complete": True,
                "triggered": False,
                "reason_code": "KILL_SWITCH",
                "work_units": 1,
            },
            {
                "block_id": "loss_limit",
                "priority": 1,
                "decisive": True,
                "evidence_complete": True,
                "triggered": False,
                "reason_code": "LOSS_LIMIT",
                "work_units": 2,
            },
        ],
        "downstream_stages": [
            {"stage_id": "caps", "work_units": 10},
            {"stage_id": "scoring", "work_units": 20},
        ],
    }


def _payload(module):
    payload = {"schema": module.INPUT_SCHEMA, "candidates": [_candidate()]}
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_clear_candidate_executes_downstream_with_exact_equivalence():
    module = _module()
    report = module.build_report(_payload(module))
    row = report["results"][0]
    assert row["legacy_decision"] == row["optimized_decision"] == "CONTINUE"
    assert row["legacy_work_units"] == row["optimized_work_units"] == 33
    assert row["avoided_work_units"] == 0
    assert row["downstream_executed"] is True


def test_decisive_trigger_skips_only_downstream_work():
    module = _module()
    payload = _payload(module)
    payload["candidates"][0]["hard_blocks"][1]["triggered"] = True
    _rehash(module, payload)
    row = module.build_report(payload)["results"][0]
    assert row["legacy_decision"] == row["optimized_decision"] == "HARD_BLOCK"
    assert row["reason_codes"] == ["LOSS_LIMIT"]
    assert row["optimized_work_units"] == 3
    assert row["avoided_work_units"] == 30
    assert row["downstream_executed"] is False


def test_incomplete_evidence_refuses_and_skips_downstream():
    module = _module()
    payload = _payload(module)
    payload["candidates"][0]["hard_blocks"][0]["evidence_complete"] = False
    _rehash(module, payload)
    row = module.build_report(payload)["results"][0]
    assert row["optimized_decision"] == "REFUSE_INCOMPLETE_EVIDENCE"
    assert row["reason_codes"] == ["INCOMPLETE:KILL_SWITCH"]
    assert row["downstream_executed"] is False


def test_nondecisive_trigger_cannot_short_circuit():
    module = _module()
    payload = _payload(module)
    payload["candidates"][0]["hard_blocks"][0]["decisive"] = False
    payload["candidates"][0]["hard_blocks"][0]["triggered"] = True
    _rehash(module, payload)
    assert module.build_report(payload)["results"][0]["optimized_decision"] == "CONTINUE"


def test_priority_then_id_controls_order_and_reason_order():
    module = _module()
    payload = _payload(module)
    for block in payload["candidates"][0]["hard_blocks"]:
        block["priority"] = 0
        block["triggered"] = True
    payload["candidates"][0]["hard_blocks"].reverse()
    _rehash(module, payload)
    row = module.build_report(payload)["results"][0]
    assert row["hard_block_order"] == ["kill_switch", "loss_limit"]
    assert row["reason_codes"] == ["KILL_SWITCH", "LOSS_LIMIT"]


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate_candidate",
        "duplicate_block",
        "duplicate_stage",
        "empty_blocks",
        "empty_stages",
        "bad_priority",
        "bad_work",
        "bad_boolean",
        "fields",
    ],
)
def test_malformed_pipeline_fails_closed(kind):
    module = _module()
    payload = _payload(module)
    candidate = payload["candidates"][0]
    if kind == "duplicate_candidate":
        payload["candidates"].append(copy.deepcopy(candidate))
    elif kind == "duplicate_block":
        candidate["hard_blocks"].append(dict(candidate["hard_blocks"][0]))
    elif kind == "duplicate_stage":
        candidate["downstream_stages"].append(dict(candidate["downstream_stages"][0]))
    elif kind == "empty_blocks":
        candidate["hard_blocks"] = []
    elif kind == "empty_stages":
        candidate["downstream_stages"] = []
    elif kind == "bad_priority":
        candidate["hard_blocks"][0]["priority"] = -1
    elif kind == "bad_work":
        candidate["downstream_stages"][0]["work_units"] = True
    elif kind == "bad_boolean":
        candidate["hard_blocks"][0]["triggered"] = 1
    else:
        candidate["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_candidate_order_is_canonicalized_and_input_not_mutated():
    module = _module()
    payload = _payload(module)
    payload["candidates"].append(_candidate("candidate-0"))
    _rehash(module, payload)
    original = copy.deepcopy(payload)
    report = module.build_report(payload)
    assert [row["candidate_id"] for row in report["results"]] == ["candidate-0", "candidate-1"]
    assert payload == original


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["candidates"][0]["downstream_stages"][0]["work_units"] = 11
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "evaluation.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4ec_early_hard_block_evaluation.py"
    ).read_text()
    for token in (
        "sqlite3",
        "requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "create_order",
        "/home/james",
    ):
        assert token not in source
