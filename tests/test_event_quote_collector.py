from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import kalshi_predictor.research.event_quote_collector as collector
from kalshi_predictor.data.schema import CryptoCurrentEvent
from kalshi_predictor.research.event_quote_collector import (
    EventCandidate,
    bucket_interval,
    capture_request_budget_reason,
    select_candidates_for_liquidity_window,
    select_candidates_with_fresh_forecasts,
    validate_topology,
)
from kalshi_predictor.research.liquidity_priority import family_key, family_yield_score


def _market(ticker, strike_type, floor=None, cap=None):
    return {
        "ticker": ticker,
        "strike_type": strike_type,
        "floor_strike": floor,
        "cap_strike": cap,
    }


def test_complete_tail_and_interior_topology_is_accepted():
    buckets, reasons = validate_topology(
        [
            _market("LOW", "less", cap=100),
            _market("MID", "between", floor=100, cap=110),
            _market("HIGH", "greater", floor=110),
        ]
    )
    assert reasons == []
    assert [row["kind"] for row in buckets] == ["lower_tail", "interior", "upper_tail"]


def test_missing_tail_and_gap_fail_closed():
    _, reasons = validate_topology(
        [
            _market("MID", "between", floor=100, cap=110),
            _market("HIGH", "greater", floor=120),
        ]
    )
    assert "LOWER_TAIL_MISSING" in reasons
    assert "BUCKET_GAP_OR_OVERLAP" in reasons


def test_bucket_parser_rejects_invalid_range():
    assert bucket_interval(_market("BAD", "between", floor=110, cap=100)) is None


def test_current_event_registry_has_required_composite_indexes():
    indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in CryptoCurrentEvent.__table__.indexes
    }
    assert indexes["ix_crypto_current_events_series_event_close"] == (
        "series_ticker",
        "event_ticker",
        "close_time",
    )
    assert indexes["ix_crypto_current_events_close_series"] == (
        "close_time",
        "series_ticker",
    )


def test_family_yield_score_prioritizes_coverage_and_narrower_events():
    strong = family_yield_score(
        average_coverage=0.90,
        bounds_failure_rate=0.0,
        coherence_failure_rate=0.0,
        bucket_count=20,
        observed_events=5,
    )
    sparse = family_yield_score(
        average_coverage=0.25,
        bounds_failure_rate=0.0,
        coherence_failure_rate=0.0,
        bucket_count=50,
        observed_events=5,
    )
    assert strong > sparse
    assert family_key("KXETH", 20) == "KXETH|buckets:LE25"


def test_liquidity_window_selects_in_window_and_falls_back():
    now = datetime.now(UTC)
    near = EventCandidate(
        "NEAR", "KXETH", "ETH", ({"close_time": (now + timedelta(hours=3)).isoformat()},)
    )
    far = EventCandidate(
        "FAR", "KXETH", "ETH", ({"close_time": (now + timedelta(hours=9)).isoformat()},)
    )
    policy = {
        "scheduling_enabled": True,
        "recommended_window": {"lower_hours": 2, "upper_hours": 4},
    }
    assert select_candidates_for_liquidity_window([far, near], policy, now=now) == [near]
    assert select_candidates_for_liquidity_window([far], policy, now=now) == [far]
    assert (
        select_candidates_for_liquidity_window(
            [far], policy, now=now, fallback_when_empty=False
        )
        == []
    )


def test_capture_candidates_require_fresh_point_in_time_forecast():
    now = datetime.now(UTC)
    fresh = EventCandidate("FRESH", "KXETH", "ETH", ())
    stale = EventCandidate("STALE", "KXETH", "ETH", ())

    class _Rows:
        def all(self):
            return [
                ("FRESH", now - timedelta(minutes=10)),
                ("STALE", now - timedelta(minutes=31)),
            ]

    class _Session:
        def execute(self, _statement):
            return _Rows()

    selected = select_candidates_with_fresh_forecasts(
        _Session(), [stale, fresh], now=now, max_lag_minutes=30
    )
    assert selected == [fresh]


