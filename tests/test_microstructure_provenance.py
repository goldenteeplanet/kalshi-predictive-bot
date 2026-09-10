import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.data.schema import (
    Base,
    Forecast,
    Market,
    MarketSnapshot,
    MicrostructureFeature,
)
from kalshi_predictor.forecasting.microstructure_v1 import MicrostructureV1Forecaster
from kalshi_predictor.microstructure.orderbook_features import compute_microstructure_feature
from kalshi_predictor.microstructure.provenance import (
    FeatureBuildContext,
    RecordReceipt,
    record_hash,
)
from kalshi_predictor.microstructure.repository import insert_microstructure_feature


@pytest.fixture
def sample(monkeypatch):
    cutoff = datetime(2026, 9, 10, 21, tzinfo=UTC)
    now = cutoff + timedelta(seconds=3)
    monkeypatch.setattr("kalshi_predictor.microstructure.orderbook_features.utc_now", lambda: now)
    monkeypatch.setattr(
        "kalshi_predictor.forecasting.microstructure_v1.utc_now", lambda: now + timedelta(seconds=1)
    )
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            Market.__table__,
            MarketSnapshot.__table__,
            Forecast.__table__,
            MicrostructureFeature.__table__,
        ],
    )
    with Session(engine) as session:
        rows = []
        for i in range(3):
            row = MarketSnapshot(
                ticker="SOL",
                captured_at=cutoff - timedelta(minutes=3 - i),
                best_yes_bid=str(Decimal(".40") + Decimal(i) / 100),
                best_yes_ask=".50",
                best_no_bid=".50",
                best_no_ask=".60",
                spread=".08",
                volume_fp="20",
                raw_market_json=json.dumps(
                    {"close_time": (cutoff + timedelta(hours=1)).isoformat()}
                ),
                raw_orderbook_json=json.dumps(
                    {
                        "orderbook_fp": {
                            "yes_dollars": [["0.42", "12"]],
                            "no_dollars": [["0.50", "8"]],
                        }
                    }
                ),
            )
            session.add(row)
            rows.append(row)
        ensemble = Forecast(
            ticker="SOL",
            forecasted_at=cutoff - timedelta(minutes=1),
            model_name="ensemble_v2",
            yes_probability=".52",
            feature_json="{}",
        )
        session.add(ensemble)
        session.flush()

        def receipt(r):
            return RecordReceipt(r.id, record_hash(r), cutoff)

        context = FeatureBuildContext(cutoff, cutoff, tuple(map(receipt, rows)), receipt(ensemble))
        settings = Settings()
        feature = insert_microstructure_feature(
            session,
            compute_microstructure_feature(
                session, rows, lookback_minutes=60, settings=settings, context=context
            ),
        )
        yield session, rows, ensemble, context, settings, feature


def invoke(sample, **kwargs):
    session, rows, _, context, settings, feature = sample
    return MicrostructureV1Forecaster(settings).forecast_bound(
        session,
        rows[-1],
        feature_id=feature.id,
        feature_sha256=record_hash(feature),
        context=kwargs.get("context", context),
    )


def test_exact_feature_after_snapshot_and_sqlite_roundtrip(sample):
    session, rows, _, context, _, feature = sample
    session.commit()
    session.expire_all()
    output = invoke(sample)
    assert output is not None
    assert output.forecasted_at > context.model_input_as_of
    assert output.feature_json["feature_created_at"] > output.feature_json["snapshot_at"]
    assert output.feature_json["decision_at"] is None
    assert not output.feature_json["release_certified"]
    # A newer unrelated row cannot replace the exact selected feature.
    newer = MicrostructureFeature(
        ticker="SOL",
        created_at=context.model_input_as_of + timedelta(seconds=8),
        lookback_minutes=60,
        snapshot_count=3,
        raw_json="{}",
    )
    session.add(newer)
    session.flush()
    assert invoke(sample).yes_probability == output.yes_probability


def test_future_receipt_rejected(sample):
    context = sample[3]
    future = replace(
        context.snapshots[-1], received_at=context.model_input_as_of + timedelta(seconds=1)
    )
    with pytest.raises(ValueError, match="VISIBILITY"):
        invoke(sample, context=replace(context, snapshots=(*context.snapshots[:-1], future)))


def test_changed_snapshot_and_ensemble_rejected(sample):
    sample[1][0].best_yes_bid = ".01"
    with pytest.raises(ValueError, match="HASH"):
        invoke(sample)


def test_changed_ensemble_rejected(sample):
    sample[2].yes_probability = ".99"
    with pytest.raises(ValueError, match="HASH"):
        invoke(sample)


def test_future_ensemble_receipt_rejected(sample):
    context = sample[3]
    with pytest.raises(ValueError, match="VISIBILITY"):
        invoke(
            sample,
            context=replace(
                context,
                ensemble=replace(
                    context.ensemble, received_at=context.model_input_as_of + timedelta(seconds=1)
                ),
            ),
        )


