from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import prospective_evaluation_values


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ae_reconciliation_plan.py"
    spec = importlib.util.spec_from_file_location("phase4ae_reconciliation_plan", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _phase4ad_test_module():
    path = Path(__file__).with_name("test_phase4ad_reconciliation_attribution.py")
    spec = importlib.util.spec_from_file_location("phase4ad_fixture_for_4ae", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ready_fixture(tmp_path: Path):
    helper = _phase4ad_test_module()
    ad_module, source_db, research_db, hint_path, hint = helper._fixture_files(tmp_path)
    now = datetime(2026, 8, 25, 19, 10, tzinfo=UTC)
    history = helper._history(tmp_path, ad_module, hint, now - timedelta(seconds=10))
    attribution = ad_module.audit(
        source_db, research_db, hint_path, history, now=now, maximum_age_seconds=60
    )
    attribution_path = tmp_path / "attribution.json"
    attribution_path.write_text(json.dumps(attribution), encoding="utf-8")
    return (
        _module(),
        ad_module,
        source_db,
        research_db,
        hint_path,
        history,
        attribution_path,
        attribution,
        now,
    )


def _plan(fixture, *, now=None, maximum_age_seconds=60):
    module, _, source_db, research_db, hint_path, history, attribution_path, _, at = fixture
    return module.plan(
        source_db,
        research_db,
        attribution_path,
        history,
        hint_path,
        now=now or at,
        maximum_age_seconds=maximum_age_seconds,
    )


def test_one_fully_planned_evaluation_and_database_bytes_unchanged(tmp_path: Path) -> None:
    fixture = _ready_fixture(tmp_path)
    source_db, research_db = fixture[2], fixture[3]
    before = source_db.read_bytes(), research_db.read_bytes()
    result = _plan(fixture)
    assert result["disposition_counts"] == {"PLANNED": 1}
    assert result["planned_row_count"] == 1
    assert result["safe_for_research_apply"] is True
    assert result["rows"][0]["planned_record_hash"]
    assert before == (source_db.read_bytes(), research_db.read_bytes())


@pytest.mark.parametrize(
    ("crypto_probability", "bid", "ask", "terminal"),
    [
        ("0.60", "0.39", "0.41", "EVALUATED_EXECUTABLE"),
        ("0.43", "0.39", "0.41", "CALIBRATION_ONLY"),
        ("0.40", "0.39", "0.41", "NON_POSITIVE_GROSS_EDGE"),
    ],
)
def test_shared_phase4cd_terminal_formulas(crypto_probability, bid, ask, terminal) -> None:
    helper = _phase4ad_test_module()
    capture = helper._capture(
        crypto_probability=crypto_probability, best_yes_bid=bid, best_yes_ask=ask
    )
    settlement = helper._settlement()
    values = prospective_evaluation_values(capture, settlement)
    row = _module()._planned_row(capture, settlement, fees=Decimal("0"), slippage=Decimal("0"))
    assert row["terminal_reason"] == terminal == values["terminal_reason"]
    assert row["evaluation_id"] == values["evaluation_id"]
    assert row["market_brier"] == values["market_brier"]
    assert row["model_log_loss"] == values["model_log_loss"]


def test_stable_ordering_for_multiple_rows() -> None:
    rows = [
        {
            "disposition": "PLANNED",
            "settlement_timestamp": "2026-08-25T19:00:00Z",
            "snapshot_timestamp": "2026-08-25T18:00:00Z",
            "ticker": "B",
            "capture_id": "2",
        },
        {
            "disposition": "PLANNED",
            "settlement_timestamp": "2026-08-25T19:00:00Z",
            "snapshot_timestamp": "2026-08-25T17:00:00Z",
            "ticker": "A",
            "capture_id": "1",
        },
    ]
    assert [row["capture_id"] for row in _module()._order_rows(rows)] == ["1", "2"]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("evaluation", "EVALUATION_APPEARED"),
        ("settlement", "SETTLEMENT_DRIFTED"),
        ("capture", "CAPTURE_DRIFTED"),
        ("missing_capture", "SOURCE_MISSING"),
        ("missing_settlement", "SOURCE_MISSING"),
    ],
)
def test_race_drift_and_missing_sources(tmp_path: Path, mutation: str, expected: str) -> None:
    fixture = _ready_fixture(tmp_path)
    source_db, research_db = fixture[2], fixture[3]
    if mutation == "evaluation":
        connection = sqlite3.connect(research_db)
        connection.execute(
            "INSERT INTO prospective_pair_evaluations VALUES(?,?,?)",
            ("eval-1", "capture-1", fixture[7]["rows"][0]["settlement_hash"]),
        )
    elif mutation == "settlement":
        connection = sqlite3.connect(source_db)
        connection.execute("UPDATE settlements SET result='no' WHERE ticker='KXTEST-1'")
    elif mutation == "capture":
        connection = sqlite3.connect(research_db)
        connection.execute("UPDATE prospective_paired_captures SET crypto_probability='0.55'")
    elif mutation == "missing_capture":
        connection = sqlite3.connect(research_db)
        connection.execute("DELETE FROM prospective_paired_captures")
    else:
        connection = sqlite3.connect(source_db)
        connection.execute("DELETE FROM settlements")
    connection.commit()
    connection.close()
    result = _plan(fixture)
    assert result["disposition_counts"] == {expected: 1}
    assert result["safe_for_research_apply"] is False


