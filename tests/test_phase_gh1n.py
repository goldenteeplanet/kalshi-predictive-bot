from kalshi_predictor.phase_gh1n import synchronized_market_implied_audit


def test_gh1n_synchronized_midpoint_edge_equals_negative_half_spread() -> None:
    result = synchronized_market_implied_audit(
        ticker="KXBTC-TEST",
        payload={"orderbook_fp": {"yes_dollars": [["0.48", "5"]], "no_dollars": [["0.50", "5"]]}},
        fetch_started_at="2026-01-01T00:00:00+00:00", fetch_completed_at="2026-01-01T00:00:00.1+00:00",
    )
    assert result["midpoint_forecast"] == "0.49"
    assert result["best_executable_edge"] == "-0.01"
    assert result["synchronized_edge_positive"] is False
