import importlib.util
import json
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

S = Path(__file__).parents[1] / "scripts/local/phase4fj_final_readiness_audit.py"
X = importlib.util.spec_from_file_location("m", S)
assert X and X.loader
M = importlib.util.module_from_spec(X)
X.loader.exec_module(M)


def seal(v):
    v.pop("artifact_hash", None)
    v["artifact_hash"] = canonical_hash(v)
    return v


def payload():
    return seal(
        {
            "schema": M.SCHEMA,
            "baseline_hash": "a" * 64,
            "environment_hash": "b" * 64,
            "metrics": [
                {
                    "metric": m,
                    "baseline_ms": 100,
                    "final_ms": 80 if i < 6 else 100,
                    "evidence_kind": "MEASURED" if i < 6 else "PROPOSED",
                    "evidence_hash": f"{i + 1:064x}",
                }
                for i, m in enumerate(M.METRICS)
            ],
        }
    )


def test_measured_and_proposed_are_separated():
    r = M.build_report(payload())
    assert (
        r["readiness_passed"]
        and len(r["measured_improvements"]) == 6
        and len(r["proposals"]) == 2
        and not r["proposal_counted_as_gain"]
    )


@pytest.mark.parametrize("metric", M.METRICS)
def test_every_metric_required(metric):
    v = payload()
    v["metrics"] = [x for x in v["metrics"] if x["metric"] != metric]
    seal(v)
    with pytest.raises(ValueError, match="METRICS"):
        M.build_report(v)


def test_measured_regression_blocks():
    v = payload()
    v["metrics"][0]["final_ms"] = 101
    seal(v)
    r = M.build_report(v)
    assert not r["readiness_passed"] and r["measured_regressions"]


def test_proposed_improvement_never_claimed():
    v = payload()
    x = v["metrics"][-1]
    x["final_ms"] = 1
    seal(v)
    r = M.build_report(v)
    row = next(x for x in r["metrics"] if x["metric"] == M.METRICS[-1])
    assert not row["gain_claimed"]


def test_boundary_equal_is_not_regression():
    v = payload()
    v["metrics"][0]["final_ms"] = 100
    seal(v)
    assert M.build_report(v)["readiness_passed"]


def test_tamper():
    v = payload()
    v["metrics"][0]["final_ms"] = 0
    with pytest.raises(ValueError, match="HASH"):
        M.build_report(v)


@pytest.mark.parametrize(
    "field",
    [
        "proposal_counted_as_gain",
        "production_database_mutated",
        "services_controlled",
        "exchange_requests_made",
        "execution_authorized",
    ],
)
def test_safe(field):
    assert M.build_report(payload())[field] is False


def test_atomic(tmp_path):
    p = tmp_path / "x"
    r = M.build_report(payload())
    M.publish(p, r)
    assert json.loads(p.read_text()) == r
