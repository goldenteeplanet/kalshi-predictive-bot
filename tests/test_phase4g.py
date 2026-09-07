import json
from datetime import timedelta

from sqlalchemy import select

from kalshi_predictor.data.schema import (
    CalibrationObservation,
    CryptoFeature,
    CryptoFeatureLineage,
    PaperOrder,
)
from kalshi_predictor.phase4cd.lineage import (
    FEATURE_CONFLICT,
    FEATURE_LINEAGE_VERIFIED,
    FEATURE_SOURCE_AFTER_DECISION,
    audit_crypto_feature_lineage,
    build_event_level_comparison,
    immutable_provenance_hash,
)
from kalshi_predictor.utils.time import utc_now
from tests.test_phase4cd import _count, _factory, _seed


def test_provenance_hash_is_deterministic() -> None:
    assert immutable_provenance_hash({"b": 2, "a": 1}) == immutable_provenance_hash(
        {"a": 1, "b": 2}
    )


def test_lineage_timestamp_enforcement_conflict_and_no_fabrication(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session:
        forecast = _seed(session, settled=True)
        feature_time = forecast.forecasted_at - timedelta(seconds=1)
        feature = _feature(11, feature_time, feature_time)
        session.add(feature)
        session.flush()
        forecast.feature_json = _payload(feature, embedded_time=feature_time)
        session.commit()
        paper_before = _count(session, PaperOrder)
        first = audit_crypto_feature_lineage(session, session)
        row = session.scalar(select(CryptoFeatureLineage))
        first_hash = row.provenance_hash
        second = audit_crypto_feature_lineage(session, session)
        assert first["verdicts"][FEATURE_LINEAGE_VERIFIED] == 1
        assert second["fabricated_features"] == 0
        assert session.scalar(select(CryptoFeatureLineage)).provenance_hash == first_hash
        assert _count(session, PaperOrder) == paper_before

        feature.generated_at = forecast.forecasted_at + timedelta(seconds=1)
        forecast.feature_json = _payload(feature, embedded_time=feature.generated_at)
        session.commit()
        after = audit_crypto_feature_lineage(session, session)
        assert after["verdicts"][FEATURE_SOURCE_AFTER_DECISION] == 1

        feature.generated_at = feature_time
        forecast.feature_json = _payload(feature, embedded_time=feature_time - timedelta(seconds=2))
        session.commit()
        conflict = audit_crypto_feature_lineage(session, session)
        assert conflict["verdicts"][FEATURE_CONFLICT] == 1


def test_event_comparison_matches_identical_events_only(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session:
        now = utc_now()
        session.add_all(
            [
                _observation("m-run", "market_implied_v1", "E1", 1, now),
                _observation("c-run", "crypto_v2", "E1", 2, now),
                _observation("c-run", "crypto_v2", "E2", 3, now),
            ]
        )
        session.commit()
        result = build_event_level_comparison(
            session,
            model_runs={"market_implied_v1": "m-run", "crypto_v2": "c-run"},
        )
        assert result["matched_events"] == 1
        assert result["models"]["crypto_v2"]["unmatched_events"] == 1
        assert result["models"]["market_implied_v1"]["rows_on_matched_events"] == 1


def _feature(feature_id, generated_at, source_at):
    return CryptoFeature(
        id=feature_id,
        symbol="BTC",
        source="stored_prices",
        generated_at=generated_at,
        window_minutes=1440,
        price="100",
        return_5m="0",
        return_15m="0",
        return_1h="0",
        return_4h="0",
        return_24h="0",
        volatility_1h="0",
        volatility_4h="0",
        volatility_24h="0",
        momentum_score="0",
        trend_direction="FLAT",
        raw_json=json.dumps(
            {
                "feature_version": "crypto_features_v2_point_in_time",
                "source_latest_observed_at": source_at.isoformat(),
            }
        ),
        created_at=generated_at,
    )


def _payload(feature, *, embedded_time):
    return json.dumps(
        {
            "component_feature_ids": {"BTC": feature.id},
            "crypto_feature_id": feature.id,
            "forecast_cutoff": embedded_time.isoformat(),
            "point_in_time_validation": {"BTC": {"generated_at": embedded_time.isoformat()}},
        }
    )


def _observation(run_id, model, event_id, forecast_id, now):
    return CalibrationObservation(
        observation_id=f"{run_id}-{forecast_id}",
        run_id=run_id,
        forecast_id=forecast_id,
        market_ticker=f"T-{forecast_id}",
        event_ticker=event_id,
        model=model,
        forecast_timestamp=now,
        settlement_timestamp=now + timedelta(hours=1),
        forecast_probability="0.7",
        settlement_result="yes",
        brier_contribution="0.09",
        log_loss_contribution="0.35667494",
        independent_event_id=event_id,
        provenance_json="{}",
        created_at=now,
    )
