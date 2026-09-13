from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.crypto.independent_evaluation import (
    PredictionRecord,
    evaluate_independent_models,
)

NOW = datetime(2026, 9, 10, tzinfo=UTC)
MODELS = ("market", "independent")


def row(model: str = "market", event: str = "event1", **kwargs) -> PredictionRecord:
    base = PredictionRecord(
        event_id=event, target_id="BTC-threshold", model=model, symbol="BTC", horizon="15 minute",
        decision_at=NOW, model_committed_at=NOW - timedelta(days=1),
        input_received_at=NOW, prediction_recorded_at=NOW,
        settlement_known_at=NOW + timedelta(minutes=15),
        prediction_sha256="a" * 64, settlement_sha256="b" * 64,
        probability=0.25, outcome=1,
    )
    return replace(base, **kwargs)


def evaluate(records):
    return evaluate_independent_models(records, models=MODELS, as_of=NOW + timedelta(hours=1))


def test_empty_cohort_is_unavailable_not_zero_metrics() -> None:
    result = evaluate([])
    assert result["status"] == "UNAVAILABLE_NO_COMMON_COHORT"
    assert result["models"]["market"] == {"status": "UNAVAILABLE", "independent_event_n": 0}


def test_paired_metrics_and_reference_quartiles() -> None:
    result = evaluate([
        row(), row("independent", probability=0.75),
        row(event="unpaired"),
    ])
    assert result["independent_event_n"] == 1
    assert result["excluded_unpaired_by_model"] == {"market": 1, "independent": 0}
    baseline = result["models"]["market"]
    assert baseline["brier"] == pytest.approx(0.5625)
    assert baseline["log_loss"] == pytest.approx(1.38629436112)
    assert baseline["ece_10_equal_width_bins"] == pytest.approx(0.75)
    assert baseline["positive_net_ev_frequency"] is None
    segment = result["segments"]["quartile"]["25-50"]
    assert segment["independent"]["independent_event_n"] == 1
    assert result["quartile_reference_model"] == "market"
    assert not result["promotion_authority"]


@pytest.mark.parametrize("field", [
    "model_committed_at", "input_received_at", "prediction_recorded_at",
])
def test_future_receipts_reject_backdated_decision(field: str) -> None:
    with pytest.raises(ValueError, match="FUTURE_RECEIPT_OR_SETTLEMENT_LEAKAGE"):
        evaluate([row(**{field: NOW + timedelta(seconds=1)}), row("independent")])


def test_known_outcome_and_future_settlement_are_not_evaluation_evidence() -> None:
    for timestamp in (NOW, NOW + timedelta(hours=2)):
        with pytest.raises(ValueError, match="FUTURE_RECEIPT_OR_SETTLEMENT_LEAKAGE"):
            evaluate([row(settlement_known_at=timestamp)])


@pytest.mark.parametrize("changes", [
    {"outcome": 0}, {"target_id": "different"}, {"symbol": "ETH"},
    {"horizon": "hourly"}, {"settlement_sha256": "c" * 64},
    {"decision_at": NOW + timedelta(seconds=1)},
])
def test_different_evidence_cannot_be_called_paired(changes) -> None:
    with pytest.raises(ValueError, match="UNPAIRED_EVENT_TARGET_OR_OUTCOME"):
        evaluate([row(), row("independent", **changes)])


def test_duplicate_event_cannot_inflate_independent_n() -> None:
    with pytest.raises(ValueError, match="DUPLICATE_MODEL_EVENT"):
        evaluate([row(), row(), row("independent")])


def test_ev_denominators_require_identical_explicit_cost_cohorts() -> None:
    costs = {"executable_price": 0.2, "fee": 0.01, "slippage": 0.01, "uncertainty": 0.04}
    result = evaluate([
        row(**costs), row("independent", probability=0.5, **costs),
        row(event="event2", **costs), row("independent", event="event2", executable_price=0.2),
        row(event="event3"), row("independent", event="event3", **costs),
    ])
    baseline = result["models"]["market"]
    assert baseline["independent_event_n"] == 3
    assert baseline["gross_ev_event_n"] == 2
    assert baseline["net_ev_event_n"] == 1
    assert baseline["positive_gross_ev_frequency"] == 1
    assert baseline["positive_net_ev_frequency"] == 0
    assert result["models"]["independent"]["positive_net_ev_frequency"] == 1


def test_no_side_and_infinite_log_loss_are_explicit() -> None:
    costs = {"side": "NO", "executable_price": 0.2, "fee": 0., "slippage": 0., "uncertainty": 0.}
    result = evaluate([row(probability=0., **costs), row("independent", probability=1., **costs)])
    assert result["models"]["market"]["log_loss"] is None
    assert result["models"]["market"]["log_loss_infinite"]
    assert result["models"]["market"]["positive_net_ev_frequency"] == 1
    assert result["models"]["independent"]["positive_net_ev_frequency"] == 0


@pytest.mark.parametrize("changes", [
    {"probability": float("nan")}, {"probability": 1.01}, {"outcome": True},
    {"fee": -0.01}, {"prediction_sha256": ""}, {"side": "unknown"},
])
def test_invalid_numerics_and_evidence_fail(changes) -> None:
    with pytest.raises(ValueError, match="INVALID_IDENTITY_PROBABILITY_COST_OR_PROVENANCE"):
        evaluate([row(**changes)])
