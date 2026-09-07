import json
from copy import deepcopy
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

from scripts.local.phase4dr_settlement_evaluation_incrementality import (
    INPUT_SCHEMA,
    build_report,
    publish,
)


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def record(identifier, market, probability, outcome):
    return {
        "forecast_id": identifier,
        "market_id": market,
        "probability": probability,
        "outcome": outcome,
    }


def fixture(updates=None):
    return signed(
        {
            "schema": INPUT_SCHEMA,
            "settlements": [
                record("a", "m1", "0.8", 1),
                record("b", "m1", "0.4", 0),
                record("c", "m2", "0.5", 1),
            ],
            "previous_metrics": {
                "m1": {"settlement_count": 2, "brier_sum": "0.2", "correct_count": 2},
                "m2": {"settlement_count": 1, "brier_sum": "0.25", "correct_count": 1},
            },
            "updates": updates
            or [{"operation": "UPSERT", "forecast_id": "a", "record": record("a", "m1", "0.9", 1)}],
        }
    )


def test_only_affected_market_recomputed_with_full_equivalence():
    report = build_report(fixture())
    assert report["affected_market_ids"] == ["m1"]
    assert report["reused_market_ids"] == ["m2"]
    assert report["full_equivalence"] is True
    assert report["incremental_metrics_hash"] == report["full_metrics_hash"]
    assert report["metrics"]["m1"]["brier_sum"] == "0.17"


def test_insert_delete_and_market_move_affect_exact_markets():
    report = build_report(
        fixture(
            [
                {"operation": "DELETE", "forecast_id": "b", "record": None},
                {"operation": "UPSERT", "forecast_id": "c", "record": record("c", "m3", "0.5", 1)},
                {"operation": "UPSERT", "forecast_id": "d", "record": record("d", "m4", "0.1", 0)},
            ]
        )
    )
    assert report["affected_market_ids"] == ["m1", "m2", "m3", "m4"]
    assert "m2" not in report["metrics"]


def test_exact_probability_and_outcome_boundaries():
    report = build_report(
        fixture([{"operation": "UPSERT", "forecast_id": "a", "record": record("a", "m1", "1", 1)}])
    )
    assert report["metrics"]["m1"]["brier_sum"] == "0.16"


def test_stale_previous_metrics_fail_closed():
    payload = fixture()
    payload["previous_metrics"]["m1"]["brier_sum"] = "999"
    signed(payload)
    with pytest.raises(ValueError, match="PREVIOUS_METRICS_MISMATCH"):
        build_report(payload)


@pytest.mark.parametrize("probability", ["-0.1", "1.1", "NaN", " 0.5"])
def test_invalid_probability_fails_closed(probability):
    with pytest.raises(ValueError, match="PROBABILITY"):
        build_report(
            fixture(
                [
                    {
                        "operation": "UPSERT",
                        "forecast_id": "a",
                        "record": record("a", "m1", probability, 1),
                    }
                ]
            )
        )


@pytest.mark.parametrize("outcome", [-1, 2, True, "1"])
def test_invalid_outcome_fails_closed(outcome):
    with pytest.raises(ValueError, match="OUTCOME"):
        build_report(
            fixture(
                [
                    {
                        "operation": "UPSERT",
                        "forecast_id": "a",
                        "record": record("a", "m1", "0.5", outcome),
                    }
                ]
            )
        )


def test_duplicate_update_and_missing_delete_fail_closed():
    update = {"operation": "DELETE", "forecast_id": "a", "record": None}
    with pytest.raises(ValueError, match="UPDATE_ID"):
        build_report(fixture([update, update]))
    with pytest.raises(ValueError, match="DELETE_INVALID"):
        build_report(fixture([{"operation": "DELETE", "forecast_id": "z", "record": None}]))


def test_cannot_delete_all_settlements():
    payload = signed(
        {
            "schema": INPUT_SCHEMA,
            "settlements": [record("a", "m1", "0.5", 1)],
            "previous_metrics": {
                "m1": {"settlement_count": 1, "brier_sum": "0.25", "correct_count": 1}
            },
            "updates": [{"operation": "DELETE", "forecast_id": "a", "record": None}],
        }
    )
    with pytest.raises(ValueError, match="EMPTY_EVALUATION"):
        build_report(payload)


def test_tampering_fails_closed():
    payload = fixture()
    payload["updates"][0]["record"]["probability"] = "0.1"
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
    assert report["evaluation_records_created"] == 0
    assert report["execution_authorized"] is False


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4dr_settlement_evaluation_incrementality.py"
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
