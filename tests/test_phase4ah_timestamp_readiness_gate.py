from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _module():
    return _load(
        "phase4ah_tested",
        Path(__file__).parents[1] / "scripts/local/phase4ah_timestamp_readiness_gate.py",
    )


def _ag_helper():
    return _load(
        "phase4ag_fixture_for_4ah",
        Path(__file__).with_name("test_phase4ag_exchange_timestamp_collector.py"),
    )


def _fixture(tmp_path: Path, *, with_evidence: bool = False):
    helper = _ag_helper()
    ag_fixture = helper._fixture(tmp_path)
    module, database, history, ad, ae, af, now = ag_fixture
    archive_dir = None
    if with_evidence:
        archive_dir = tmp_path / "archives"
        helper._archive(
            module,
            archive_dir,
            {
                "market": {
                    "ticker": "KXTEST-1",
                    "result": "yes",
                    "settlement_ts": "2026-08-25T19:00:00Z",
                }
            },
        )
    status, evidence = module.collect(
        database,
        af,
        ad,
        ae,
        history,
        now=now,
        mode="offline",
        archive_dir=archive_dir,
    )
    status_path, evidence_path = (
        tmp_path / "phase4ag-status.json",
        tmp_path / "phase4ag-evidence.json",
    )
    status_path.write_text(json.dumps(status), encoding="utf-8")
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    return _module(), database, history, ad, ae, af, status_path, evidence_path, now


def _build(fixture, **overrides):
    module, database, history, ad, ae, af, status, evidence, now = fixture
    return module.build(
        database,
        af,
        status,
        evidence,
        ad,
        ae,
        history,
        now=overrides.get("now", now),
        freshness_seconds=overrides.get("freshness_seconds", 3600),
    )


def test_valid_evidence_reaudit_becomes_ready_and_database_unchanged(tmp_path: Path):
    fixture = _fixture(tmp_path, with_evidence=True)
    database = fixture[1]
    before = database.read_bytes()
    reaudit, gate = _build(fixture)
    assert gate["transition_counts"] == {"AUTHORITATIVE_EVIDENCE_ADDED": 1}
    assert gate["gate_state"] == "READY_FOR_CANONICALIZATION_PROPOSAL"
    assert gate["safe_for_canonicalization_proposal"] is True
    assert reaudit["classification_counts"] == {"VALIDATED_ARTIFACT_TIMESTAMP_AVAILABLE": 1}
    assert database.read_bytes() == before
    assert gate["exchange_requests_made"] is False


def test_empty_evidence_remains_waiting(tmp_path: Path):
    _, gate = _build(_fixture(tmp_path))
    assert gate["transition_counts"] == {"UNCHANGED_BLOCKED": 1}
    assert gate["gate_state"] == "WAITING_FOR_EVIDENCE"
    assert gate["safe_for_canonicalization_proposal"] is False


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("pair", "PAIR_ID_MISMATCH"),
        ("status_hash", "EVIDENCE_STATUS_HASH_MISMATCH"),
        ("timestamp", "EVIDENCE_TIMESTAMP_MISMATCH"),
        ("identity", "EVIDENCE_SOURCE_IDENTITY_MISMATCH"),
        ("ticker", "EVIDENCE_TICKER_NOT_SUCCESSFUL"),
    ],
)
def test_pair_and_evidence_mismatches_rejected(tmp_path: Path, mutation: str, message: str):
    fixture = _fixture(tmp_path, with_evidence=True)
    module, evidence_path = fixture[0], fixture[7]
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    if mutation == "pair":
        payload["pair_id"] = "x" * 64
    elif mutation == "status_hash":
        payload["source_phase4ag_artifact_hash"] = "x" * 64
    elif mutation == "timestamp":
        payload["rows"][0]["settlement_timestamp"] = "2026-08-25T19:01:00+00:00"
        payload["rows_hash"] = module.canonical_hash(payload["rows"])
    elif mutation == "identity":
        payload["rows"][0]["source_record_identity"] = "wrong"
        payload["rows_hash"] = module.canonical_hash(payload["rows"])
    else:
        payload["rows"][0]["ticker"] = "OTHER"
        payload["rows_hash"] = module.canonical_hash(payload["rows"])
    payload["artifact_hash"] = module.artifact_hash(payload)
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        _build(fixture)


