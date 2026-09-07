import json
from copy import deepcopy
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

from scripts.local.phase4dt_bounded_local_parallel_executor import (
    INPUT_SCHEMA,
    build_report,
    publish,
)


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def fixture(workers=4, tasks=None):
    return signed(
        {
            "schema": INPUT_SCHEMA,
            "synthetic_only": True,
            "worker_count": workers,
            "tasks": tasks
            or [
                {"task_id": "b", "operation": "SUM", "values": ["0.1", "0.2"]},
                {"task_id": "a", "operation": "SUM_OF_SQUARES", "values": ["2", "3"]},
                {"task_id": "c", "operation": "SUM", "values": ["-1", "1"]},
            ],
        }
    )


@pytest.mark.parametrize("workers", [1, 2, 4, 8])
def test_parallel_results_equal_sequential_with_stable_order(workers):
    report = build_report(fixture(workers))
    assert report["deterministic_equivalence"] is True
    assert report["parallel_results_hash"] == report["sequential_results_hash"]
    assert report["results"] == [
        {"task_id": "a", "result": "13"},
        {"task_id": "b", "result": "0.3"},
        {"task_id": "c", "result": "0"},
    ]


def test_input_order_does_not_change_results():
    payload = fixture()
    reversed_payload = fixture(tasks=list(reversed(payload["tasks"])))
    assert build_report(payload)["results"] == build_report(reversed_payload)["results"]


@pytest.mark.parametrize("workers", [0, 9, True])
def test_worker_limit_fails_closed(workers):
    with pytest.raises(ValueError, match="WORKER_COUNT"):
        build_report(fixture(workers))


def test_synthetic_flag_is_mandatory():
    payload = fixture()
    payload["synthetic_only"] = False
    signed(payload)
    with pytest.raises(ValueError, match="SYNTHETIC_ONLY"):
        build_report(payload)


def test_task_and_total_work_limits_fail_before_execution():
    too_many_values = [{"task_id": "a", "operation": "SUM", "values": ["1"] * 1001}]
    with pytest.raises(ValueError, match="TASK_VALUES"):
        build_report(fixture(tasks=too_many_values))
    tasks = [
        {"task_id": f"t{index:03d}", "operation": "SUM", "values": ["1"] * 101}
        for index in range(991)
    ]
    with pytest.raises(ValueError, match="TOTAL_WORK"):
        build_report(fixture(tasks=tasks))


def test_duplicate_task_and_unknown_operation_fail_closed():
    task = {"task_id": "a", "operation": "SUM", "values": ["1"]}
    with pytest.raises(ValueError, match="TASK_ID"):
        build_report(fixture(tasks=[task, task]))
    with pytest.raises(ValueError, match="OPERATION"):
        build_report(fixture(tasks=[{"task_id": "a", "operation": "EXEC", "values": ["1"]}]))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "", " 1"])
def test_invalid_decimal_fails_closed(value):
    with pytest.raises(ValueError, match="DECIMAL"):
        build_report(fixture(tasks=[{"task_id": "a", "operation": "SUM", "values": [value]}]))


def test_tampering_fails_closed():
    payload = fixture()
    payload["worker_count"] = 1
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        build_report(payload)


def test_deterministic_and_nonmutating():
    payload = fixture()
    before = deepcopy(payload)
    assert build_report(payload) == build_report(payload)
    assert payload == before


def test_atomic_publication(tmp_path):
    output = tmp_path / "nested" / "report.json"
    report = build_report(fixture())
    publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(output.parent.glob(".*"))


def test_non_executable_outside_fixed_synthetic_tasks():
    report = build_report(fixture())
    assert report["external_processes_launched"] == 0
    assert report["task_records_created"] == 0
    assert report["execution_authorized"] is False


def test_source_has_no_connected_or_process_launch_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4dt_bounded_local_parallel_executor.py"
    ).read_text()
    for token in (
        "sqlite3",
        "import requests",
        "subprocess",
        "ProcessPool",
        "systemctl",
        "exchange_client",
        "create_order",
        "/home/james",
    ):
        assert token not in source
