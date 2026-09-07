from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.roadmap.artifacts import verify_signed_artifact
from kalshi_predictor.roadmap.category_census import (
    build_category_ingestion_census,
    write_category_ingestion_census,
)
from kalshi_predictor.roadmap.category_contract import CATEGORY_NAMES


def _payload(markets):
    now = datetime.now(UTC)
    return {
        "source": {
            "name": "official-api",
            "kind": "external",
            "state": "READY",
            "published_at": (now - timedelta(minutes=3)).isoformat(),
            "available_at": (now - timedelta(minutes=2)).isoformat(),
            "ingested_at": (now - timedelta(minutes=1)).isoformat(),
        },
        "markets": markets,
        "counts": {"active_markets": len(markets)},
    }


def _market(ticker: str, **overrides):
    row = {
        "ticker": ticker,
        "active": True,
        "verified_link": True,
        "fresh_snapshot": True,
        "fresh_features": True,
        "forecast": True,
        "ranking": True,
        "opportunity": True,
        "risk_evidence": True,
        "paper_trace": True,
    }
    row.update(overrides)
    return row


def test_always_reports_all_categories_and_exact_market_gaps() -> None:
    census = build_category_ingestion_census(
        {"sports": _payload([_market("GAME-1"), _market("GAME-2", verified_link=False)])}
    )

    assert [row["category"] for row in census["categories"]] == list(CATEGORY_NAMES)
    sports = census["categories"][2]
    assert sports["stage_coverage"]["verified_link"]["numerator"] == 1
    assert sports["stage_coverage"]["verified_link"]["denominator"] == 2
    assert sports["stage_coverage"]["verified_link"]["missing_tickers"] == ["GAME-2"]
    assert census["market_gaps"][0]["first_blocker"] == "NO_VERIFIED_LINK"
    assert census["market_gaps"][0]["ticker"] == "GAME-2"


def test_first_blocker_is_pipeline_order_not_input_order() -> None:
    census = build_category_ingestion_census(
        {
            "economic": _payload(
                [_market("CPI-1", verified_link=False, forecast=False, blockers=["custom"])]
            )
        }
    )
    gap = census["market_gaps"][0]
    assert gap["first_blocker"] == "NO_VERIFIED_LINK"
    assert gap["blockers"] == ["NO_VERIFIED_LINK", "NO_FORECAST", "CUSTOM"]


def test_aggregate_only_evidence_is_explicitly_insufficient() -> None:
    census = build_category_ingestion_census({"news": _payload([])})
    news = census["categories"][4]
    assert news["evidence_granularity"] == "AGGREGATE_ONLY"
    assert "DETAIL_EVIDENCE_MISSING" in news["deterministic_blockers"]
    assert news["stage_coverage"]["forecast"]["coverage_pct"] is None


def test_signed_artifact_and_safety_contract(tmp_path) -> None:
    census = build_category_ingestion_census({"crypto": _payload([_market("BTC-1")])})
    path = write_category_ingestion_census(tmp_path / "census.json", census)
    verified = verify_signed_artifact(path)
    assert verified["verified"] is True
    assert all(value is False for value in census["safety"].values())


def test_rejects_unknown_category_and_missing_ticker() -> None:
    with pytest.raises(ValueError, match="Unknown categories"):
        build_category_ingestion_census({"politics": _payload([])})
    with pytest.raises(ValueError, match="missing ticker"):
        build_category_ingestion_census({"general": _payload([_market("")])})
