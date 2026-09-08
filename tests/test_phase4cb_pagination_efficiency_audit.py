from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4cb_pagination_efficiency_audit.py"
    spec = importlib.util.spec_from_file_location("phase4cb_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture(module):
    capture = {
        "schema": module.INPUT_SCHEMA,
        "page_size": 4,
        "pages": [
            {
                "page_number": 1,
                "request_cursor": None,
                "next_cursor": "c1",
                "request_attempts": 1,
                "records": ["A", "B", "C", "D"],
                "server_has_more": True,
            },
            {
                "page_number": 2,
                "request_cursor": "c1",
                "next_cursor": "c2",
                "request_attempts": 3,
                "records": ["D", "E"],
                "server_has_more": True,
            },
            {
                "page_number": 3,
                "request_cursor": "c2",
                "next_cursor": None,
                "request_attempts": 1,
                "records": [],
                "server_has_more": False,
            },
        ],
    }
    capture["artifact_hash"] = module._hash(capture)
    return capture


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_metrics_are_deterministic_and_complete():
    module = _module()
    report = module.build_audit(_capture(module))
    assert report == module.build_audit(_capture(module))
    assert report["page_count"] == 3
    assert report["record_count"] == 6
    assert report["unique_record_count"] == 5
    assert report["duplicate_record_count"] == 1
    assert report["empty_page_count"] == 1
    assert report["request_attempt_count"] == 5
    assert report["retry_amplification_ppm"] == 666_666
    assert report["overall_page_utilization_ppm"] == 500_000
    assert report["stop_condition_correct"] is True
    assert report["execution_authorized"] is False


@pytest.mark.parametrize("kind", ("mismatch", "premature", "after_terminal"))
def test_stop_condition_violations_are_reported(kind: str):
    module = _module()
    capture = _capture(module)
    if kind == "mismatch":
        capture["pages"][-1]["next_cursor"] = "unexpected"
    elif kind == "premature":
        capture["pages"][-1]["next_cursor"] = "more"
        capture["pages"][-1]["server_has_more"] = True
    else:
        capture["pages"][1]["next_cursor"] = None
        capture["pages"][2]["request_cursor"] = None
    _rehash(module, capture)
    report = module.build_audit(capture)
    assert report["stop_condition_correct"] is False
    assert report["stop_violations"]


@pytest.mark.parametrize(
    "kind",
    ("page_size", "page_number", "cursor", "attempts", "records", "record_id", "fields"),
)
def test_malformed_capture_fails_closed(kind: str):
    module = _module()
    capture = _capture(module)
    if kind == "page_size":
        capture["page_size"] = True
    elif kind == "page_number":
        capture["pages"][0]["page_number"] = 2
    elif kind == "cursor":
        capture["pages"][1]["request_cursor"] = "wrong"
    elif kind == "attempts":
        capture["pages"][0]["request_attempts"] = 0
    elif kind == "records":
        capture["pages"][0]["records"].append("E")
    elif kind == "record_id":
        capture["pages"][0]["records"][0] = ""
    else:
        capture["pages"][0]["extra"] = True
    _rehash(module, capture)
    with pytest.raises(ValueError):
        module.build_audit(capture)


def test_outer_tampering_fails_closed():
    module = _module()
    capture = _capture(module)
    capture["extra"] = True
    with pytest.raises(ValueError, match="HASH"):
        module.build_audit(capture)


def test_atomic_publication_round_trip(tmp_path: Path):
    module = _module()
    report = module.build_audit(_capture(module))
    output = tmp_path / "audit.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_database_service_network_or_exchange_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4cb_pagination_efficiency_audit.py"
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
