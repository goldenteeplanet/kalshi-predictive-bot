import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.crypto.named_research_baselines import (
    BASELINE_MODULES,
    capture_named_baselines,
    select_exact_receipt,
)
from kalshi_predictor.crypto.research_provenance import freeze_prediction
from kalshi_predictor.forecasting.crypto_v3_independent import (
    CryptoTarget,
    PriceObservation,
    forecast_independent,
)
from kalshi_predictor.forecasting.market_implied import MarketImpliedForecaster

NOW = datetime(2026, 9, 10, 22, tzinfo=UTC)
TICKER = "KXSOL-TEST"


def encoded(obj):
    raw = json.dumps(obj).encode()
    return raw, hashlib.sha256(raw).hexdigest()


def arguments(*, book=False):
    market, digest = encoded(
        {
            "markets": [
                {
                    "ticker": TICKER,
                    "yes_bid_dollars": ".20",
                    "yes_ask_dollars": ".30",
                    "last_price_dollars": ".70",
                }
            ]
        }
    )
    args = dict(
        ticker=TICKER,
        market_original=market,
        market_sha256=digest,
        market_received_at=NOW - timedelta(seconds=2),
        model_input_as_of=NOW,
    )
    if book:
        raw, sha = encoded(
            {"orderbook_fp": {"yes_dollars": [[".40", "2"]], "no_dollars": [[".50", "3"]]}}
        )
        args.update(
            orderbook_original=raw,
            orderbook_sha256=sha,
            orderbook_received_at=NOW - timedelta(seconds=1),
            orderbook_url=f"https://api.elections.kalshi.com/trade-api/v2/markets/{TICKER}/orderbook?depth=5",
        )
    return args


def test_nested_event_original_baseline_keeps_original_hash():
    args = arguments(book=True)
    args["ticker"] = "KXBTC-EVENT-STRIKE"
    rows = json.loads(args["market_original"])["markets"]
    rows[0].update(ticker=args["ticker"], event_ticker="KXBTC-EVENT")
    raw, digest = encoded(
        dict(events=[dict(series_ticker="KXBTC", event_ticker="KXBTC-EVENT", markets=rows)])
    )
    args.update(
        market_original=raw,
        market_sha256=digest,
        orderbook_url="https://external-api.kalshi.com/trade-api/v2/markets/KXBTC-EVENT-STRIKE/orderbook?depth=5",
    )
    result = capture_named_baselines(**args)["market_implied_v1"]
    assert result["probability"] == 0.45
    assert result["input_provenance"]["market_sha256"] == digest
    altered = json.loads(raw)
    altered["events"][0]["markets"][0]["event_ticker"] = "KXBTC-OTHER"
    args["market_original"], args["market_sha256"] = encoded(altered)
    with pytest.raises(ValueError, match="MARKET_EVENT_MEMBERSHIP"):
        capture_named_baselines(**args)


@pytest.mark.parametrize(
    "book,p,source", [(False, 0.25, "market_quote_midpoint"), (True, 0.45, "orderbook_midpoint")]
)
def test_actual_named_baseline_branch_and_exact_input_receipt_binding(book, p, source):
    args = arguments(book=book)
    result = capture_named_baselines(**args)
    baseline = result["market_implied_v1"]
    assert baseline["probability"] == p and baseline["source"] == source
    assert baseline["model_invoked"]
    assert baseline["input_provenance"]["market_sha256"] == args["market_sha256"]
    assert baseline["input_provenance"]["model_input_as_of"] == NOW.isoformat()
    assert not baseline["execution_liquidity_verified"]
    assert not result["crypto_v2"]["model_invoked"]
    assert result["crypto_v2"]["probability"] is None
    assert result["crypto_v2"]["status"] == "UNAVAILABLE_MISSING_CAPTURED_INPUTS"
    assert MarketImpliedForecaster.__module__ in BASELINE_MODULES


@pytest.mark.parametrize(
    "change,error",
    [
        ({"market_sha256": "0" * 64}, "HASH_MISMATCH"),
        ({"market_received_at": NOW + timedelta(seconds=1)}, "FUTURE_RECEIPT"),
        ({"ticker": "WRONG"}, "EXACT_MARKET_IDENTITY"),
        ({"model_input_as_of": NOW.replace(tzinfo=None)}, "AWARE_RECEIPTS"),
    ],
)
def test_original_identity_hash_and_clock_required(change, error):
    args = arguments()
    args.update(change)
    with pytest.raises(ValueError, match=error):
        capture_named_baselines(**args)


