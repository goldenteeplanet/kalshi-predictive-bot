from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = (
        Path(__file__).parents[1]
        / "scripts/local/phase4dy_forecast_ranking_residual_risk_review.py"
    )
    spec = importlib.util.spec_from_file_location("phase4dy_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "performance_gains": [
            {
                "id": "G-1",
                "optimization": "memoization",
                "metric": "work",
                "before_work_units": 10,
                "after_work_units": 6,
                "evidence": "4DX exact replay",
            }
        ],
        "unsupported_optimizations": [
            {
                "id": "U-1",
                "optimization": "lossy rounding",
                "reason": "logical drift",
                "required_evidence": "exact equivalence",
            }
        ],
        "model_risks": [
            {
                "id": "R-1",
                "risk": "stale feature",
                "severity": "HIGH",
                "mitigation": "freshness refusal",
                "residual_exposure": "clock skew",
            }
        ],
        "critical_path_costs": [
            {
                "id": "C-1",
                "stage": "ranking",
                "cost_driver": "sorting",
                "measurement": "deterministic work units",
                "next_action": "bounded benchmark",
            }
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def test_complete_review_is_deterministic_and_non_authorizing():
    module = _module()
    payload = _payload(module)
    original = copy.deepcopy(payload)
    report = module.build_report(payload)
    assert report == module.build_report(payload)
    assert payload == original
    assert report["status"] == "RESIDUAL_RISK_REVIEW_COMPLETE"
    assert report["total_deterministic_work_reduction"] == 4
    assert report["high_or_critical_risk_count"] == 1
    assert report["execution_authorized"] is False
    assert report["production_deployment_authorized"] is False


@pytest.mark.parametrize(
    "section",
    ["performance_gains", "unsupported_optimizations", "model_risks", "critical_path_costs"],
)
def test_empty_required_section_fails_closed(section):
    module = _module()
    payload = _payload(module)
    payload[section] = []
    payload["artifact_hash"] = module._hash(payload)
    with pytest.raises(ValueError, match="EMPTY"):
        module.build_report(payload)


@pytest.mark.parametrize(
    "kind", ["outer_hash", "duplicate", "severity", "regression", "boolean_work", "extra_field"]
)
def test_invalid_evidence_fails_closed(kind):
    module = _module()
    payload = _payload(module)
    if kind == "outer_hash":
        payload["model_risks"][0]["risk"] = "changed"
    elif kind == "duplicate":
        payload["model_risks"].append(dict(payload["model_risks"][0]))
        payload["artifact_hash"] = module._hash(payload)
    elif kind == "severity":
        payload["model_risks"][0]["severity"] = "UNKNOWN"
        payload["artifact_hash"] = module._hash(payload)
    elif kind == "regression":
        payload["performance_gains"][0]["after_work_units"] = 11
        payload["artifact_hash"] = module._hash(payload)
    elif kind == "boolean_work":
        payload["performance_gains"][0]["after_work_units"] = True
        payload["artifact_hash"] = module._hash(payload)
    else:
        payload["critical_path_costs"][0]["extra"] = "x"
        payload["artifact_hash"] = module._hash(payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_input_order_is_canonicalized():
    module = _module()
    payload = _payload(module)
    second = dict(payload["model_risks"][0])
    second["id"] = "R-0"
    second["severity"] = "LOW"
    payload["model_risks"].append(second)
    payload["artifact_hash"] = module._hash(payload)
    report = module.build_report(payload)
    assert [row["id"] for row in report["model_risks"]] == ["R-0", "R-1"]


def test_atomic_publication(tmp_path: Path):
    module = _module()
    report = module.build_report(_payload(module))
    output = tmp_path / "review.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1]
        / "scripts/local/phase4dy_forecast_ranking_residual_risk_review.py"
    ).read_text()
    for token in (
        "sqlite3",
        "import requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "create_order",
        "/home/james",
    ):
        assert token not in source
