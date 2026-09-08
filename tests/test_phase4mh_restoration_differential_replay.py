from __future__ import annotations

import copy

import pytest

from scripts.local.phase4me_nonce_consumption_ledger import _digest, make_record
from scripts.local.phase4mh_restoration_differential_replay import certify_all_cuts, certify_cut
from tests.test_phase4me_nonce_consumption_ledger import _committed, _consumed, _prepared


def _abort_history():
    records = _prepared()
    records.append(
        make_record(
            "ABORT",
            generation=2,
            expected_generation=1,
            transaction_id="tx-1",
            token_id="a" * 64,
            nonce_sha256="b" * 64,
            receipt_sha256=None,
            occurred_at="2026-08-28T20:01:00Z",
            previous_record_sha256=records[-1]["record_sha256"],
        )
    )
    return records


def _multi_history():
    records = _committed()
    records.append(
        make_record(
            "PREPARE",
            generation=4,
            expected_generation=3,
            transaction_id="tx-2",
            token_id="d" * 64,
            nonce_sha256="e" * 64,
            receipt_sha256=None,
            occurred_at="2026-08-28T20:03:00Z",
            previous_record_sha256=records[-1]["record_sha256"],
        )
    )
    return records


@pytest.mark.parametrize(
    "records",
    [_prepared(), _consumed(), _committed(), _abort_history(), _multi_history()],
)
def test_every_cut_is_equivalent_across_all_transaction_states(records) -> None:
    result = certify_all_cuts(records, evaluated_at="2026-08-28T20:10:00Z")
    assert result["verdict"] == "PASS"
    assert result["passing_cut_count"] == len(records) + 1
    assert all(row["first_divergent_field"] is None for row in result["cuts"])


def test_duplicate_exact_replay_is_canonicalized_and_equivalent() -> None:
    records = _committed()
    records.insert(2, copy.deepcopy(records[1]))
    result = certify_all_cuts(records, evaluated_at="2026-08-28T20:10:00Z")
    assert result["verdict"] == "PASS"
    assert result["source_record_count"] == 4
    assert result["canonical_record_count"] == 3
    assert result["cut_count"] == 4


def test_crash_recovery_classifications_match_at_every_cut() -> None:
    for records in (_prepared(), _consumed()):
        result = certify_all_cuts(records, evaluated_at="2026-08-28T20:10:00Z")
        assert result["verdict"] == "PASS"
        assert all(
            not any(row["field"] == "recovery_classification" for row in cut["divergences"])
            for cut in result["cuts"]
        )


def test_omitted_suffix_localizes_generation_at_exact_cut() -> None:
    records = _committed()
    result = certify_cut(
        records,
        cut_point=1,
        evaluated_at="2026-08-28T20:10:00Z",
        supplied_suffix=records[1:-1],
    )
    assert result["verdict"] == "REFUSE"
    assert result["cut_point"] == 1
    assert result["first_divergent_field"] == "generation"


@pytest.mark.parametrize(
    "mutation,expected_restoration_error",
    [
        (lambda suffix: suffix.reverse(), "SUFFIX_ANCHOR_INCOMPATIBLE"),
        (lambda suffix: suffix[0].update(token_id="f" * 64), "TOKEN_OR_NONCE_SUBSTITUTION"),
        (lambda suffix: suffix[0].update(record_sha256="0" * 64), "RECORD_HASH_INVALID"),
        (
            lambda suffix: suffix[0].update(previous_record_sha256="f" * 64),
            "SUFFIX_ANCHOR_INCOMPATIBLE",
        ),
    ],
)
def test_bad_suffix_localizes_restoration_refusal(
    mutation, expected_restoration_error: str
) -> None:
    records = _committed()
    suffix = copy.deepcopy(records[1:])
    mutation(suffix)
    if "token_id" in suffix[0] and suffix[0]["token_id"] == "f" * 64:
        suffix[0]["record_sha256"] = _digest(
            {key: value for key, value in suffix[0].items() if key != "record_sha256"}
        )
    result = certify_cut(
        records,
        cut_point=1,
        evaluated_at="2026-08-28T20:10:00Z",
        supplied_suffix=suffix,
    )
    assert result["verdict"] == "REFUSE"
    assert result["errors"] == ["RESTORATION_REFUSED"]
    assert any(expected_restoration_error in error for error in result["restoration_errors"])


def test_conflicting_replay_makes_authoritative_history_invalid() -> None:
    records = _committed()
    conflict = copy.deepcopy(records[-1])
    conflict["occurred_at"] = "2026-08-28T20:04:00Z"
    conflict["record_sha256"] = _digest(
        {key: value for key, value in conflict.items() if key != "record_sha256"}
    )
    records.append(conflict)
    result = certify_all_cuts(records, evaluated_at="2026-08-28T20:10:00Z")
    assert result["verdict"] == "REFUSE"
    assert result["first_failing_cut_point"] == 0


def test_certifier_has_no_write_runtime_or_order_capability() -> None:
    safety = certify_all_cuts(_committed(), evaluated_at="2026-08-28T20:10:00Z")["safety"]
    assert safety["read_only"] is True
    assert all(value is False for key, value in safety.items() if key != "read_only")
