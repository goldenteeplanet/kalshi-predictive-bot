import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from kalshi_predictor.data.schema import (
    Base,
    CryptoFeature,
    Market,
    MarketSnapshot,
    PaperFill,
    PaperOrder,
    ProspectivePairedCapture,
    ProspectivePairEvaluation,
    Settlement,
)
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.forecasting.crypto_v2 import (
    CryptoV2Forecaster,
    _component_feature_rows,
)
from kalshi_predictor.phase4cd.prospective import (
    _candidate_category,
    _comparator_lineage,
    _event_round_robin,
    _research_skip_recorder,
    _strict_feature_bundle,
    capture_prospective_pairs,
    prospective_status,
    reconcile_prospective_pairs,
    watch_canonical_settlements,
)
from kalshi_predictor.phase4cd.range_comparator import audit_range_comparator
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session


def test_event_round_robin_is_deterministic_and_outcome_blind() -> None:
    rows = [
        (SimpleNamespace(id=index), SimpleNamespace(event_ticker=event, ticker=event))
        for index, event in enumerate(["A", "A", "A", "B", "B", "C"])
    ]
    selected = _event_round_robin(rows, 4)
    assert [(row[1].event_ticker, row[0].id) for row in selected] == [
        ("A", 0),
        ("B", 3),
        ("C", 5),
        ("A", 1),
    ]


@pytest.mark.parametrize(
    ("event_ticker", "series_ticker", "raw_json", "expected"),
    [
        ("KXBTC-26AUG", "KXBTC", "{}", "EXACT_CRYPTO_CANDIDATE"),
        ("KXRAINAUSM-26AUG", "KXRAINAUSM", "{}", "CONFIRMED_NON_CRYPTO_CATEGORY"),
        ("KXADA-26AUG", None, '{"category":"crypto"}', "UNSUPPORTED_CRYPTO_ASSET"),
        ("KXUNKNOWN-26AUG", None, "{}", "AMBIGUOUS_CATEGORY"),
    ],
)
def test_candidate_category_is_structured_and_fail_closed(
    event_ticker: str,
    series_ticker: str | None,
    raw_json: str,
    expected: str,
) -> None:
    market = SimpleNamespace(
        ticker=f"{event_ticker}-T1",
        event_ticker=event_ticker,
        series_ticker=series_ticker,
        raw_json=raw_json,
    )
    result = _candidate_category(market)
    assert result["verdict"] == expected
    assert result["title_consulted"] is False
    assert result["settlement_consulted"] is False
    assert result["crypto_link_required"] is False
    assert len(result["classification_hash"]) == 64


def _sessions() -> tuple[Session, Session]:
    research_engine = create_engine("sqlite:///:memory:")
    source_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(research_engine)
    Base.metadata.create_all(source_engine)
    return Session(research_engine), Session(source_engine)


def _feature(source: Session, generated_at: datetime, observed_at: datetime) -> CryptoFeature:
    row = CryptoFeature(
        symbol="BTC",
        source="test",
        generated_at=generated_at,
        window_minutes=60,
        momentum_score="0.2",
        trend_direction="up",
        raw_json='{"history_minutes":60,"source_observation_ref":{"observed_at":"'
        + observed_at.isoformat()
        + '"}}',
        created_at=generated_at,
    )
    source.add(row)
    source.flush()
    return row


def _crypto_feature_json(feature_id: int) -> dict[str, object]:
    return {
        "component_feature_ids": {"BTC": feature_id},
        "title": "BTC above",
        "structured_terms": {"components": [{"symbol": "BTC", "comparator": "ABOVE"}]},
        "direction_detected": "ABOVE",
        "momentum_score": "0.125",
        "adjustment": "0.01",
        "market_price_anchor": "0.42",
        "market_probability_bounds": {"lower": "0.40", "upper": "0.44"},
        "final_probability": "0.43",
    }


def test_zero_skew_rejects_feature_after_cutoff() -> None:
    _, source = _sessions()
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    feature = _feature(source, cutoff + timedelta(microseconds=1), cutoff)
    with pytest.raises(ValueError, match="FEATURE_SOURCE_AFTER_CUTOFF"):
        _strict_feature_bundle(source, {"component_feature_ids": {"BTC": feature.id}}, cutoff)


