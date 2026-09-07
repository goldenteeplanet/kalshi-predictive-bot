import json
from copy import deepcopy
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

from scripts.local.phase4ds_forecast_ranking_parallelism_model import (
    INPUT_SCHEMA,
    build_report,
    publish,
)

H1, H2, H3, H4 = "1" * 64, "2" * 64, "3" * 64, "4" * 64


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def task(identifier, dependencies, digest, work=1):
    return {
        "task_id": identifier,
        "dependencies": dependencies,
        "result_hash": digest,
        "work_units": work,
    }


def fixture(tasks=None):
    return signed(
        {
            "schema": INPUT_SCHEMA,
            "tasks": tasks
            or [
                task("forecast-a", [], H1, 2),
                task("forecast-b", [], H2, 3),
                task("ranking", ["forecast-a", "forecast-b"], H3, 4),
                task("summary", ["ranking"], H4, 1),
            ],
        }
    )


def test_independent_branches_share_stage_and_join_deterministically():
    report = build_report(fixture())
    assert report["stages"][0]["task_ids"] == ["forecast-a", "forecast-b"]
    assert report["maximum_parallel_width"] == 2
    assert report["parallel_join_hash"] == report["sequential_join_hash"]
    assert report["parallel_execution_enabled"] is False


def test_input_order_does_not_change_stages_or_join():
    payload = fixture()
    reversed_payload = fixture(list(reversed(payload["tasks"])))
    left, right = build_report(payload), build_report(reversed_payload)
    assert left["stages"] == right["stages"]
    assert left["canonical_join"] == right["canonical_join"]


def test_diamond_dependencies_have_expected_levels():
    tasks = [
        task("a", [], H1),
        task("b", ["a"], H2),
        task("c", ["a"], H3),
        task("d", ["b", "c"], H4),
    ]
    assert [stage["task_ids"] for stage in build_report(fixture(tasks))["stages"]] == [
        ["a"],
        ["b", "c"],
        ["d"],
    ]


def test_cycle_and_missing_dependency_fail_closed():
    with pytest.raises(ValueError, match="CYCLE"):
        build_report(fixture([task("a", ["b"], H1), task("b", ["a"], H2)]))
    with pytest.raises(ValueError, match="MISSING"):
        build_report(fixture([task("a", ["missing"], H1)]))


def test_duplicate_task_or_dependency_fails_closed():
    with pytest.raises(ValueError, match="TASK_ID"):
        build_report(fixture([task("a", [], H1), task("a", [], H2)]))
    with pytest.raises(ValueError, match="DEPENDENCIES"):
        build_report(fixture([task("a", [], H1), task("b", ["a", "a"], H2)]))


@pytest.mark.parametrize("work", [0, -1, True])
def test_invalid_work_units_fail_closed(work):
    with pytest.raises(ValueError, match="WORK_UNITS"):
        build_report(fixture([task("a", [], H1, work)]))


def test_tampering_fails_closed():
    payload = fixture()
    payload["tasks"][0]["work_units"] = 99
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


def test_non_executable():
    report = build_report(fixture())
    assert report["parallel_execution_enabled"] is False
    assert report["task_records_created"] == 0
    assert report["execution_authorized"] is False


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4ds_forecast_ranking_parallelism_model.py"
    ).read_text()
    for token in (
        "sqlite3",
        "import requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "create_order",
        "/home/james",
    ):
        assert token not in source
