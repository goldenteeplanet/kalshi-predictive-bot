from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCRIPT = Path(__file__).parents[1] / "scripts/local/phase4er_operator_review_priority_queue.py"
SPEC = importlib.util.spec_from_file_location("phase4er", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _seal(value):
    value.pop("artifact_hash", None)
    value["artifact_hash"] = canonical_hash(value)
    return value


def candidate(
    candidate_id,
    *,
    expires="2026-08-26T12:10:00.000Z",
    evidence="2026-08-26T12:00:00.000Z",
    value=100,
    cost=10,
    eligible=True,
    review=True,
):
    return {
        "candidate_id": candidate_id,
        "packet_hash": (candidate_id[0].lower() if candidate_id[0].lower() in "abcdef" else "a")
        * 64,
        "expires_at_utc": expires,
        "evidence_at_utc": evidence,
        "expected_value_micros": value,
        "review_cost_units": cost,
        "eligible": eligible,
        "operator_review_required": review,
    }


def payload(candidates=None):
    return _seal(
        {
            "schema": MODULE.INPUT_SCHEMA,
            "as_of_utc": "2026-08-26T12:00:30.000Z",
            "candidates": candidates if candidates is not None else [candidate("A")],
        }
    )


def ids(report):
    return [item["candidate_id"] for item in report["queue"]]


def test_orders_by_expiration_first():
    report = MODULE.build_report(
        payload(
            [
                candidate("A", expires="2026-08-26T12:20:00.000Z", value=1000),
                candidate("B", expires="2026-08-26T12:05:00.000Z", value=1),
            ]
        )
    )
    assert ids(report) == ["B", "A"]


def test_expected_value_breaks_expiration_tie_descending():
    report = MODULE.build_report(payload([candidate("A", value=10), candidate("B", value=20)]))
    assert ids(report) == ["B", "A"]


def test_freshness_breaks_value_tie_newest_first():
    report = MODULE.build_report(
        payload(
            [
                candidate("A", evidence="2026-08-26T11:59:00.000Z"),
                candidate("B", evidence="2026-08-26T12:00:20.000Z"),
            ]
        )
    )
    assert ids(report) == ["B", "A"]
    assert report["queue"][0]["freshness_age_ms"] == 10_000


def test_review_cost_breaks_freshness_tie_lowest_first():
    report = MODULE.build_report(payload([candidate("A", cost=20), candidate("B", cost=5)]))
    assert ids(report) == ["B", "A"]


def test_candidate_id_is_stable_final_tie_breaker():
    report = MODULE.build_report(payload([candidate("B"), candidate("A")]))
    assert ids(report) == ["A", "B"]
    assert [item["position"] for item in report["queue"]] == [1, 2]


def test_input_order_does_not_change_queue_content():
    values = [candidate("A", value=9), candidate("B", value=10)]
    first = MODULE.build_report(payload(values))
    second = MODULE.build_report(payload(list(reversed(values))))
    assert first["queue"] == second["queue"]
    assert first["excluded"] == second["excluded"]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"eligible": False}, "NOT_ELIGIBLE"),
        ({"review": False}, "REVIEW_NOT_REQUIRED"),
        ({"expires": "2026-08-26T12:00:30.000Z"}, "EXPIRED"),
        ({"expires": "2026-08-26T11:59:00.000Z"}, "EXPIRED"),
    ],
)
def test_nonqueueable_candidates_are_excluded(changes, reason):
    report = MODULE.build_report(payload([candidate("A", **changes)]))
    assert report["queue"] == []
    assert reason in report["excluded"][0]["reason_codes"]


def test_all_exclusion_reasons_are_reported_in_stable_order():
    report = MODULE.build_report(
        payload(
            [
                candidate(
                    "A",
                    eligible=False,
                    review=False,
                    expires="2026-08-26T12:00:30.000Z",
                )
            ]
        )
    )
    assert report["excluded"][0]["reason_codes"] == [
        "NOT_ELIGIBLE",
        "REVIEW_NOT_REQUIRED",
        "EXPIRED",
    ]


def test_empty_queue_is_valid():
    report = MODULE.build_report(payload([]))
    assert report["queued_count"] == report["excluded_count"] == 0


def test_duplicate_candidate_fails_closed():
    with pytest.raises(ValueError, match="DUPLICATE"):
        MODULE.build_report(payload([candidate("A"), candidate("A")]))


@pytest.mark.parametrize(
    "field",
    [
        "candidate_id",
        "packet_hash",
        "expires_at_utc",
        "evidence_at_utc",
        "expected_value_micros",
        "review_cost_units",
        "eligible",
        "operator_review_required",
    ],
)
def test_missing_candidate_field_fails_closed(field):
    item = candidate("A")
    item.pop(field)
    with pytest.raises(ValueError, match="CANDIDATE_FIELDS"):
        MODULE.build_report(payload([item]))


@pytest.mark.parametrize(
    ("field", "bad", "code"),
    [
        ("candidate_id", "", "CANDIDATE_ID"),
        ("packet_hash", "bad", "PACKET_HASH"),
        ("expires_at_utc", "2026-08-26T12:10:00Z", "EXPIRATION"),
        ("evidence_at_utc", "2026-08-26T12:01:00.000Z", "FUTURE_EVIDENCE"),
        ("expected_value_micros", True, "EXPECTED_VALUE"),
        ("review_cost_units", -1, "REVIEW_COST"),
        ("eligible", "yes", "STATUS"),
        ("operator_review_required", 1, "STATUS"),
    ],
)
def test_invalid_candidate_values_fail_closed(field, bad, code):
    item = candidate("A")
    item[field] = bad
    with pytest.raises(ValueError, match=code):
        MODULE.build_report(payload([item]))


def test_noncanonical_as_of_fails_closed():
    value = payload()
    value["as_of_utc"] = "2026-08-26T12:00:30Z"
    _seal(value)
    with pytest.raises(ValueError, match="AS_OF"):
        MODULE.build_report(value)


def test_input_tampering_fails_closed():
    value = payload()
    value["candidates"][0]["review_cost_units"] = 1
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        MODULE.build_report(value)


def test_atomic_publication(tmp_path):
    report = MODULE.build_report(payload())
    output = tmp_path / "queue.json"
    MODULE.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_queue_never_authorizes_or_mutates():
    report = MODULE.build_report(payload())
    assert report["operator_authorization_recorded"] is False
    assert report["paper_order_creation_authorized"] is False
    assert report["paper_orders_created"] == 0
    assert report["execution_authorized"] is False
    assert report["production_records_created"] == 0


def test_source_has_no_connected_or_mutating_surface():
    source = SCRIPT.read_text()
    for token in (
        "sqlite3",
        "requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "create_order",
        "insert_order",
        "/home/james",
    ):
        assert token not in source