def test_source_observation_must_precede_feature() -> None:
    _, source = _sessions()
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    feature = _feature(source, cutoff - timedelta(seconds=2), cutoff - timedelta(seconds=1))
    with pytest.raises(ValueError, match="SOURCE_AFTER_FEATURE"):
        _strict_feature_bundle(source, {"component_feature_ids": {"BTC": feature.id}}, cutoff)


def test_exact_snapshot_pair_is_idempotent_and_paper_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    research, source = _sessions()
    cutoff = datetime(2026, 1, 1, 12, tzinfo=UTC)
    feature = _feature(source, cutoff - timedelta(seconds=1), cutoff - timedelta(seconds=2))
    source.add(
        Market(
            ticker="KXBTC-TEST-T1",
            event_ticker="KXBTC-TEST",
            series_ticker="KXBTC",
            title="BTC above",
            status="open",
            liquidity_dollars="100",
            raw_json="{}",
            first_seen_at=cutoff,
            last_seen_at=cutoff,
        )
    )
    source.add(
        MarketSnapshot(
            id=1,
            ticker="KXBTC-TEST-T1",
            captured_at=cutoff,
            status="open",
            yes_bid_dollars="0.40",
            yes_ask_dollars="0.44",
            best_yes_bid="0.40",
            best_yes_ask="0.44",
            spread="0.04",
            raw_market_json='{"yes_bid_dollars":"0.40","yes_ask_dollars":"0.44"}',
            raw_orderbook_json=None,
        )
    )
    source.commit()

    def fake_crypto(self: object, session: Session, snapshot: MarketSnapshot) -> ForecastOutput:
        return ForecastOutput(
            ticker=snapshot.ticker,
            forecasted_at=snapshot.captured_at,
            model_name="crypto_v2",
            yes_probability=Decimal("0.43"),
            market_mid_probability=Decimal("0.42"),
            best_yes_bid=Decimal("0.40"),
            best_yes_ask=Decimal("0.44"),
            feature_json=_crypto_feature_json(feature.id),
        )

    monkeypatch.setattr(
        "kalshi_predictor.phase4cd.prospective.CryptoV2Forecaster.forecast", fake_crypto
    )
    before = (
        source.scalar(select(func.count()).select_from(PaperOrder)),
        source.scalar(select(func.count()).select_from(PaperFill)),
    )
    first = capture_prospective_pairs(research, source, now=cutoff + timedelta(seconds=1))
    second = capture_prospective_pairs(research, source, now=cutoff + timedelta(seconds=1))
    after = (
        source.scalar(select(func.count()).select_from(PaperOrder)),
        source.scalar(select(func.count()).select_from(PaperFill)),
    )
    assert first.captured == 1
    assert second.captured == 1
    assert research.scalar(select(func.count()).select_from(ProspectivePairedCapture)) == 1
    pair = research.scalar(select(ProspectivePairedCapture))
    assert pair is not None
    assert pair.snapshot_timestamp.replace(tzinfo=UTC) == cutoff
    lineage = json.loads(pair.comparator_lineage_json or "null")
    assert lineage["component_directions"] == [{"comparator": "ABOVE", "symbol": "BTC"}]
    assert lineage["fallback_direction"] == "ABOVE"
    assert lineage["final_bounded_adjustment"] == "0.01"
    assert lineage["bound_clipped"] is False
    range_verdict = json.loads(pair.range_comparator_verdict_json or "null")
    assert range_verdict["verdict"] == "RANGE_COMPARATOR_REJECTED"
    assert range_verdict["model_version"] is None
    assert json.loads(pair.model_versions_json) == {
        "crypto": "crypto_v2",
        "market": "market_implied_v1",
    }
    assert before == after == (0, 0)
    status = prospective_status(research)
    assert status["performance_metrics_merged"] is False
    assert status["unsettled_pairs"] == 1
    assert status["concentration"]["by_asset"] == {"KXBTC": 1}
    assert len(status["cohort_growth"]) == 1
    assert status["cohort_growth"][0]["new_events"] == 1
    assert status["cohort_growth"][0]["repeated_events"] == 0
    assert status["cohort_growth"][0]["assets"] == {"KXBTC": 1}
    assert status["range_comparator_identifiability"]["forward_verdict_rows"] == 1
    assert status["range_comparator_identifiability"]["research_model_deployed"] is False

    source.add(
        Settlement(
            ticker="KXBTC-TEST-T1",
            settled_at=cutoff + timedelta(hours=1),
            result="yes",
            yes_settlement_value="1",
            raw_json='{"result":"yes"}',
            updated_at=cutoff + timedelta(hours=1),
        )
    )
    source.commit()
    reconciled = reconcile_prospective_pairs(research, source)
    assert reconciled["settled_pairs"] == 1
    assert reconciled["matched_identical_time_rows"] == 1
    assert reconciled["paired_event_bootstrap"] == {
        "resampling_unit": "independent_event",
        "seed": 404019,
        "resamples": 5000,
        "minimum_events": 30,
        "permitted": False,
        "effective_event_n": 1,
        "reason": "INSUFFICIENT_INDEPENDENT_EVENTS",
        "intervals": None,
    }
    assert reconciled["probability_comparison"] == {
        "all_rows_equal": 0,
        "all_rows_different": 1,
        "settled_rows_equal": 0,
        "settled_rows_different": 1,
        "max_absolute_delta": "0.01",
        "diagnosis": "IMMUTABLE_CAPTURED_PROBABILITIES_DIFFER",
    }
    evaluation = research.scalar(select(ProspectivePairEvaluation))
    assert evaluation is not None
    assert len(evaluation.settlement_hash) == 64
    watch = watch_canonical_settlements(research, source)
    assert watch["scope"] == "PREVIOUSLY_CAPTURED_TICKERS_ONLY"
    assert watch["captured_tickers"] == 1
    assert watch["canonical_settlements"] == 1

    settlement = source.get(Settlement, "KXBTC-TEST-T1")
    assert settlement is not None
    settlement.raw_json = '{"result":"yes","changed":true}'
    settlement.updated_at = cutoff + timedelta(hours=2)
    source.commit()
    conflict = reconcile_prospective_pairs(research, source)
    assert conflict["batch_reasons"] == {"SETTLEMENT_LINEAGE_CONFLICT": 1}
    with pytest.raises(RuntimeError, match="SETTLEMENT_LINEAGE_CONFLICT"):
        watch_canonical_settlements(research, source)


