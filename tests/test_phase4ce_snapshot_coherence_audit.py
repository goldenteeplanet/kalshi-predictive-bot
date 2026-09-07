from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ce_snapshot_coherence_audit.py"
    spec = importlib.util.spec_from_file_location("phase4ce_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


START = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _payload(module, *, skew_ms=1_000, window_ms=1_000):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "groups": [{"group_id": "weather", "market_ids": ["A", "B"], "window_ms": window_ms}],
        "snapshots": [
            {"market_id": "A", "captured_at": START.isoformat()},
            {
                "market_id": "B",
                "captured_at": (START + timedelta(milliseconds=skew_ms)).isoformat(),
            },
            {"market_id": "C", "captured_at": START.isoformat()},
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_exact_boundary_is_coherent_deterministic_and_non_authorizing():
    module = _module()
    report = module.build_audit(_payload(module))
    assert report == module.build_audit(_payload(module))
    assert report["groups"][0]["observed_skew_ms"] == 1_000
    assert report["groups"][0]["coherent"] is True
    assert report["groups"][0]["offline_evaluation_eligible"] is True
    assert report["ungrouped_market_ids"] == ["C"]
    assert report["execution_authorized"] is False


def test_one_millisecond_over_window_is_incoherent_and_ineligible():
    module = _module()
    report = module.build_audit(_payload(module, skew_ms=1_001))
    assert report["all_groups_coherent"] is False
    assert report["incoherent_group_ids"] == ["weather"]
    assert report["groups"][0]["offline_evaluation_eligible"] is False


def test_timezone_offsets_normalize_to_utc():
    module = _module()
    payload = _payload(module, skew_ms=0, window_ms=0)
    payload["snapshots"][1]["captured_at"] = "2026-08-26T07:00:00-05:00"
    _rehash(module, payload)
    assert module.build_audit(payload)["groups"][0]["coherent"] is True


@pytest.mark.parametrize(
    "kind",
    (
        "snapshot_fields",
        "duplicate",
        "timestamp",
        "group_fields",
        "group_order",
        "missing",
        "window",
    ),
)
def test_malformed_evidence_fails_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "snapshot_fields":
        payload["snapshots"][0]["extra"] = True
    elif kind == "duplicate":
        payload["snapshots"][1]["market_id"] = "A"
    elif kind == "timestamp":
        payload["snapshots"][0]["captured_at"] = "2026-08-26T12:00:00"
    elif kind == "group_fields":
        payload["groups"][0]["extra"] = True
    elif kind == "group_order":
        payload["groups"][0]["market_ids"] = ["B", "A"]
    elif kind == "missing":
        payload["groups"][0]["market_ids"] = ["A", "Z"]
    else:
        payload["groups"][0]["window_ms"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_audit(payload)


def test_outer_tampering_fails_closed():
    module = _module()
    payload = _payload(module)
    payload["extra"] = True
    with pytest.raises(ValueError, match="HASH"):
        module.build_audit(payload)


def test_atomic_publication_round_trip(tmp_path: Path):
    module = _module()
    report = module.build_audit(_payload(module))
    output = tmp_path / "audit.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_database_service_network_or_exchange_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4ce_snapshot_coherence_audit.py"
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
