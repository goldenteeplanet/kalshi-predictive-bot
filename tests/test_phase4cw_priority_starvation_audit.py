from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4cw_priority_starvation_audit.py"
    spec = importlib.util.spec_from_file_location("phase4cw_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    cycles = []
    for index in range(1, 7):
        served = ["HIGH", "LOW"] if index in {1, 4} else ["HIGH"]
        stale = ["LOW"] if index == 2 else []
        cycles.append(
            {
                "cycle": index,
                "served_markets": served,
                "stale_markets": stale,
                "surfaced_stale_markets": stale,
            }
        )
    payload = {
        "schema": module.INPUT_SCHEMA,
        "markets": ["HIGH", "LOW"],
        "max_wait_cycles": 3,
        "cycles": cycles,
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_service_at_exact_wait_boundary_proves_fairness():
    module = _module()
    report = module.build_report(_payload(module))
    assert report["status"] == "FAIRNESS_PROVEN"
    assert report["starved_market_count"] == 0
    by_ticker = {row["ticker"]: row for row in report["markets"]}
    assert by_ticker["LOW"]["served_cycles"] == [1, 4]
    assert by_ticker["LOW"]["violating_windows"] == []
    assert report["stale_visibility_violations"] == []


def test_one_cycle_beyond_wait_bound_detects_starvation():
    module = _module()
    payload = _payload(module)
    payload["cycles"][3]["served_markets"] = ["HIGH"]
    payload["cycles"][4]["served_markets"] = ["HIGH", "LOW"]
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["status"] == "REFUSE"
    low = {row["ticker"]: row for row in report["markets"]}["LOW"]
    assert low["status"] == "STARVED"
    assert {"start_cycle": 2, "end_cycle": 4} in low["violating_windows"]


def test_hidden_stale_state_refuses_even_when_service_is_fair():
    module = _module()
    payload = _payload(module)
    payload["cycles"][1]["surfaced_stale_markets"] = []
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["status"] == "REFUSE"
    assert report["starved_market_count"] == 0
    assert report["stale_visibility_violations"] == [{"cycle": 2, "ticker": "LOW"}]


def test_trace_shorter_than_bound_still_requires_each_market_once():
    module = _module()
    payload = _payload(module)
    payload["max_wait_cycles"] = 10
    for cycle in payload["cycles"]:
        cycle["served_markets"] = ["HIGH"]
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["audited_window_size"] == 6
    assert report["starved_market_count"] == 1


@pytest.mark.parametrize("kind", ["markets", "duplicate", "wait", "cycles", "fields", "sequence"])
def test_malformed_trace_envelopes_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "markets":
        payload["markets"] = []
    elif kind == "duplicate":
        payload["markets"] = ["HIGH", "HIGH"]
    elif kind == "wait":
        payload["max_wait_cycles"] = 0
    elif kind == "cycles":
        payload["cycles"] = []
    elif kind == "fields":
        payload["cycles"][0]["extra"] = True
    else:
        payload["cycles"][1]["cycle"] = 3
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


@pytest.mark.parametrize("field", ["served_markets", "stale_markets", "surfaced_stale_markets"])
def test_unknown_or_duplicate_cycle_markets_fail_closed(field: str):
    module = _module()
    payload = _payload(module)
    payload["cycles"][0][field] = ["UNKNOWN"]
    _rehash(module, payload)
    with pytest.raises(ValueError, match="CYCLE_MARKETS"):
        module.build_report(payload)


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["max_wait_cycles"] = 4
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "starvation.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4cw_priority_starvation_audit.py"
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