def test_research_skip_recorder_is_exact_and_non_writing() -> None:
    _, source = _sessions()
    cutoff = datetime(2026, 1, 1, 12, tzinfo=UTC)
    snapshot = SimpleNamespace(id=7, captured_at=cutoff)
    records: list[dict[str, object]] = []
    recorder = _research_skip_recorder(records, snapshot=snapshot)

    recorder(
        source,
        model_name="crypto_v2",
        ticker="T7",
        reason="no crypto features",
        required_data=["crypto features"],
        available_data={"symbol": "BTC", "feature_id": 11},
    )

    assert not source.new
    assert records[0]["reason"] == "no crypto features"
    assert records[0]["snapshot_id"] == 7
    assert records[0]["available_data"] == {"symbol": "BTC", "feature_id": 11}
    assert len(str(records[0]["required_data_hash"])) == 64
    assert len(str(records[0]["available_data_hash"])) == 64


def test_comparator_lineage_records_neutrality_and_bound_clipping() -> None:
    payload = _crypto_feature_json(1)
    payload["structured_terms"] = {"components": [{"symbol": "BTC", "comparator": "UNKNOWN"}]}
    payload["direction_detected"] = "UNKNOWN"
    payload["momentum_score"] = "0.0000"
    payload["adjustment"] = "0.04"
    payload["final_probability"] = "0.44"

    lineage = _comparator_lineage(payload)

    assert lineage["component_directions"] == [{"symbol": "BTC", "comparator": "UNKNOWN"}]
    assert lineage["fallback_direction"] == "UNKNOWN"
    assert lineage["signed_momentum"] == "0.0000"
    assert lineage["bound_clipped"] is True
    assert lineage["final_bounded_adjustment"] == "0.02"
    assert len(str(lineage["raw_title_hash"])) == 64
    assert len(str(lineage["structured_terms_hash"])) == 64
    assert lineage == _comparator_lineage(payload)


