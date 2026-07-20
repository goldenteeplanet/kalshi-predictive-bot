from decimal import Decimal

from kalshi_predictor.kalshi.orderbook import LocalOrderbook
from kalshi_predictor.phase_gh1k import depth_notional, preview_liquidity_score
from kalshi_predictor.opportunities.scanner import top5_orderbook_notional


def test_gh1k_preview_adds_depth_without_changing_existing_scoring_function() -> None:
    book = LocalOrderbook("KXBTC-TEST")
    book.apply_rest_snapshot({"orderbook_fp": {"yes_dollars": [["0.40", "10"]],
                                                "no_dollars": [["0.50", "10"]]}}, resume_sequence=0)
    notional = depth_notional(book)
    result = preview_liquidity_score(volume="0", open_interest="0", market_liquidity="0",
                                     orderbook_depth_notional=notional)
    assert notional == Decimal("9.00")
    assert result["current_score"] == "0.00"
    assert Decimal(result["preview_score"]) > 0


def test_ranking_liquidity_uses_exact_top5_orderbook_notional() -> None:
    raw = '{"orderbook_fp":{"yes_dollars":[["0.40","10"]],"no_dollars":[["0.50","10"]]}}'
    assert top5_orderbook_notional(ticker="KXBTC-TEST", raw_orderbook_json=raw) == Decimal("9.00")
