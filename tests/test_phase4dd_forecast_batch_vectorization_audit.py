import json
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

from scripts.local.phase4dd_forecast_batch_vectorization_audit import (
    INPUT_SCHEMA,
    build_report,
    publish,
)


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def fixture(batch_size=2):
    return signed(
        {
            "schema": INPUT_SCHEMA,
            "batch_size": batch_size,
            "rows": [
                {"row_id": "a", "probability": "0", "market_price": "1", "weight": "1"},
                {"row_id": "b", "probability": "1", "market_price": "0", "weight": "0.1"},
                {
                    "row_id": "c",
                    "probability": "0.3333333333333333333333333333",
                    "market_price": "0.3",
                    "weight": "3",
                },
                {"row_id": "d", "probability": "0.5", "market_price": "0.5", "weight": "9"},
                {"row_id": "e", "probability": "0.75", "market_price": "0.25", "weight": "2"},
            ],
        }
    )


@pytest.mark.parametrize("batch_size", [1, 2, 4, 5, 10])
def test_vectorized_semantics_equal_scalar_for_batch_boundaries(batch_size):
    report = build_report(fixture(batch_size))
    assert report["semantic_equivalence"] is True
    assert report["scalar_results_hash"] == report["vectorized_results_hash"]
    assert [row["row_id"] for row in report["results"]] == list("abcde")
    assert report["results"][0]["score"] == "-1"
    assert report["results"][3]["score"] == "0"


def test_partial_final_batch_has_exact_offsets():
    report = build_report(fixture(2))
    assert report["batch_boundaries"][-1] == {
        "batch_index": 2,
        "start_offset": 4,
        "end_offset_exclusive": 5,
        "first_row_id": "e",
        "last_row_id": "e",
    }


def test_decimal_precision_is_not_binary_float():
    report = build_report(fixture())
    assert report["results"][2]["score"] == "0.0999999999999999999999999999"


@pytest.mark.parametrize(
    "field,value",
    [("probability", "-0.1"), ("probability", "1.1"), ("market_price", "NaN")],
)
def test_invalid_boundaries_fail_closed(field, value):
    payload = fixture()
    payload["rows"][0][field] = value
    signed(payload)
    with pytest.raises(ValueError, match="BOUNDARY|DECIMAL"):
        build_report(payload)


@pytest.mark.parametrize("size", [0, -1, 10001, True])
def test_invalid_batch_size_fails_closed(size):
    payload = fixture()
    payload["batch_size"] = size
    signed(payload)
    with pytest.raises(ValueError, match="BATCH_SIZE"):
        build_report(payload)


def test_duplicate_row_id_fails_closed():
    payload = fixture()
    payload["rows"][1]["row_id"] = "a"
    signed(payload)
    with pytest.raises(ValueError, match="ROW_ID"):
        build_report(payload)


def test_tampering_fails_closed():
    payload = fixture()
    payload["rows"][0]["weight"] = "99"
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        build_report(payload)


def test_deterministic_report():
    assert build_report(fixture()) == build_report(fixture())


def test_atomic_publication(tmp_path):
    output = tmp_path / "nested" / "report.json"
    report = build_report(fixture())
    publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(output.parent.glob(".*"))


def test_report_is_synthetic_and_non_executable():
    report = build_report(fixture())
    assert report["synthetic_benchmark_only"] is True
    assert report["forecast_records_created"] == 0
    assert report["execution_authorized"] is False


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4dd_forecast_batch_vectorization_audit.py"
    ).read_text()
    for token in (
        "sqlite3",
        "requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "create_forecast",
        "create_order",
        "/home/james",
    ):
        assert token not in source