def test_research_crypto_selector_threads_zero_future_skew(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, source = _sessions()
    observed: list[int] = []

    def fake_select(*_args: object, **kwargs: object) -> SimpleNamespace:
        observed.append(int(kwargs["future_skew_seconds"]))
        return SimpleNamespace(ok=False, feature=None, reason="no_feature", details={})

    monkeypatch.setattr(
        "kalshi_predictor.forecasting.crypto_v2.select_compatible_crypto_feature",
        fake_select,
    )
    terms = SimpleNamespace()
    snapshot = SimpleNamespace(ticker="T", captured_at=datetime(2026, 1, 1, tzinfo=UTC))

    _component_feature_rows(
        source,
        [{"symbol": "BTC", "direction": "ABOVE"}],
        terms=terms,
        snapshot=snapshot,
        future_skew_seconds=0,
    )

    assert observed == [0]
    assert CryptoV2Forecaster().future_skew_seconds == 30
    assert CryptoV2Forecaster(future_skew_seconds=0).future_skew_seconds == 0


def test_range_audit_rejects_ambiguous_inclusivity_and_source_mismatch() -> None:
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    verdict = audit_range_comparator(
        raw_market={"strike_type": "between", "floor_strike": 90, "cap_strike": 110},
        structured_terms={
            "reference_price_source": "cf_benchmarks",
            "components": [{"symbol": "BTC", "comparator": "RANGE"}],
        },
        feature={
            "source": "stored_prices",
            "generated_at": cutoff - timedelta(seconds=1),
            "price": "100",
            "volatility_1h": "0.001",
            "raw_json": {
                "feature_version": "v1",
                "volatility_unit": "simple_return_per_sqrt_minute",
            },
        },
        cutoff=cutoff,
        settlement_target=cutoff + timedelta(hours=1),
    )
    assert verdict["verdict"] == "RANGE_COMPARATOR_REJECTED"
    assert verdict["candidate_probability"] is None
    assert verdict["reason_codes"] == [
        "CONTRACT_TIMEZONE_MISSING",
        "INCLUSIVITY_UNPROVEN",
        "OBSERVATION_WINDOW_MISSING",
        "REFERENCE_PRICE_SOURCE_MISMATCH",
        "REFERENCE_SOURCE_RIGHTS_UNPROVEN",
        "SETTLEMENT_INDEX_IDENTIFIER_MISSING",
        "VOLATILITY_TRANSFORMATION_INCOMPATIBLE",
    ]
    assert len(verdict["verdict_hash"]) == 64


def test_range_audit_is_deterministic_and_bounded_when_identifiable() -> None:
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    kwargs = {
        "raw_market": {
            "strike_type": "between",
            "floor_strike": 90,
            "cap_strike": 110,
            "lower_bound_inclusive": True,
            "upper_bound_inclusive": True,
            "observation_window_seconds": 60,
            "settlement_index_identifier": "XBTUSD_RTI",
            "settlement_timezone": "America/New_York",
        },
        "structured_terms": {
            "reference_price_source": "cf_benchmarks",
            "components": [{"symbol": "BTC", "comparator": "RANGE"}],
        },
        "feature": {
            "source": "cf_benchmarks",
            "generated_at": cutoff - timedelta(seconds=1),
            "price": "100",
            "volatility_1h": "0.001",
            "raw_json": {
                "feature_version": "v1",
                "volatility_unit": "log_return_per_sqrt_minute",
            },
        },
        "cutoff": cutoff,
        "settlement_target": cutoff + timedelta(hours=1),
        "authorized_reference_sources": frozenset({"cf_benchmarks"}),
    }
    first = audit_range_comparator(**kwargs)
    assert first == audit_range_comparator(**kwargs)
    assert first["verdict"] == "RANGE_COMPARATOR_IDENTIFIABLE"
    assert first["model_version"] == "range_distribution_v1_research"
    assert Decimal("0") < Decimal(first["candidate_probability"]) < Decimal("1")


def test_range_audit_enforces_zero_skew() -> None:
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    verdict = audit_range_comparator(
        raw_market={
            "strike_type": "between",
            "floor_strike": 90,
            "cap_strike": 110,
            "lower_bound_inclusive": True,
            "upper_bound_inclusive": True,
            "observation_window_seconds": 60,
            "settlement_index_identifier": "XBTUSD_RTI",
            "settlement_timezone": "America/New_York",
        },
        structured_terms={
            "reference_price_source": "cf_benchmarks",
            "components": [{"symbol": "BTC", "comparator": "RANGE"}],
        },
        feature={
            "source": "cf_benchmarks",
            "generated_at": cutoff + timedelta(microseconds=1),
            "price": "100",
            "volatility_1h": "0.001",
            "raw_json": {
                "feature_version": "v1",
                "volatility_unit": "log_return_per_sqrt_minute",
            },
        },
        cutoff=cutoff,
        settlement_target=cutoff + timedelta(hours=1),
        authorized_reference_sources=frozenset({"cf_benchmarks"}),
    )
    assert "FEATURE_SOURCE_AFTER_CUTOFF" in verdict["reason_codes"]
    assert verdict["candidate_probability"] is None


def test_range_audit_rights_gate_is_fail_closed() -> None:
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    verdict = audit_range_comparator(
        raw_market={
            "strike_type": "between",
            "floor_strike": 90,
            "cap_strike": 110,
            "lower_bound_inclusive": True,
            "upper_bound_inclusive": True,
            "observation_window_seconds": 60,
            "settlement_index_identifier": "XBTUSD_RTI",
            "settlement_timezone": "America/New_York",
        },
        structured_terms={
            "reference_price_source": "cf_benchmarks",
            "components": [{"symbol": "BTC", "comparator": "RANGE"}],
        },
        feature={
            "source": "cf_benchmarks",
            "generated_at": cutoff - timedelta(seconds=1),
            "price": "100",
            "volatility_1h": "0.001",
            "raw_json": {
                "feature_version": "v1",
                "volatility_unit": "log_return_per_sqrt_minute",
            },
        },
        cutoff=cutoff,
        settlement_target=cutoff + timedelta(hours=1),
    )
    assert verdict["reason_codes"] == ["REFERENCE_SOURCE_RIGHTS_UNPROVEN"]
    assert verdict["model_version"] is None


def test_paired_event_bootstrap_is_deterministic_at_thirty_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    research, source = _sessions()
    cutoff = datetime(2026, 1, 1, 12, tzinfo=UTC)
    feature = _feature(source, cutoff - timedelta(seconds=1), cutoff - timedelta(seconds=2))
    for index in range(30):
        ticker = f"KXBTC-E{index:02d}-T1"
        source.add(
            Market(
                ticker=ticker,
                event_ticker=f"KXBTC-E{index:02d}",
                series_ticker="KXBTC",
                title="BTC above",
                status="open",
                close_time=cutoff + timedelta(hours=1),
                liquidity_dollars="100",
                raw_json="{}",
                first_seen_at=cutoff,
                last_seen_at=cutoff,
            )
        )
        source.add(
            MarketSnapshot(
                id=index + 1,
                ticker=ticker,
                captured_at=cutoff,
                status="open",
                yes_bid_dollars="0.40",
                yes_ask_dollars="0.44",
                best_yes_bid="0.40",
                best_yes_ask="0.44",
                spread="0.04",
                raw_market_json='{"yes_bid_dollars":"0.40","yes_ask_dollars":"0.44"}',
                raw_orderbook_json=None,
            )
        )
    source.commit()

    def fake_crypto(self: object, session: Session, snapshot: MarketSnapshot) -> ForecastOutput:
        return ForecastOutput(
            ticker=snapshot.ticker,
            forecasted_at=snapshot.captured_at,
            model_name="crypto_v2",
            yes_probability=Decimal("0.43"),
            market_mid_probability=Decimal("0.42"),
            best_yes_bid=Decimal("0.40"),
            best_yes_ask=Decimal("0.44"),
            feature_json=_crypto_feature_json(feature.id),
        )

    monkeypatch.setattr(
        "kalshi_predictor.phase4cd.prospective.CryptoV2Forecaster.forecast", fake_crypto
    )
    captured = capture_prospective_pairs(research, source, now=cutoff + timedelta(seconds=1))
    assert captured.captured == 30
    for index in range(30):
        source.add(
            Settlement(
                ticker=f"KXBTC-E{index:02d}-T1",
                settled_at=cutoff + timedelta(hours=1),
                result="yes" if index % 2 else "no",
                yes_settlement_value="1" if index % 2 else "0",
                raw_json="{}",
                updated_at=cutoff + timedelta(hours=1),
            )
        )
    source.commit()
    reconcile_prospective_pairs(research, source)

    first = prospective_status(research)["paired_event_bootstrap"]
    second = prospective_status(research)["paired_event_bootstrap"]
    assert first == second
    assert first["permitted"] is True
    assert first["effective_event_n"] == 30
    assert first["resampling_unit"] == "independent_event"
    assert first["seed"] == 404019
    assert set(first["intervals"]) == {
        "brier_delta_crypto_minus_market",
        "log_loss_delta_crypto_minus_market",
    }
