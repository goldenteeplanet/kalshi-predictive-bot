from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4cv_critical_market_priority.py"
    spec = importlib.util.spec_from_file_location("phase4cv_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _market(ticker, evaluation_at, ready=True, fresh=True):
    return {
        "ticker": ticker,
        "evaluation_at": evaluation_at,
        "evidence_ready": ready,
        "data_fresh": fresh,
    }


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "evaluated_at": "2026-08-26T00:00:00Z",
        "critical_window_ms": 60_000,
        "markets": [
            _market("B-CRITICAL", "2026-08-26T00:00:30Z"),
            _market("A-CRITICAL", "2026-08-26T00:00:30Z"),
            _market("BOUNDARY", "2026-08-26T00:01:00Z"),
            _market("UPCOMING", "2026-08-26T00:01:00.001000Z"),
            _market("BLOCKED", "2026-08-26T00:00:01Z", ready=False),
            _market("EXPIRED", "2026-08-25T23:59:59.999000Z"),
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_priority_tiers_boundaries_and_ties_are_deterministic():
    module = _module()
    report = module.build_report(_payload(module))
    rows = report["markets"]
    assert [row["ticker"] for row in rows] == [
        "A-CRITICAL",
        "B-CRITICAL",
        "BOUNDARY",
        "UPCOMING",
        "BLOCKED",
        "EXPIRED",
    ]
    assert [row["tier"] for row in rows] == [
        "CRITICAL",
        "CRITICAL",
        "CRITICAL",
        "UPCOMING",
        "BLOCKED",
        "EXPIRED",
    ]
    assert rows[2]["time_to_evaluation_ms"] == 60_000
    assert rows[3]["time_to_evaluation_ms"] == 60_001
    assert [row["priority_rank"] for row in rows] == list(range(1, 7))


def test_stale_data_is_blocked_even_when_evidence_is_ready():
    module = _module()
    payload = _payload(module)
    payload["markets"][0]["data_fresh"] = False
    _rehash(module, payload)
    by_ticker = {row["ticker"]: row for row in module.build_report(payload)["markets"]}
    assert by_ticker["B-CRITICAL"]["tier"] == "BLOCKED"


def test_output_has_no_trading_instruction_surface():
    module = _module()
    report = module.build_report(_payload(module))
    assert report["trading_fields_emitted"] == []
    assert report["orders_created"] == 0
    assert report["execution_authorized"] is False
    forbidden = {"side", "price", "size", "quantity", "order_type"}
    assert all(not forbidden.intersection(row) for row in report["markets"])


@pytest.mark.parametrize("kind", ["empty", "fields", "duplicate", "ticker", "ready", "timestamp"])
def test_malformed_market_evidence_fails_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "empty":
        payload["markets"] = []
    elif kind == "fields":
        payload["markets"][0]["extra"] = True
    elif kind == "duplicate":
        payload["markets"][1]["ticker"] = "B-CRITICAL"
    elif kind == "ticker":
        payload["markets"][0]["ticker"] = ""
    elif kind == "ready":
        payload["markets"][0]["evidence_ready"] = 1
    else:
        payload["markets"][0]["evaluation_at"] = "tomorrow"
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


@pytest.mark.parametrize("window", [-1, True])
def test_invalid_window_fails_closed(window):
    module = _module()
    payload = _payload(module)
    payload["critical_window_ms"] = window
    _rehash(module, payload)
    with pytest.raises(ValueError, match="WINDOW"):
        module.build_report(payload)


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["markets"][0]["evaluation_at"] = "2026-08-26T00:00:01Z"
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "priority.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4cv_critical_market_priority.py"
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
