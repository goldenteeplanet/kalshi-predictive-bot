import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from kalshi_predictor.candidate_coverage_audit import build_candidate_coverage_audit
from kalshi_predictor.candidate_funnel_audit import make_candidate_funnel_read_only_engine


def test_candidate_coverage_builds_monotonic_funnels_and_exclusions() -> None:
    evidence = [
        _row("CRYPTO-READY", category="crypto", manifest=True, gross=True, executable=True),
        _row("CRYPTO-WAIT", category="crypto", manifest=False, gross=False, executable=False),
        _row("WEATHER-STALE", category="weather", fresh_snapshot=False),
        _row("SPORT", category="sports", supported=False),
    ]
    payload = build_candidate_coverage_audit(
        evidence=evidence,
        manifest_payload={"tickers": ["CRYPTO-READY"]},
        gh2_payload={},
        crypto_r5_payload={},
        freshness_minutes=15,
        addition_limit=10,
    )

    crypto = payload["category_funnels"]["crypto"]["counts"]
    values = list(crypto.values())
    assert values == sorted(values, reverse=True)
    assert crypto["catalog"] == 2
    assert crypto["candidate_manifest"] == 1
    assert payload["exclusion_reason_counts"]["NOT_SELECTED_IN_CANDIDATE_MANIFEST"] == 1
    assert payload["exclusion_reason_counts"]["SNAPSHOT_MISSING_OR_STALE"] == 1
    assert payload["exclusion_reason_counts"]["CATEGORY_NOT_SUPPORTED_BY_GH2"] == 1
    assert payload["safe_coverage_additions"][0]["ticker"] == "CRYPTO-WAIT"


def test_candidate_coverage_fee_adjusted_ev_remains_diagnostic() -> None:
    payload = build_candidate_coverage_audit(
        evidence=[_row("C", category="crypto", manifest=True, gross=True, executable=False)],
        manifest_payload={"tickers": ["C"]},
        gh2_payload={},
        crypto_r5_payload={},
        freshness_minutes=15,
        addition_limit=10,
    )

    assert payload["safety"]["fee_adjusted_ev_is_diagnostic_only"] is True
    assert payload["category_funnels"]["crypto"]["counts"]["positive_gross_ev"] == 1
    assert payload["category_funnels"]["crypto"]["counts"]["executable_ev"] == 0


def test_candidate_coverage_read_only_engine_rejects_writes(tmp_path) -> None:
    database = tmp_path / "coverage.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
    engine = make_candidate_funnel_read_only_engine(f"sqlite:///{database.as_posix()}")
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA query_only")).scalar_one() == 1
        with pytest.raises(OperationalError, match="readonly database"):
            connection.execute(text("INSERT INTO evidence VALUES ('forbidden')"))


def _row(
    ticker: str,
    *,
    category: str,
    manifest: bool = False,
    supported: bool = True,
    fresh_snapshot: bool = True,
    gross: bool = False,
    executable: bool = False,
) -> dict:
    return {
        "ticker": ticker,
        "category": category,
        "catalog": True,
        "active": True,
        "semantically_supported": supported,
        "linked": supported,
        "fresh_snapshot": fresh_snapshot,
        "forecast": fresh_snapshot,
        "ranking": fresh_snapshot,
        "candidate_manifest": manifest,
        "positive_gross_ev": gross,
        "executable_ev": executable,
        "paper_ready": False,
        "gross_expected_value": "0.01" if gross else "-0.01",
        "fee_adjusted_expected_value": "0.009" if executable else "-0.011",
        "event_ticker": f"{ticker}-EVENT",
        "series_ticker": f"{ticker}-SERIES",
        "crypto_symbol": "BTC" if category == "crypto" else None,
        "weather_location": "new_york" if category == "weather" else None,
        "weather_target_time": None,
    }
