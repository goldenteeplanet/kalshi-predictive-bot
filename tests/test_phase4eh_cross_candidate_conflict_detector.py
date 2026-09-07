from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4eh_cross_candidate_conflict_detector.py"
    spec = importlib.util.spec_from_file_location("phase4eh_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidate(identifier, exposure="30", group="weather", concentration="20"):
    return {
        "candidate_id": identifier,
        "snapshot_artifact_hash": "a" * 64,
        "exposure": exposure,
        "expected_loss": "10",
        "drawdown": "5",
        "liquidity_use": "8",
        "concentration_group": group,
        "concentration": concentration,
    }


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "snapshot_artifact_hash": "a" * 64,
        "limits": {
            "max_exposure": "100",
            "max_expected_loss": "100",
            "max_drawdown": "100",
            "max_liquidity_use": "100",
            "max_group_concentration": "100",
        },
        "candidates": [_candidate("a"), _candidate("b"), _candidate("c")],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_no_conflicts_at_or_below_all_limits():
    module = _module()
    report = module.build_report(_payload(module))
    assert report["status"] == "NO_CONFLICTS"
    assert report["minimal_conflicts"] == []
    assert report["capital_reserved"] is False


def test_pairwise_conflict_is_minimal_and_supersets_are_omitted():
    module = _module()
    payload = _payload(module)
    payload["candidates"][0]["exposure"] = "60"
    payload["candidates"][1]["exposure"] = "41"
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["minimal_conflicts"] == [
        {
            "candidate_ids": ["a", "b"],
            "reasons": ["EXPOSURE_LIMIT_EXCEEDED"],
            "totals": {
                "exposure": "101",
                "expected_loss": "20",
                "drawdown": "10",
                "liquidity_use": "16",
            },
            "group_concentrations": {"weather": "40"},
        }
    ]


def test_three_way_conflict_detected_when_all_pairs_pass():
    module = _module()
    payload = _payload(module)
    for candidate in payload["candidates"]:
        candidate["exposure"] = "34"
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["minimal_conflicts"][0]["candidate_ids"] == ["a", "b", "c"]
    assert report["minimal_conflicts"][0]["totals"]["exposure"] == "102"


def test_concentration_is_aggregated_only_within_group():
    module = _module()
    payload = _payload(module)
    payload["limits"]["max_group_concentration"] = "50"
    payload["candidates"][2]["concentration_group"] = "crypto"
    _rehash(module, payload)
    assert module.build_report(payload)["status"] == "NO_CONFLICTS"
    payload["candidates"][1]["concentration"] = "31"
    _rehash(module, payload)
    conflict = module.build_report(payload)["minimal_conflicts"][0]
    assert conflict["reasons"] == ["CONCENTRATION_LIMIT_EXCEEDED:weather"]


def test_multiple_constraint_reasons_have_stable_order():
    module = _module()
    payload = _payload(module)
    candidate = payload["candidates"][0]
    for metric in ("exposure", "expected_loss", "drawdown", "liquidity_use", "concentration"):
        candidate[metric] = "101"
    _rehash(module, payload)
    reasons = module.build_report(payload)["minimal_conflicts"][0]["reasons"]
    assert reasons == [
        "EXPOSURE_LIMIT_EXCEEDED",
        "EXPECTED_LOSS_LIMIT_EXCEEDED",
        "DRAWDOWN_LIMIT_EXCEEDED",
        "LIQUIDITY_LIMIT_EXCEEDED",
        "CONCENTRATION_LIMIT_EXCEEDED:weather",
    ]


@pytest.mark.parametrize(
    "kind",
    [
        "empty",
        "too_many",
        "duplicate",
        "snapshot_hash",
        "snapshot_mismatch",
        "decimal",
        "negative",
        "group",
        "limits",
        "fields",
    ],
)
def test_malformed_evidence_fails_closed(kind):
    module = _module()
    payload = _payload(module)
    if kind == "empty":
        payload["candidates"] = []
    elif kind == "too_many":
        payload["candidates"] = [
            _candidate(f"c-{index}") for index in range(module.MAX_CANDIDATES + 1)
        ]
    elif kind == "duplicate":
        payload["candidates"].append(copy.deepcopy(payload["candidates"][0]))
    elif kind == "snapshot_hash":
        payload["snapshot_artifact_hash"] = "bad"
    elif kind == "snapshot_mismatch":
        payload["candidates"][0]["snapshot_artifact_hash"] = "b" * 64
    elif kind == "decimal":
        payload["candidates"][0]["exposure"] = "NaN"
    elif kind == "negative":
        payload["limits"]["max_exposure"] = "-1"
    elif kind == "group":
        payload["candidates"][0]["concentration_group"] = ""
    elif kind == "limits":
        payload["limits"]["extra"] = "1"
    else:
        payload["candidates"][0]["extra"] = "1"
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_candidate_order_is_canonicalized_and_input_not_mutated():
    module = _module()
    payload = _payload(module)
    for candidate in payload["candidates"]:
        candidate["exposure"] = "34"
    _rehash(module, payload)
    first = module.build_report(payload)
    payload["candidates"].reverse()
    _rehash(module, payload)
    original = copy.deepcopy(payload)
    second = module.build_report(payload)
    assert first["minimal_conflicts"] == second["minimal_conflicts"]
    assert payload == original


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["limits"]["max_exposure"] = "99"
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "conflicts.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4eh_cross_candidate_conflict_detector.py"
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
