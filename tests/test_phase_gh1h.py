from kalshi_predictor.phase_gh1h import analyze_public_orderbook


def test_gh1h_calibrates_quoted_book_without_auth_or_writes() -> None:
    result = analyze_public_orderbook(
        ticker="KXBTC-TEST",
        category="crypto",
        payload={"orderbook_fp": {"yes_dollars": [["0.48", "5"]], "no_dollars": [["0.51", "5"]]}},
    )
    assert result["spread"] == "0.01"
    assert result["legacy_parser_consistent"] is True
    assert result["ranking_effect"]["liquidity_usable"] is True
    assert result["risk_effect"]["gate_pass"] is True
