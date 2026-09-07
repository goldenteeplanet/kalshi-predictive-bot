from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4cn_crypto_quote_latency_audit.py"
    spec = importlib.util.spec_from_file_location("phase4cn_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _quote(identifier, source, symbol="BTCUSD", captured="2026-08-26T00:00:00Z"):
    return {
        "quote_id": identifier,
        "source": source,
        "symbol": symbol,
        "available": True,
        "captured_at": captured,
        "bid": "100.0",
        "ask": "102.0",
    }


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "evaluated_at": "2026-08-26T00:00:01Z",
        "expected_symbols": ["BTCUSD", "ETHUSD"],
        "max_quote_age_ms": 1000,
        "max_source_skew_ms": 100,
        "midpoint_tolerance": "0.5",
        "quotes": [_quote("btc-a", "A"), _quote("btc-b", "B")],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_fresh_aligned_quotes_pass_and_missing_symbol_is_explicit():
    module = _module()
    report = module.build_report(_payload(module))
    by_symbol = {row["symbol"]: row for row in report["symbols"]}
    assert by_symbol["BTCUSD"]["status"] == "PASS"
    assert by_symbol["BTCUSD"]["source_skew_ms"] == 0
    assert by_symbol["ETHUSD"]["status"] == "MISSING_SYMBOL"
    assert report["network_calls_performed"] == 0


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("stale", "STALE_SYMBOL"),
        ("skew", "TEMPORALLY_MISALIGNED"),
        ("spread", "PRICE_DIVERGENT"),
        ("crossed", "INCOHERENT_QUOTE"),
    ],
)
def test_failure_modes_are_distinct(kind: str, expected: str):
    module = _module()
    payload = _payload(module)
    if kind == "stale":
        payload["quotes"][0]["captured_at"] = "2026-08-25T23:59:59.999000Z"
    elif kind == "skew":
        payload["quotes"][1]["captured_at"] = "2026-08-26T00:00:00.101000Z"
    elif kind == "spread":
        payload["quotes"][1]["bid"] = "101.1"
        payload["quotes"][1]["ask"] = "103.1"
    else:
        payload["quotes"][0]["bid"] = "103"
    _rehash(module, payload)
    assert module.build_report(payload)["symbols"][0]["status"] == expected


def test_exact_age_skew_and_spread_boundaries_pass():
    module = _module()
    payload = _payload(module)
    payload["quotes"][1]["captured_at"] = "2026-08-26T00:00:00.100000Z"
    payload["quotes"][1]["bid"] = "100.5"
    payload["quotes"][1]["ask"] = "102.5"
    _rehash(module, payload)
    assert module.build_report(payload)["symbols"][0]["status"] == "PASS"


@pytest.mark.parametrize("kind", ["empty", "fields", "duplicate", "source", "symbol", "available"])
def test_malformed_quotes_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "empty":
        payload["quotes"] = []
    elif kind == "fields":
        payload["quotes"][0]["extra"] = True
    elif kind == "duplicate":
        payload["quotes"][1]["quote_id"] = "btc-a"
    elif kind == "source":
        payload["quotes"][0]["source"] = ""
    elif kind == "symbol":
        payload["quotes"][0]["symbol"] = "DOGEUSD"
    else:
        payload["quotes"][0]["available"] = 1
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


@pytest.mark.parametrize("kind", ["future", "negative", "nan", "expected"])
def test_invalid_temporal_numeric_and_symbol_inputs_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "future":
        payload["quotes"][0]["captured_at"] = "2026-08-26T00:00:02Z"
    elif kind == "negative":
        payload["quotes"][0]["bid"] = "-1"
    elif kind == "nan":
        payload["midpoint_tolerance"] = "NaN"
    else:
        payload["expected_symbols"] = ["BTCUSD", "BTCUSD"]
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["quotes"][0]["bid"] = "99"
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "crypto-audit.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_network_database_service_or_exchange_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4cn_crypto_quote_latency_audit.py"
    ).read_text()
    for token in (
        "requests",
        "httpx",
        "sqlite3",
        "subprocess",
        "systemctl",
        "exchange_client",
        "/home/james",
    ):
        assert token not in source