def test_tampered_status_baseline_and_history_rejected(tmp_path: Path):
    fixture = _fixture(tmp_path)
    status_path = fixture[6]
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    payload["request_count"] = 99
    status_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="PHASE4AG_STATUS_HASH_MISMATCH"):
        _build(fixture)


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (None, {"classification": "RESULT_PRESENT_TIMESTAMP_MISSING"}, "ROW_ADDED"),
        ({"classification": "RESULT_PRESENT_TIMESTAMP_MISSING"}, None, "ROW_REMOVED"),
        (
            {"classification": "RESULT_PRESENT_TIMESTAMP_MISSING", "settlement_lineage_hash": "a"},
            {"classification": "RESULT_PRESENT_TIMESTAMP_MISSING", "settlement_lineage_hash": "b"},
            "LINEAGE_DRIFTED",
        ),
        (
            {"classification": "RESULT_PRESENT_TIMESTAMP_MISSING"},
            {"classification": "TIMESTAMP_CONFLICT"},
            "CONFLICT_DISCOVERED",
        ),
        (
            {"classification": "RESULT_PRESENT_TIMESTAMP_MISSING"},
            {"classification": "SOURCE_STALE"},
            "SOURCE_BECAME_STALE",
        ),
        (
            {"classification": "RESULT_PRESENT_TIMESTAMP_MISSING"},
            {"classification": "SOURCE_MISSING"},
            "SOURCE_DISAPPEARED",
        ),
    ],
)
def test_transition_matrix(before, after, expected):
    assert _module().transition(before, after) == expected


@pytest.mark.parametrize(
    ("transition_name", "classification", "expected"),
    [
        ("CONFLICT_DISCOVERED", "TIMESTAMP_CONFLICT", "ATTENTION_CONFLICT"),
        ("LINEAGE_DRIFTED", "LINEAGE_FAILURE", "ATTENTION_LINEAGE"),
        ("SOURCE_BECAME_STALE", "SOURCE_STALE", "ATTENTION_STALE"),
        ("UNEXPECTED_TRANSITION", "AUDIT_BLOCKED", "BLOCKED"),
    ],
)
def test_gate_state_attention_precedence(transition_name, classification, expected):
    rows = [{"transition": transition_name, "reaudit_classification": classification}]
    state, _ = _module()._gate_state(rows, {"ready_count": 0, "input_row_count": 1})
    assert state == expected


def test_no_eligible_rows_gate():
    state, reasons = _module()._gate_state([], {"ready_count": 0, "input_row_count": 0})
    assert state == "NO_ELIGIBLE_ROWS"
    assert reasons == ["VALIDATED_COHORT_EMPTY"]


def test_atomic_pair_publication_refusal_replace_and_cleanup(tmp_path: Path):
    fixture = _fixture(tmp_path)
    module = fixture[0]
    reaudit, gate = _build(fixture)
    reaudit_path, gate_path = tmp_path / "reaudit.json", tmp_path / "phase4ah-gate.json"
    module.publish_pair(reaudit_path, gate_path, reaudit, gate)
    with pytest.raises(FileExistsError):
        module.publish_pair(reaudit_path, gate_path, reaudit, gate)
    module.publish_pair(reaudit_path, gate_path, reaudit, gate, replace=True)
    assert not list(tmp_path.glob(".*.tmp"))
    assert (
        json.loads(gate_path.read_text(encoding="utf-8"))["publication_pair_id"]
        == reaudit["publication_pair_id"]
    )