@pytest.mark.parametrize(
    "change,error",
    [
        ({"orderbook_sha256": "0" * 64}, "HASH_MISMATCH"),
        ({"orderbook_received_at": NOW + timedelta(seconds=1)}, "FUTURE_RECEIPT"),
        (
            {
                "orderbook_url": "https://api.elections.kalshi.com/trade-api/v2/markets/OTHER/orderbook"
            },
            "REQUEST_IDENTITY",
        ),
        ({"orderbook_received_at": None}, "PARTIAL_BOOK"),
    ],
)
def test_book_binding_is_complete_and_for_same_market(change, error):
    args = arguments(book=True)
    args.update(change)
    with pytest.raises(ValueError, match=error):
        capture_named_baselines(**args)


def test_baseline_does_not_change_independent_forecast_and_is_frozen_before_decision(tmp_path):
    prices = [
        PriceObservation(
            100 + (i % 3) * 0.1, NOW - timedelta(minutes=60 - i), NOW, "coinbase", "a" * 64, "SOL"
        )
        for i in range(61)
    ]
    target = CryptoTarget("SOL", "ABOVE", NOW + timedelta(minutes=30), threshold=100)
    independent = forecast_independent(prices, target, decision_at=NOW)
    original = copy.deepcopy(independent)
    baselines = capture_named_baselines(**arguments(book=True))
    assert independent == original
    bundle = {
        "forecast": independent,
        "named_baselines": baselines,
        "code_dependencies": BASELINE_MODULES,
    }
    clocks = iter([NOW + timedelta(seconds=1), NOW + timedelta(seconds=2)])
    receipt = freeze_prediction(
        tmp_path / "prediction",
        bundle,
        model_input_as_of=NOW,
        input_received_at=NOW,
        model_committed_at=NOW - timedelta(minutes=1),
        target_at=target.observation_at,
        clock=lambda: next(clocks),
    )
    raw = (tmp_path / "prediction/prediction.json").read_bytes()
    frozen = json.loads(raw)["prediction"]
    assert frozen["forecast"] == json.loads(json.dumps(original, default=str))
    assert frozen["named_baselines"]["market_implied_v1"]["probability"] == 0.45
    assert receipt["prediction_recorded_at"] <= receipt["decision_at"]
    assert hashlib.sha256(raw).hexdigest() == receipt["prediction_sha256"]


def test_zero_size_listing_fallback_is_actual_model_not_execution_claim():
    args = arguments()
    raw, sha = encoded(
        {
            "markets": [
                {
                    "ticker": TICKER,
                    "yes_bid_dollars": "0",
                    "yes_ask_dollars": ".01",
                    "yes_bid_size_fp": "0",
                }
            ]
        }
    )
    args.update(market_original=raw, market_sha256=sha)
    baseline = capture_named_baselines(**args)["market_implied_v1"]
    assert baseline["probability"] == 0.005
    assert not baseline["execution_liquidity_verified"]


def test_identical_book_bytes_keep_distinct_ticker_receipt_bindings():
    raw, digest = encoded({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}})
    receipts = [
        {
            "url": f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook?depth=5",
            "sha256": digest,
            "received_at": (NOW - timedelta(seconds=2 - i)).isoformat(),
            "path": ticker + "-book.json",
        }
        for i, ticker in enumerate(("FIRST", TICKER))
    ]
    receipt = select_exact_receipt(receipts, sha256=digest, request_url=receipts[1]["url"])
    assert receipt == receipts[1]
    args = arguments()
    args.update(
        orderbook_original=raw,
        orderbook_sha256=digest,
        orderbook_received_at=datetime.fromisoformat(receipt["received_at"]),
        orderbook_url=receipt["url"],
    )
    result = capture_named_baselines(**args)["market_implied_v1"]
    assert result["input_provenance"]["orderbook_url"] == receipts[1]["url"]
    assert result["input_provenance"]["orderbook_received_at"] == receipts[1]["received_at"]
    assert result["probability"] == 0.25


@pytest.mark.parametrize("receipts", [[], [{"sha256": "a", "url": "u"}] * 2])
def test_exact_receipt_selector_rejects_missing_or_ambiguous_record(receipts):
    with pytest.raises(ValueError, match="EXACT_REQUEST_RECEIPT"):
        select_exact_receipt(receipts, sha256="a", request_url="u")


@pytest.mark.parametrize(
    "url",
    [
        f"https://api.elections.kalshi.com:8443/trade-api/v2/markets/{TICKER}/orderbook?depth=5",
        f"https://api.elections.kalshi.com:443/trade-api/v2/markets/{TICKER}/orderbook?depth=5",
        f"https://user@api.elections.kalshi.com/trade-api/v2/markets/{TICKER}/orderbook?depth=5",
        f"https://api.elections.kalshi.com/trade-api/v2/markets/{TICKER}/orderbook?depth=5#other",
    ],
)
def test_book_provenance_rejects_port_userinfo_or_fragment(url):
    args = arguments(book=True)
    args["orderbook_url"] = url
    with pytest.raises(ValueError, match="REQUEST_IDENTITY"):
        capture_named_baselines(**args)
