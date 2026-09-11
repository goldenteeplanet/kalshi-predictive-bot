from datetime import UTC, datetime

import pytest

from kalshi_predictor.overnight_paper.settlement import market_lifecycle

NOW = datetime(2026, 9, 8, 4, tzinfo=UTC)
MARKET = {
    "ticker": "TEST",
    "status": "finalized",
    "result": "yes",
    "close_time": "2026-09-08T03:00:00Z",
    "settlement_ts": "2026-09-08T03:25:00Z",
}


@pytest.mark.parametrize("status", ["determined", "disputed", "amended"])
def test_result_before_finalization_never_settles(status):
    row = market_lifecycle("TEST", {**MARKET, "status": status}, now=NOW)
    assert row["state"] == "AWAITING_SETTLEMENT" and row["final"] is None


def test_close_is_not_settlement():
    assert market_lifecycle("TEST", {**MARKET, "status": "closed"}, now=NOW)["final"] is None
    assert (
        market_lifecycle("TEST", {**MARKET, "status": "active"}, now=NOW)["state"]
        == "MARKET_CLOSED"
    )


def test_rest_finalized_is_normalized_and_hash_bound():
    row = market_lifecycle("TEST", MARKET, now=NOW)
    assert row["state"] == "FINAL_RESULT_AVAILABLE"
    assert row["final"]["status"] == "settled"
    assert len(row["final"]["source_sha256"]) == 64


@pytest.mark.parametrize("value", [None, False])
def test_optional_empty_provisional_flag_preserves_finality(value):
    assert market_lifecycle("TEST", {**MARKET, "is_provisional": value}, now=NOW)["final"]


@pytest.mark.parametrize("value", [0, 1, 0.0, 1.0, "true", "false", "", [], {}])
@pytest.mark.parametrize("status", ["finalized", "determined"])
def test_non_boolean_provisional_flag_is_never_authoritative(value, status):
    with pytest.raises(ValueError, match="INVALID_PROVISIONAL_FLAG"):
        market_lifecycle("TEST", {**MARKET, "status": status, "is_provisional": value}, now=NOW)


@pytest.mark.parametrize(
    "patch",
    [
        {"settlement_ts": None},
        {"settlement_ts": "2026-09-08T04:01:00Z"},
        {"result": ""},
        {"result": "scalar"},
        {"is_provisional": True},
        {"ticker": "SIBLING"},
    ],
)
def test_incomplete_or_conflicting_result_fails_closed(patch):
    with pytest.raises(ValueError):
        market_lifecycle("TEST", {**MARKET, **patch}, now=NOW)
