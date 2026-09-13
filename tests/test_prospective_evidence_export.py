"""Synthetic journal originals; no production evidence or policy is fabricated."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from test_prospective_calibration import NOW, decision, official

from kalshi_predictor.crypto.prospective_evidence_export import (
    JournalOriginal,
    export_prospective_evidence,
)
from kalshi_predictor.overnight_paper.current_research_store import PREFIX
from kalshi_predictor.overnight_paper.store import digest, encode

AS_OF = NOW + timedelta(hours=4)


def journal(kind, identity, payload, at):
    envelope = dict(
        kind="CURRENT_MISSION_RESEARCH_V1",
        record_kind=kind,
        identity=identity,
        record=payload,
        recorded_at=at.isoformat(),
        payload_sha256=digest(payload),
    )
    return JournalOriginal(
        PREFIX + kind.lower() + ":" + digest({"identity": identity}),
        at.isoformat(),
        encode(envelope).encode(),
    )


def shadow(row):
    return journal(
        "PROSPECTIVE_SHADOW",
        row["decision_id"],
        dict(row, execution_authority=False),
        NOW + timedelta(seconds=1),
    )


def evaluated(row, result=None):
    result = result or official(row)
    return journal(
        "EVALUATION",
        row["decision_id"],
        dict(
            decision=row,
            evaluation=result,
            decision_time=row["decision_time"],
            assessed_at=result["evaluated_at"],
            paper_eligible=False,
            execution_authority=False,
        ),
        NOW + timedelta(hours=3, seconds=1),
    )


def test_exports_every_final_outcome_and_original_with_pending_inventory():
    first = decision()
    second = decision(ticker="BTC-B", p_yes="0.9")
    pending = decision(ticker="BTC-C")
    records = (
        shadow(first),
        evaluated(first),
        shadow(second),
        evaluated(second, official(second, result="no", settlement_value_dollars="0")),
        shadow(pending),
    )
    output = export_prospective_evidence(records, as_of=AS_OF)
    manifest, dataset = json.loads(output.manifest), json.loads(output.dataset)
    assert manifest["evaluated_n"] == 2 and manifest["decision_n"] == 3
    assert {row["outcome"] for row in dataset["rows"]} == {0, 1}
    assert len(dataset["exclusions"]) == 1
    assert dataset["exclusions"][0]["decision_id"] == pending["decision_id"]
    assert manifest["dependency_cluster_n"] == 1
    assert manifest["independent_cluster_n"] is None
    assert manifest["calibration_policy_issued"] is manifest["paper_eligible"] is False
    originals = dict(output.originals)
    for record in records:
        assert originals[hashlib.sha256(record.payload).hexdigest()] == record.payload
    for row in dataset["rows"]:
        for hashed in row["original_references"].values():
            assert hashlib.sha256(originals[hashed]).hexdigest() == hashed
    assert manifest["original_input_coverage"] == "PARTIAL"
    assert set(manifest["missing_input_original_hashes"]) == {"a" * 64, "b" * 64, "c" * 64}
    assert export_prospective_evidence(tuple(reversed(records)), as_of=AS_OF) == output


def test_supplied_market_book_source_bytes_are_bound_without_authentication_claim():
    inputs = (b"actual source fixture", b"actual book fixture", b"actual market fixture")
    hashes = [hashlib.sha256(raw).hexdigest() for raw in inputs]
    row = decision(source_sha256=hashes[0], book_sha256=hashes[1], market_sha256=hashes[2])
    output = export_prospective_evidence(
        (shadow(row), evaluated(row)),
        as_of=AS_OF,
        attached_originals=tuple(zip(hashes, inputs, strict=True)),
    )
    manifest = json.loads(output.manifest)
    assert manifest["missing_input_original_hashes"] == []
    assert manifest["external_timestamp_attestation"] is False
    assert manifest["candidate_applicability"] is None


def test_journal_payload_tamper_is_rejected():
    row = shadow(decision())
    value = json.loads(row.payload)
    value["record"]["p_yes"] = "0.99"
    with pytest.raises(ValueError):
        export_prospective_evidence(
            (replace(row, payload=json.dumps(value).encode()),), as_of=AS_OF
        )


@pytest.mark.parametrize("change", ["ticker", "provisional", "outcome", "score"])
def test_final_original_or_evaluation_tamper_rejected_even_with_new_journal_hash(change):
    row = decision()
    score = official(row)
    if change == "score":
        score["brier"] = "0"
    else:
        market = json.loads(score["official_original_json"])
        if change == "ticker":
            market["market"]["ticker"] = "OTHER"
        elif change == "provisional":
            market["market"]["is_provisional"] = True
        else:
            market["market"]["result"] = "no"
            market["market"]["settlement_value_dollars"] = "0"
        raw = json.dumps(market).encode()
        score["official_original_json"] = raw.decode()
        score["official_sha256"] = hashlib.sha256(raw).hexdigest()
        receipt = json.loads(score["official_receipt_json"])
        receipt["source_sha256"] = score["official_sha256"]
        receipt_raw = json.dumps(receipt).encode()
        score["official_receipt_json"] = receipt_raw.decode()
        score["receipt_sha256"] = hashlib.sha256(receipt_raw).hexdigest()
    with pytest.raises(ValueError):
        export_prospective_evidence((shadow(row), evaluated(row, score)), as_of=AS_OF)


def test_forecast_retrofit_cannot_pass_by_rehashing_outer_journal():
    row = decision()
    original = json.loads(row["decision_original_json"])
    original["decision_time"] = (NOW + timedelta(hours=2)).isoformat()
    row["decision_original_json"] = json.dumps(original)
    with pytest.raises(ValueError):
        export_prospective_evidence((shadow(row),), as_of=AS_OF)


def test_evaluation_requires_prior_exact_shadow_and_export_clock():
    row = decision()
    with pytest.raises(ValueError, match="PRIOR_EXACT_SHADOW"):
        export_prospective_evidence((evaluated(row),), as_of=AS_OF)
    with pytest.raises(ValueError, match="AFTER_EXPORT"):
        export_prospective_evidence((shadow(row), evaluated(row)), as_of=NOW + timedelta(hours=2))


def test_duplicate_raw_rows_fail_instead_of_double_counting():
    original = shadow(decision())
    with pytest.raises(ValueError, match="DUPLICATE_JOURNAL"):
        export_prospective_evidence((original, original), as_of=AS_OF)


def test_duplicate_evaluation_under_new_identity_is_not_accepted():
    row = decision()
    first = evaluated(row)
    value = json.loads(first.payload)
    second = journal(
        "EVALUATION", "new-identity", value["record"], NOW + timedelta(hours=3, seconds=2)
    )
    with pytest.raises(ValueError, match="MISBOUND_EVALUATION"):
        export_prospective_evidence((shadow(row), first, second), as_of=AS_OF)


def test_attached_hash_mismatch_fails():
    with pytest.raises(ValueError, match="ATTACHED_ORIGINAL_HASH"):
        export_prospective_evidence((), as_of=AS_OF, attached_originals=(("a" * 64, b"wrong"),))
