from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4cg_adaptive_concurrency_proposal.py"
    spec = importlib.util.spec_from_file_location("phase4cg_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _evidence(module, *, current=4, samples=100, p95=99, throttle=0, timeout=0):
    evidence = {
        "schema": module.INPUT_SCHEMA,
        "current_concurrency": current,
        "target_p95_ms": 100,
        "windows": [
            {
                "window": 1,
                "sample_count": samples,
                "p95_ms": p95,
                "throttle_ppm": throttle,
                "timeout_ppm": timeout,
            }
        ],
    }
    evidence["artifact_hash"] = module._hash(evidence)
    return evidence


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_low_error_evidence_proposes_only_one_step_increase():
    module = _module()
    report = module.build_proposal(_evidence(module))
    assert report == module.build_proposal(_evidence(module))
    assert report["proposed_concurrency"] == 5
    assert report["decision"] == "INCREASE_ONE_STEP"
    assert report["live_setting_changes_applied"] == 0
    assert report["execution_authorized"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    (("throttle", 10_000), ("timeout", 20_000)),
)
def test_degradation_boundary_immediately_proposes_decrease(field: str, value: int):
    module = _module()
    kwargs = {field: value}
    report = module.build_proposal(_evidence(module, **kwargs))
    assert report["proposed_concurrency"] == 2
    assert report["decision"] == "DECREASE_FOR_DEGRADATION"


def test_insufficient_evidence_holds():
    module = _module()
    report = module.build_proposal(_evidence(module, samples=99))
    assert report["proposed_concurrency"] == 4
    assert report["decision"] == "HOLD_INSUFFICIENT_EVIDENCE"


def test_latency_at_target_holds_and_maximum_does_not_exceed_bound():
    module = _module()
    assert module.build_proposal(_evidence(module, p95=100))["decision"] == "HOLD_WITHIN_GUARDRAILS"
    report = module.build_proposal(_evidence(module, current=module.MAX_CONCURRENCY))
    assert report["proposed_concurrency"] == module.MAX_CONCURRENCY


@pytest.mark.parametrize(
    "kind", ("current", "target", "windows", "order", "value", "ppm", "fields")
)
def test_malformed_evidence_fails_closed(kind: str):
    module = _module()
    evidence = _evidence(module)
    if kind == "current":
        evidence["current_concurrency"] = True
    elif kind == "target":
        evidence["target_p95_ms"] = 0
    elif kind == "windows":
        evidence["windows"] = []
    elif kind == "order":
        evidence["windows"][0]["window"] = 2
    elif kind == "value":
        evidence["windows"][0]["p95_ms"] = -1
    elif kind == "ppm":
        evidence["windows"][0]["timeout_ppm"] = 1_000_001
    else:
        evidence["windows"][0]["extra"] = True
    _rehash(module, evidence)
    with pytest.raises(ValueError):
        module.build_proposal(evidence)


def test_outer_tampering_fails_closed():
    module = _module()
    evidence = _evidence(module)
    evidence["extra"] = True
    with pytest.raises(ValueError, match="HASH"):
        module.build_proposal(evidence)


def test_atomic_publication_round_trip(tmp_path: Path):
    module = _module()
    report = module.build_proposal(_evidence(module))
    output = tmp_path / "proposal.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_database_service_network_or_exchange_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4cg_adaptive_concurrency_proposal.py"
    ).read_text()
    for token in (
        "sqlite3",
        "subprocess",
        "requests",
        "systemctl",
        "exchange_client",
        "/home/james",
    ):
        assert token not in source
