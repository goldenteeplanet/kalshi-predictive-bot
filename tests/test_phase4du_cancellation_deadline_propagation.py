import json
from copy import deepcopy
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

from scripts.local.phase4du_cancellation_deadline_propagation import (
    INPUT_SCHEMA,
    build_report,
    publish,
)

H1, H2, H3 = "1" * 64, "2" * 64, "3" * 64


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def task(
    identifier,
    dependencies=None,
    started="2026-08-26T00:00:00Z",
    finished="2026-08-26T00:00:01Z",
    deadline="2026-08-26T00:00:01Z",
    complete=True,
    digest=H1,
):
    return {
        "task_id": identifier,
        "dependencies": dependencies or [],
        "started_at": started,
        "finished_at": finished,
        "deadline": deadline,
        "complete": complete,
        "result_hash": digest if complete else None,
    }


def fixture(tasks=None):
    return signed(
        {
            "schema": INPUT_SCHEMA,
            "evaluated_at": "2026-08-26T00:01:00Z",
            "tasks": tasks or [task("a"), task("b", ["a"], digest=H2)],
        }
    )


def test_finish_at_exact_deadline_is_publishable():
    report = build_report(fixture())
    assert [row["task_id"] for row in report["publishable_results"]] == ["a", "b"]
    assert report["partial_or_late_results_published"] == 0


def test_one_microsecond_late_cancels_and_propagates():
    tasks = [
        task("a", finished="2026-08-26T00:00:01.000001Z"),
        task("b", ["a"], digest=H2),
        task("independent", digest=H3),
    ]
    report = build_report(fixture(tasks))
    assert report["cancelled_task_ids"] == ["a", "b"]
    assert report["publishable_results"] == [{"task_id": "independent", "result_hash": H3}]
    assert "DEPENDENCY_CANCELLED_OR_UNPUBLISHABLE" in report["task_decisions"][1]["reasons"]


def test_start_at_deadline_is_cancelled():
    item = task("a", started="2026-08-26T00:00:01Z")
    decision = build_report(fixture([item]))["task_decisions"][0]
    assert decision["reasons"] == ["EXPIRED_BEFORE_OR_AT_START"]


def test_incomplete_result_never_publishes():
    decision = build_report(fixture([task("a", complete=False)]))["task_decisions"][0]
    assert decision["publishable"] is False
    assert decision["result_hash"] is None
    assert decision["reasons"] == ["INCOMPLETE_RESULT"]


def test_partial_result_hash_fails_closed():
    item = task("a", complete=False)
    item["result_hash"] = H1
    with pytest.raises(ValueError, match="PARTIAL_RESULT_HASH"):
        build_report(fixture([item]))


def test_cycle_missing_dependency_and_duplicate_edges_fail_closed():
    with pytest.raises(ValueError, match="CYCLE"):
        build_report(fixture([task("a", ["b"]), task("b", ["a"])]))
    with pytest.raises(ValueError, match="MISSING"):
        build_report(fixture([task("a", ["missing"])]))
    with pytest.raises(ValueError, match="DEPENDENCIES"):
        build_report(fixture([task("a"), task("b", ["a", "a"])]))


def test_impossible_timeline_fails_closed():
    with pytest.raises(ValueError, match="TIMELINE"):
        build_report(fixture([task("a", started="2026-08-26T00:00:02Z")]))
    with pytest.raises(ValueError, match="TIMELINE"):
        build_report(fixture([task("a", finished="2026-08-26T00:02:00Z")]))


def test_input_order_does_not_change_decisions():
    payload = fixture()
    reverse = fixture(list(reversed(payload["tasks"])))
    assert build_report(payload)["task_decisions"] == build_report(reverse)["task_decisions"]


def test_tampering_fails_closed():
    payload = fixture()
    payload["tasks"][0]["complete"] = False
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


def test_non_executable_and_local_only():
    report = build_report(fixture())
    assert report["local_cancellation_only"] is True
    assert report["task_records_created"] == 0
    assert report["execution_authorized"] is False


def test_source_has_no_connected_or_process_control_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4du_cancellation_deadline_propagation.py"
    ).read_text()
    for token in (
        "sqlite3",
        "import requests",
        "subprocess",
        "ThreadPool",
        "systemctl",
        "exchange_client",
        "create_order",
        "/home/james",
    ):
        assert token not in source