def test_stale_attribution_boundary_is_inclusive(tmp_path: Path) -> None:
    fixture = _ready_fixture(tmp_path)
    at = fixture[8]
    result = _plan(fixture, now=at + timedelta(seconds=60), maximum_age_seconds=60)
    assert result["disposition_counts"] == {"ATTRIBUTION_STALE": 1}


def test_tampered_attribution_and_history_are_rejected(tmp_path: Path) -> None:
    fixture = _ready_fixture(tmp_path)
    attribution_path, history = fixture[6], fixture[5]
    payload = json.loads(attribution_path.read_text(encoding="utf-8"))
    payload["ready_count"] = 999
    attribution_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="ATTRIBUTION_HASH_MISMATCH"):
        _plan(fixture)
    attribution_path.write_text(json.dumps(fixture[7]), encoding="utf-8")
    manifest_path = history / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["total_entries"] = 99
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="MANIFEST_HASH_MISMATCH"):
        _plan(fixture)


def test_unknown_planning_failure_is_blocked(tmp_path: Path, monkeypatch) -> None:
    fixture = _ready_fixture(tmp_path)
    monkeypatch.setattr(fixture[0], "_planned_row", lambda *args, **kwargs: 1 / 0)
    result = _plan(fixture)
    assert result["disposition_counts"] == {"PLAN_BLOCKED": 1}


def test_empty_input_is_fail_closed(tmp_path: Path) -> None:
    fixture = _ready_fixture(tmp_path)
    module, ad_module, attribution_path = fixture[0], fixture[1], fixture[6]
    payload = fixture[7]
    payload["rows"] = []
    payload["rows_hash"] = ad_module.canonical_hash([])
    payload["artifact_hash"] = ad_module.artifact_hash(payload)
    attribution_path.write_text(json.dumps(payload), encoding="utf-8")
    result = _plan(fixture)
    assert result["input_row_count"] == 0
    assert result["safe_for_research_apply"] is False
    assert module.artifact_hash(result) == result["artifact_hash"]


def test_atomic_publication_leaves_no_temporary_file(tmp_path: Path) -> None:
    fixture = _ready_fixture(tmp_path)
    module, result = fixture[0], _plan(fixture)
    output = tmp_path / "plan.json"
    module.write_atomic(output, result)
    assert json.loads(output.read_text(encoding="utf-8")) == result
    assert not list(tmp_path.glob(".plan.json.*.tmp"))


def test_timezone_equivalent_inputs_produce_identical_planned_rows() -> None:
    helper, module = _phase4ad_test_module(), _module()
    first_capture = helper._capture(snapshot_timestamp="2026-08-25T18:00:00+00:00")
    second_capture = helper._capture(snapshot_timestamp="2026-08-25T13:00:00-05:00")
    first_settlement = helper._settlement(
        settled_at="2026-08-25T19:00:00+00:00",
        updated_at="2026-08-25T19:01:00+00:00",
    )
    second_settlement = helper._settlement(
        settled_at="2026-08-25T14:00:00-05:00",
        updated_at="2026-08-25T14:01:00-05:00",
    )
    first = module._planned_row(
        first_capture, first_settlement, fees=Decimal("0"), slippage=Decimal("0")
    )
    second = module._planned_row(
        second_capture, second_settlement, fees=Decimal("0"), slippage=Decimal("0")
    )
    assert first == second
