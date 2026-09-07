import json
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

from scripts.local.phase4de_forecast_determinism_matrix import INPUT_SCHEMA, build_report, publish


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def fixture():
    base = {"decimal": ["1.0", "1.1"]}
    contexts = []
    for repeat, process_count, reverse, locale, timezone, version in [
        (1, 1, False, "C", "UTC", "1.0"),
        (2, 4, True, "en_US.UTF-8", "America/Chicago", "1.0"),
        (3, 2, False, "C", "America/Chicago", "1.1"),
    ]:
        contexts.append(
            {
                "context_id": f"c{repeat}",
                "repeat": repeat,
                "process_count": process_count,
                "reverse_input": reverse,
                "locale": locale,
                "timezone": timezone,
                "dependencies": {"decimal": version},
            }
        )
    return signed(
        {
            "schema": INPUT_SCHEMA,
            "rows": [
                {"row_id": "b", "probability": "0.7", "market_price": "0.2", "weight": "2"},
                {"row_id": "a", "probability": "0.3", "market_price": "0.1", "weight": "3"},
            ],
            "contexts": contexts,
            "supported_dependencies": base,
        }
    )


def test_full_matrix_is_deterministic_and_order_canonical():
    report = build_report(fixture())
    assert report["deterministic"] is True
    assert len({row["result_hash"] for row in report["matrix_results"]}) == 1
    assert [row["row_id"] for row in report["matrix_results"][0]["results"]] == ["a", "b"]


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("locale", "fr_FR", "ENVIRONMENT"),
        ("timezone", "Mars/Base", "ENVIRONMENT"),
        ("process_count", 0, "PROCESS_COUNT"),
        ("process_count", True, "PROCESS_COUNT"),
        ("repeat", 0, "REPEAT"),
        ("reverse_input", 1, "INPUT_ORDER"),
    ],
)
def test_invalid_context_fails_closed(field, value, error):
    payload = fixture()
    payload["contexts"][0][field] = value
    signed(payload)
    with pytest.raises(ValueError, match=error):
        build_report(payload)


def test_unsupported_dependency_version_fails_closed():
    payload = fixture()
    payload["contexts"][0]["dependencies"]["decimal"] = "9.9"
    signed(payload)
    with pytest.raises(ValueError, match="VERSION_UNSUPPORTED"):
        build_report(payload)


def test_missing_dependency_identity_fails_closed():
    payload = fixture()
    payload["contexts"][0]["dependencies"] = {}
    signed(payload)
    with pytest.raises(ValueError, match="VERSION_UNSUPPORTED"):
        build_report(payload)


def test_duplicate_context_id_fails_closed():
    payload = fixture()
    payload["contexts"][1]["context_id"] = "c1"
    signed(payload)
    with pytest.raises(ValueError, match="CONTEXT_ID"):
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


def test_non_executable_and_no_host_mutation():
    report = build_report(fixture())
    assert report["host_environment_mutated"] is False
    assert report["forecast_records_created"] == 0
    assert report["execution_authorized"] is False


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4de_forecast_determinism_matrix.py"
    ).read_text()
    for token in (
        "sqlite3",
        "requests",
        "subprocess",
        "systemctl",
        "setlocale",
        "tzset",
        "create_order",
        "/home/james",
    ):
        assert token not in source
