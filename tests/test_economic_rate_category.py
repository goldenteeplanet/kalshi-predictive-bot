import pytest

from kalshi_predictor.data.schema import Market
from kalshi_predictor.economic.linker import detect_economic_market


@pytest.mark.parametrize("hints", [None, {"KXU3": "jobs"}])
def test_unemployment_rate_is_not_a_fed_signal(hints) -> None:
    market = Market(
        ticker="KXU3-26AUG-T4.3",
        event_ticker="KXU3-26AUG",
        title="Will the unemployment rate (U-3) be above 4.3% in August?",
    )

    category, _, _ = detect_economic_market(market, series_category_hints=hints)

    assert category == "jobs"


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Will the crime rate fall?", None),
        ("Will rates rise?", None),
        ("Will the interest rate be above 4%?", "fed"),
        ("Will the Fed cut rates?", "fed"),
        ("Will there be a rate cut in July?", "fed"),
        ("Will there be two rate hikes this year?", "fed"),
    ],
)
def test_rate_word_requires_monetary_policy_context(title, expected) -> None:
    market = Market(ticker="EXAMPLE-26JUL", title=title)

    category, _, _ = detect_economic_market(market)

    assert category == expected
