import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from kalshi_predictor.candidate_funnel_audit import (
    build_candidate_funnel_audit,
    make_candidate_funnel_read_only_engine,
)


def test_candidate_funnel_preserves_source_gate_and_fee_diagnostic() -> None:
    payload = build_candidate_funnel_audit(
        gh2_payload={
            "generated_at": "2026-08-09T18:00:00+00:00",
            "weather_gate": {
                "weather_rows": [
                    {
                        "ticker": "WEATHER",
                        "first_blocker": "MARKET_WINDOW_INELIGIBLE",
                        "forecast_probability": "0.60",
                        "best_price": "0.50",
                        "snapshot_fresh": True,
                        "weather_source_forecast_fresh": True,
                        "weather_feature_fresh": True,
                        "paper_ready": False,
                    }
                ]
            },
        },
        crypto_r5_payload={
            "generated_at": "2026-08-09T18:00:00+00:00",
            "blocked_active_pure_examples": [
                {
                    "ticker": "CRYPTO",
                    "blocked_reason": "WATCH_NO_POSITIVE_EXPECTED_VALUE",
                    "expected_value": "-0.0030",
                }
            ],
            "best_ev_candidates": [
                {
                    "ticker": "CRYPTO",
                    "what_would_make_paper_ready": ["Price must improve."],
                }
            ],
        },
        ranking_evidence={
            "CRYPTO": {
                "forecast_probability": "0.997",
                "best_price": "1.0000",
                "gross_expected_value": "-0.0030",
                "estimated_taker_fee": "0.0000",
                "fee_adjusted_expected_value": "-0.0030",
                "price_tick_valid": True,
            }
        },
    )

    assert payload["summary"]["candidate_count"] == 2
    assert payload["summary"]["first_blocker_counts"] == {
        "EV_NOT_POSITIVE": 1,
        "MARKET_WINDOW_INELIGIBLE": 1,
    }
    crypto = next(row for row in payload["candidates"] if row["ticker"] == "CRYPTO")
    assert crypto["source_blocker"] == "WATCH_NO_POSITIVE_EXPECTED_VALUE"
    assert crypto["first_blocker"] == "EV_NOT_POSITIVE"
    assert crypto["price_and_ev"]["fee_adjusted_ev_role"].startswith("DIAGNOSTIC_ONLY")
    assert crypto["next_condition"] == "Price must improve."


def test_candidate_funnel_does_not_invent_missing_evidence() -> None:
    payload = build_candidate_funnel_audit(
        gh2_payload={"weather_gate": {"weather_rows": []}},
        crypto_r5_payload={
            "blocked_active_pure_examples": [
                {"ticker": "CRYPTO", "blocked_reason": "UNKNOWN"}
            ]
        },
        ranking_evidence={},
    )

    row = payload["candidates"][0]
    assert row["first_blocker"] == "UNKNOWN"
    assert row["price_and_ev"]["fee_adjusted_expected_value"] is None
    assert row["market_quality"]["executable_book"] is None


def test_candidate_funnel_honors_gh2_manifest_scope() -> None:
    payload = build_candidate_funnel_audit(
        gh2_payload={
            "candidate_alignment": {"tickers": ["CURRENT"]},
            "weather_gate": {"weather_rows": []},
        },
        crypto_r5_payload={
            "blocked_active_pure_examples": [
                {"ticker": "CURRENT", "blocked_reason": "EV_NOT_POSITIVE"},
                {"ticker": "EXPIRED", "blocked_reason": "EXPIRED_WINDOW"},
            ]
        },
        ranking_evidence={},
    )

    assert payload["summary"]["manifest_scope_count"] == 1
    assert [row["ticker"] for row in payload["candidates"]] == ["CURRENT"]


def test_candidate_funnel_engine_enforces_query_only(tmp_path) -> None:
    database_path = tmp_path / "audit.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
        connection.execute("INSERT INTO evidence VALUES ('accepted')")

    engine = make_candidate_funnel_read_only_engine(
        f"sqlite:///{database_path.as_posix()}"
    )
    with engine.connect() as connection:
        assert connection.execute(text("SELECT value FROM evidence")).scalar_one() == "accepted"
        assert connection.execute(text("PRAGMA query_only")).scalar_one() == 1
        with pytest.raises(OperationalError, match="readonly database"):
            connection.execute(text("INSERT INTO evidence VALUES ('forbidden')"))