def test_targeted_forecast_refresh_uses_interior_snapshot(monkeypatch):
    candidate = EventCandidate(
        "EVENT",
        "KXETH",
        "ETH",
        (
            _market("LOW", "less", cap=100),
            _market("MID", "between", floor=100, cap=110),
            _market("HIGH", "greater", floor=110),
        ),
    )

    class _Nested:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    class _Session:
        def begin_nested(self):
            return _Nested()

        def flush(self):
            return None

    class _Client:
        def get_orderbook(self, ticker):
            assert ticker == "MID"
            return {
                "orderbook_fp": {
                    "yes_dollars": [["0.40", "2"]],
                    "no_dollars": [],
                }
            }

    monkeypatch.setattr(
        collector,
        "insert_market_snapshot",
        lambda _session, market, _book, _time: SimpleNamespace(
            id=4, ticker=market["ticker"]
        ),
    )
    monkeypatch.setattr(
        collector,
        "link_crypto_markets",
        lambda _session, tickers, limit: SimpleNamespace(tickers=tickers, limit=limit),
    )
    monkeypatch.setattr(
        collector,
        "run_forecast_models",
        lambda _session, model_name, snapshots: SimpleNamespace(
            forecasts_inserted=int(model_name == "crypto_v2" and snapshots[0].ticker == "MID"),
            skipped=0,
        ),
    )
    result = collector.refresh_targeted_event_forecasts(
        _Session(), _Client(), [candidate], max_events=1
    )
    assert result["events_forecasted"] == 1
    assert result["midpoint_probe_failures"] == 1
    assert result["one_sided_bound_uses"] == 1
    assert result["rows"][0]["ticker"] == "MID"
    assert result["rows"][0]["family"] == "KXETH|buckets:LE25"
    assert result["rows"][0]["buckets_probed"] == 1
    assert result["rows"][0]["one_sided_bound_uses"] == 1


def test_targeted_forecast_forces_immediate_capture(monkeypatch):
    candidate = EventCandidate(
        "EVENT",
        "KXETH",
        "ETH",
        (
            _market("LOW", "less", cap=100),
            _market("MID", "between", floor=100, cap=110),
            _market("HIGH", "greater", floor=110),
        ),
    )

    class _Nested:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    class _Session:
        def begin_nested(self):
            return _Nested()

        def flush(self):
            return None

    class _Client:
        def get_orderbook(self, _ticker):
            return {"orderbook_fp": {"yes_dollars": [["0.40", "2"]]}}

    monkeypatch.setattr(
        collector,
        "insert_market_snapshot",
        lambda _session, market, _book, _time: SimpleNamespace(
            id=4, ticker=market["ticker"]
        ),
    )
    monkeypatch.setattr(collector, "link_crypto_markets", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        collector,
        "run_forecast_models",
        lambda *_args, **_kwargs: SimpleNamespace(forecasts_inserted=1, skipped=0),
    )
    monkeypatch.setattr(
        collector,
        "_capture_immediate_polytope",
        lambda *_args, **_kwargs: {
            "attempted": True,
            "coverage_id": 9,
            "within_latency_budget": True,
        },
    )
    result = collector.refresh_targeted_event_forecasts(
        _Session(), _Client(), [candidate], max_events=1, capture_immediately=True
    )
    assert result["immediate_captures_attempted"] == 1
    assert result["immediate_captures_within_budget"] == 1
    assert result["rows"][0]["immediate_capture"]["coverage_id"] == 9


def test_targeted_forecast_defers_before_forecast_when_capture_budget_is_exhausted():
    candidate = EventCandidate(
        "EVENT",
        "KXETH",
        "ETH",
        (
            _market("LOW", "less", cap=100),
            _market("MID", "between", floor=100, cap=110),
            _market("HIGH", "greater", floor=110),
        ),
    )
    result = collector.refresh_targeted_event_forecasts(
        SimpleNamespace(),
        SimpleNamespace(),
        [candidate],
        max_events=1,
        capture_immediately=True,
        capture_bucket_request_budget=2,
    )
    assert result["events_forecasted"] == 0
    assert result["capture_buckets_admitted"] == 0
    assert result["rows"][0]["reasons"] == [
        "CAPTURE_REQUEST_BUDGET_EXCEEDED_BEFORE_FORECAST"
    ]


def test_coherent_capture_rejects_fanout_larger_than_request_budget():
    candidate = EventCandidate(
        "EVENT",
        "KXETH",
        "ETH",
        tuple(_market(f"B{index}", "between", floor=index, cap=index + 1) for index in range(26)),
    )
    assert capture_request_budget_reason(candidate, bucket_request_budget=25) == (
        "CAPTURE_REQUEST_BUDGET_EXCEEDED"
    )
    assert capture_request_budget_reason(candidate, bucket_request_budget=26) is None
