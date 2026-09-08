import sqlite3

import pytest

from kalshi_predictor.overnight_paper.historical import (
    audit_research_database,
    score_historical,
    summarize_historical,
)


def record(**changes):
    before = "2026-09-08T00:00:00Z"
    row = dict(
        ticker="TEST",
        event_ticker="EVENT",
        model_version="fixture-v1",
        forecast_probability=0.75,
        forecast_at=before,
        decision_at=before,
        close_time="2026-09-08T01:00:00Z",
        snapshot_at=before,
        snapshot_available_at=before,
        source_updated_at=before,
        source_available_at=before,
        model_training_cutoff=before,
        source_sha256="fixture",
        snapshot_sha256="fixture",
        forecast_sha256="fixture",
        final_ticker="TEST",
        final_result="yes",
        final_status="settled",
        final_settled_at="2026-09-08T01:30:00Z",
        final_source_sha256="fixture",
    )
    return row | changes


@pytest.mark.parametrize(
    "field",
    [
        "forecast_at",
        "snapshot_at",
        "snapshot_available_at",
        "source_updated_at",
        "source_available_at",
        "model_training_cutoff",
    ],
)
def test_rejects_future_visible_or_trained_inputs(field):
    assert score_historical(record(**{field: "2026-09-08T00:00:01Z"}))["status"] == "BLOCKED"


@pytest.mark.parametrize(
    "change",
    [
        {"source_available_at": None},
        {"final_status": "closed"},
        {"final_ticker": "OTHER"},
        {"final_settled_at": "2026-09-08T00:30:00Z"},
        {"forecast_probability": float("nan")},
        {"decision_at": "2026-09-08T01:00:00Z"},
        {"model_training_cutoff": "2026-09-08T00:00:00"},
    ],
)
def test_missing_visibility_nonfinal_identity_and_invalid_inputs_block(change):
    assert score_historical(record(**change))["status"] == "BLOCKED"


def test_historical_scores_are_separate_and_event_balanced():
    summary = summarize_historical(
        [
            record(),
            record(ticker="T2", final_ticker="T2"),
            record(ticker="T3", final_ticker="T3", event_ticker="EVENT2", forecast_probability=0.5),
        ]
    )
    assert summary["historical_evaluated_contracts"] == 3
    assert summary["historical_independent_events"] == 2
    assert summary["event_balanced_brier"] == (0.0625 + 0.25) / 2
    assert summary["local_paper_settled_events"] == summary["shadow_settled_events"] == 0


def test_impossible_prediction_loss_is_not_clamped():
    assert score_historical(record(forecast_probability=0))["log_loss"] == "Infinity"


def test_missing_and_malformed_database_fail_without_creating_or_repairing(tmp_path):
    missing = tmp_path / "missing.db"
    assert audit_research_database(missing)["reason"] == "RESEARCH_DATABASE_READ_ERROR"
    assert not missing.exists()
    damaged = tmp_path / "damaged.db"
    damaged.write_bytes(b"not a database")
    assert audit_research_database(damaged)["reason"] == "RESEARCH_DATABASE_READ_ERROR"
    assert damaged.read_bytes() == b"not a database"


def test_readonly_schema_and_bounded_forecast_rows(tmp_path):
    path = tmp_path / "research.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE forecasts(id INTEGER,ticker TEXT,forecasted_at TEXT,feature_json TEXT)"
        )
        connection.executemany(
            "INSERT INTO forecasts VALUES(?,?,?,?)",
            [(i, "T", "2026-09-08T00:00:00Z", "{}") for i in range(4)],
        )
    before = path.read_bytes()
    result = audit_research_database(path, max_rows=2)
    assert result["rows_selected"] == 2
    assert result["historical_evaluated_contracts"] == 0
    assert result["explicit_visibility_field_coverage"]["source_available_at"] == 0
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="ROW_LIMIT"):
        audit_research_database(path, max_rows=5001)
