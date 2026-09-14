from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshi_predictor.crypto.cost_evidence import (
    CostEvidenceStatus,
    OriginalBook,
    observed_one_contract_stress,
)

AT = datetime(2026, 9, 12, 12, tzinfo=UTC)
URL = "https://external-api.kalshi.com/trade-api/v2/markets/KXSOLE-TEST/orderbook"
RAW = b'{"orderbook_fp":{"yes_dollars":[["0.4","2"]],"no_dollars":[["0.5","2"]]}}'


def assess(first=None, second=None, side="YES", price=Decimal(".5")):
    return observed_one_contract_stress(
        ticker="KXSOLE-TEST", side=side, executable_price=price,
        originals=(first or OriginalBook(URL, RAW, AT),
                   second or OriginalBook(URL, RAW, AT + timedelta(seconds=1))),
        decision_at=AT,
    )


def test_measured_zero_is_not_expected_slippage_certification():
    result = assess()
    assert result.value == 0
    assert result.status == CostEvidenceStatus.ESTIMATED_WITH_SUPPORT
    assert not result.paper_support
    assert "POSTDECISION_BOOK_DIAGNOSTIC_ONLY" in result.blockers
    assert "EXPECTED_SLIPPAGE_EXECUTION_MODEL_NOT_CALIBRATED" in result.blockers
    assert result.evidence_sources[0] == (URL, OriginalBook(URL, RAW, AT).sha256)


def test_adverse_movement_is_reconstructed():
    second = OriginalBook(URL, RAW.replace(b'"0.5"', b'"0.47"'), AT + timedelta(seconds=1))
    assert assess(second=second).value == Decimal(".03")


def test_no_side_uses_yes_bid_complement():
    assert assess(side="NO", price=Decimal(".6")).value == 0


@pytest.mark.parametrize("payload", [
    RAW.replace(b'"0.5"', b'"NaN"'),
    RAW.replace(b'"0.5"', b'0.5'),
    RAW.replace(b'[["0.5","2"]]', b'[["0.5","2"],["0.5","3"]]'),
    RAW.replace(b'"0.5"', b'"0.7"'),
    b'{"orderbook_fp":{},"orderbook_fp":{}}',
    RAW.replace(b'"yes_dollars"', b'"yes"'),
])
def test_invalid_originals_cannot_get_numeric_cost(payload):
    with pytest.raises(ValueError):
        assess(first=OriginalBook(URL, payload, AT))


def test_wrong_market_or_clock_or_price_is_rejected():
    original = OriginalBook(URL, RAW, AT)
    with pytest.raises(ValueError, match="EXACT_BOOK"):
        assess(first=replace(original, url=URL.replace("TEST", "OTHER")))
    with pytest.raises(ValueError, match="RECEIPT_ORDER"):
        assess(second=original)
    with pytest.raises(ValueError, match="PRICE_MISMATCH"):
        assess(price=Decimal(".49"))


def test_insufficient_depth_remains_unknown():
    original = OriginalBook(URL, RAW.replace(b'"2"', b'"0.5"'), AT)
    result = assess(first=original)
    assert result.value is None
    assert result.status == CostEvidenceStatus.UNKNOWN
    assert not result.paper_support


def test_original_depth_query_and_last_predecision_price():
    first = OriginalBook(URL + "?depth=100", RAW, AT - timedelta(seconds=1))
    last = OriginalBook(URL + "?depth=100", RAW.replace(b'"0.5"', b'"0.47"'), AT)
    result = assess(first=first, second=last, price=Decimal(".53"))
    assert result.value == Decimal(".03")
    assert "POSTDECISION_BOOK_DIAGNOSTIC_ONLY" not in result.blockers
    assert not result.paper_support
    with pytest.raises(ValueError, match="PRICE_MISMATCH"):
        assess(first=first, second=last, price=Decimal(".5"))


@pytest.mark.parametrize("query", ["?depth=0", "?depth=1001", "?depth=1&depth=2", "?ticker=X"])
def test_unreviewed_query_cannot_change_book_scope(query):
    with pytest.raises(ValueError, match="EXACT_BOOK"):
        assess(first=OriginalBook(URL + query, RAW, AT))
