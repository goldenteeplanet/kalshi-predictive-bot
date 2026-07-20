from decimal import Decimal

from kalshi_predictor.phase_gh1d import compare_websocket_to_rest


def test_gh1d_detects_liquidity_and_risk_gate_change() -> None:
    websocket = {"orderbook_fp": {"yes_dollars": [["0.48", "5"]], "no_dollars": [["0.51", "5"]]}}
    rest = {"orderbook_fp": {"yes_dollars": [["0.40", "5"]], "no_dollars": [["0.50", "5"]]}}

    result = compare_websocket_to_rest(
        ticker="KXBTC-TEST", websocket_orderbook=websocket, rest_orderbook=rest
    )

    assert result["category"] == "crypto"
    assert result["websocket"]["spread"] == "0.01"
    assert result["rest"]["spread"] == "0.10"
    assert Decimal(result["delta"]["spread"]) == Decimal("-0.09")
    assert result["ranking_effect"]["classification_changed"] is True
    assert result["risk_effect"]["gate_changed"] is True
