from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module(filename: str, name: str):
    path = Path(__file__).parents[1] / "scripts/local" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _modules():
    return (
        _module("phase4bv_performance_fixtures.py", "phase4bv_for_by"),
        _module("phase4bw_read_only_profiler.py", "phase4bw_for_by"),
        _module("phase4by_safe_local_optimization.py", "phase4by_tested"),
    )


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_all_fixtures_are_byte_identical_and_non_authorizing():
    fixtures, _, optimizer = _modules()
    proof = optimizer.build_proof(fixtures.build_pack())
    assert [row["name"] for row in proof["comparisons"]] == list(optimizer.EXPECTED_NAMES)
    assert proof["all_outputs_byte_identical"] is True
    assert proof["refusal_order_changed"] is False
    assert proof["production_records_created"] == 0
    assert proof["execution_authorized"] is False


def test_optimized_profiler_matches_reference_for_every_fixture():
    fixtures, profiler, optimizer = _modules()
    for fixture in fixtures.build_pack()["fixtures"]:
        events = fixture["events"]
        assert profiler._workload(events) == optimizer.reference_workload(events)
        assert optimizer.optimized_workload(events) == optimizer.reference_workload(events)


def test_empty_and_all_inactive_inputs_are_equivalent():
    _, _, optimizer = _modules()
    inactive = [
        {
            "active": False,
            "market_key": "SYNTH-00",
            "sequence": 0,
            "observed_offset_us": 0,
            "value_ppm": 1,
        }
    ]
    for events in ([], inactive):
        assert optimizer.reference_workload(events) == optimizer.optimized_workload(events)


@pytest.mark.parametrize("kind", ("outer", "manifest", "link", "fixture", "order", "authority"))
def test_refusal_conditions_fail_closed_in_stable_validation_order(kind: str):
    fixtures, _, optimizer = _modules()
    pack = fixtures.build_pack()
    if kind == "outer":
        pack["extra"] = True
    elif kind == "manifest":
        pack["manifest"]["artifact_hash"] = "0" * 64
        _rehash(optimizer, pack)
    elif kind == "link":
        pack["manifest_hash"] = "0" * 64
        _rehash(optimizer, pack)
    elif kind == "fixture":
        pack["fixtures"][0]["artifact_hash"] = "0" * 64
        _rehash(optimizer, pack)
    elif kind == "order":
        pack["fixtures"].reverse()
        _rehash(optimizer, pack)
    else:
        pack["fixtures"][0]["execution_authorized"] = True
        _rehash(optimizer, pack["fixtures"][0])
        pack["manifest"]["fixtures"][0]["fixture_hash"] = pack["fixtures"][0]["artifact_hash"]
        _rehash(optimizer, pack["manifest"])
        pack["manifest_hash"] = pack["manifest"]["artifact_hash"]
        _rehash(optimizer, pack)
    with pytest.raises(ValueError):
        optimizer.build_proof(pack)


def test_divergence_fails_closed(monkeypatch):
    fixtures, _, optimizer = _modules()
    monkeypatch.setattr(optimizer, "optimized_workload", lambda events: "0" * 64)
    with pytest.raises(ValueError, match="DIVERGENCE"):
        optimizer.build_proof(fixtures.build_pack())


def test_atomic_publication_round_trip(tmp_path: Path):
    fixtures, _, optimizer = _modules()
    report = optimizer.build_proof(fixtures.build_pack())
    output = tmp_path / "proof.json"
    optimizer.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_database_service_network_or_exchange_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4by_safe_local_optimization.py"
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
