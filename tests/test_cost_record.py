import copy
import json
import sqlite3
from decimal import Decimal

import pytest
from test_full_cost_evidence import DECISION

from kalshi_predictor.crypto.cost_record import (
    build_cost_record,
    cost_decision_from_qualification,
    replay_cost_record,
)


def record():
    return build_cost_record(decision=DECISION, selected_probability=Decimal(".7"),
                             executable_price=Decimal(".5"), side="YES")


def test_actual_sqlite_reopen_recomputes_original_cost_record(tmp_path):
    path = tmp_path / "cost.db"
    original = record()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE records (payload TEXT NOT NULL)")
        db.execute("INSERT INTO records VALUES (?)", (json.dumps(original),))
    with sqlite3.connect(path) as db:
        loaded = json.loads(db.execute("SELECT payload FROM records").fetchone()[0])
    result = replay_cost_record(loaded, expected_decision=DECISION)
    assert result == original["assessment"]
    assert result["full_net_ev"] is None
    assert not result["execution_authority"]


@pytest.mark.parametrize("field,value", [
    ("full_net_ev", ".2"), ("clears_net_gate", True), ("execution_authority", True),
    ("blockers", []),
])
def test_stored_verdict_tampering_is_rejected(field, value):
    original = record()
    original["assessment"][field] = value
    with pytest.raises(ValueError, match="RECOMPUTATION_MISMATCH"):
        replay_cost_record(original, expected_decision=DECISION)


def test_source_hash_and_foreign_decision_cannot_be_substituted():
    original = record()
    damaged = copy.deepcopy(original)
    damaged["request"]["calibration_dataset"]["payload_hex"] = "61"
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        replay_cost_record(damaged, expected_decision=DECISION)
    with pytest.raises(ValueError, match="DECISION_MISMATCH"):
        replay_cost_record(original, expected_decision=DECISION | {"ticker": "OTHER"})


def test_unrecognized_record_fields_cannot_hide_authority():
    original = record() | {"paper_eligible": True}
    with pytest.raises(ValueError, match="RECOMPUTATION_MISMATCH"):
        replay_cost_record(original, expected_decision=DECISION)


def test_qualification_side_probability_is_complemented_once_without_mutation():
    original = dict(forecast_probability=".7", side="BUY_NO", model_artifact_sha256="a"*64)
    before = original.copy()
    cost = cost_decision_from_qualification(original)
    assert cost["selected_probability"] == "0.3"
    assert cost["model_version"] == "a"*64
    assert cost["segment"] == "UNDECLARED"
    assert original == before