def test_forged_feature_resigned_hash_rejected(sample):
    sample[5].price_velocity = "0.9"
    with pytest.raises(ValueError, match="FEATURE_VALUE"):
        invoke(sample)


def test_unproven_legacy_feature_rejected(sample):
    sample[5].raw_json = "{}"
    with pytest.raises(ValueError, match="UNPROVEN"):
        invoke(sample)
    assert (
        MicrostructureV1Forecaster(sample[4])
        .forecast(sample[0], sample[1][-1])
        .feature_json["timing_status"]
        == "LEGACY_UNVERIFIED"
    )


def test_changed_settings_rejected(sample):
    sample[4].microstructure_v1_max_adjustment = Decimal(".07")
    with pytest.raises(ValueError, match="SETTINGS"):
        invoke(sample)


def test_expired_target_rejected(sample, monkeypatch):
    monkeypatch.setattr(
        "kalshi_predictor.forecasting.microstructure_v1.utc_now",
        lambda: sample[3].model_input_as_of + timedelta(hours=1),
    )
    with pytest.raises(ValueError, match="EXPIRED"):
        invoke(sample)


def test_external_naive_clock_rejected(sample):
    with pytest.raises(ValueError, match="AWARE"):
        invoke(
            sample,
            context=replace(
                sample[3], model_input_as_of=sample[3].model_input_as_of.replace(tzinfo=None)
            ),
        )


def test_missing_close_cannot_use_current_market_fallback(sample):
    session, rows, _, context, settings, _ = sample
    rows[-1].raw_market_json = "{}"
    receipts = (
        *context.snapshots[:-1],
        replace(context.snapshots[-1], record_sha256=record_hash(rows[-1])),
    )
    with pytest.raises(ValueError, match="CLOSE_TIME_REQUIRED"):
        compute_microstructure_feature(
            session,
            rows,
            lookback_minutes=60,
            settings=settings,
            context=replace(context, snapshots=receipts),
        )


def test_external_naive_close_rejected(sample):
    session, rows, _, context, settings, _ = sample
    rows[-1].raw_market_json = json.dumps({"close_time": "2026-09-10T22:00:00"})
    receipts = (
        *context.snapshots[:-1],
        replace(context.snapshots[-1], record_sha256=record_hash(rows[-1])),
    )
    with pytest.raises(ValueError, match="AWARE"):
        compute_microstructure_feature(
            session,
            rows,
            lookback_minutes=60,
            settings=settings,
            context=replace(context, snapshots=receipts),
        )


def test_completion_timestamp_after_arithmetic(sample, monkeypatch):
    cutoff = sample[3].model_input_as_of
    clocks = iter([cutoff + timedelta(seconds=4), cutoff + timedelta(seconds=7)])
    monkeypatch.setattr(
        "kalshi_predictor.forecasting.microstructure_v1.utc_now", lambda: next(clocks)
    )
    assert invoke(sample).forecasted_at == cutoff + timedelta(seconds=7)


def test_boolean_feature_id_rejected(sample):
    with pytest.raises(ValueError, match="POSITIVE_FEATURE_ID"):
        MicrostructureV1Forecaster(sample[4]).forecast_bound(
            sample[0],
            sample[1][-1],
            feature_id=True,
            feature_sha256=record_hash(sample[5]),
            context=sample[3],
        )


def test_boolean_receipt_id_rejected(sample):
    context = sample[3]
    with pytest.raises(ValueError, match="POSITIVE_RECORD_ID"):
        invoke(
            sample,
            context=replace(
                context,
                snapshots=(replace(context.snapshots[0], record_id=True), *context.snapshots[1:]),
            ),
        )


def test_boolean_lookback_rejected(sample):
    with pytest.raises(ValueError, match="INTEGER"):
        compute_microstructure_feature(
            sample[0], sample[1], lookback_minutes=True, settings=sample[4], context=sample[3]
        )


def test_missing_ensemble_explicitly_unavailable(sample):
    with pytest.raises(ValueError, match="NO_ABSENCE_PROOF"):
        invoke(sample, context=replace(sample[3], ensemble=None))


def test_resigned_nonfinite_feature_rejected(sample):
    sample[5].price_velocity = "NaN"
    with pytest.raises(ValueError, match="NONFINITE_FEATURE"):
        invoke(sample)


def test_feature_completion_after_target_rejected(sample, monkeypatch):
    monkeypatch.setattr(
        "kalshi_predictor.microstructure.orderbook_features.utc_now",
        lambda: sample[3].model_input_as_of + timedelta(hours=1),
    )
    with pytest.raises(ValueError, match="EXPIRED_FEATURE_COMPLETION"):
        compute_microstructure_feature(
            sample[0], sample[1], lookback_minutes=60, settings=sample[4], context=sample[3]
        )
