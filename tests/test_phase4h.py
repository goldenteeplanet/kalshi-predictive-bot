import json
from datetime import timedelta

from kalshi_predictor.data.schema import (
    CryptoFeature,
    EvidenceExpansionMember,
    Forecast,
    PaperOrder,
)
from kalshi_predictor.phase4cd.attribution import build_edge_attribution
from kalshi_predictor.phase4cd.expansion import expand_verified_crypto_cohort
from kalshi_predictor.phase4cd.lineage import audit_crypto_feature_lineage
from kalshi_predictor.phase4cd.replay import run_research_replay
from sqlalchemy import select

from tests.test_phase4cd import _count, _factory, _seed


def test_expansion_checkpoint_resume_hash_and_frozen_isolation(tmp_path) -> None:
    source_factory = _factory(tmp_path / "source")
    research_factory = _factory(tmp_path / "research")
    with source_factory() as source:
        first = _seed(source, settled=True)
        feature = _verified_feature(first, feature_id=71)
        source.add(feature)
        source.flush()
        first.feature_json = _feature_payload(feature)
        source.add(
            Forecast(
                ticker=first.ticker,
                forecasted_at=first.forecasted_at + timedelta(seconds=1),
                model_name="crypto_v2",
                yes_probability="0.71",
                market_mid_probability="0.40",
                best_yes_bid="0.39",
                best_yes_ask="0.40",
                feature_json=_feature_payload(feature),
                notes=None,
            )
        )
        source.commit()
        with research_factory() as research:
            frozen_before = _count(research, Forecast)
            paper_before = _count(research, PaperOrder)
            one = expand_verified_crypto_cohort(
                research, source, max_independent_events=2, scan_limit=1
            )
            two = expand_verified_crypto_cohort(
                research, source, max_independent_events=2, scan_limit=1, resume=True
            )
            assert one.status == "CHECKPOINTED"
            assert two.admitted == 2
            assert two.cohort_hash is not None
            assert len(list(research.scalars(select(EvidenceExpansionMember)))) == 2
            assert frozen_before == 0
            assert _count(research, PaperOrder) == paper_before


def test_after_decision_lineage_is_never_admitted(tmp_path) -> None:
    source_factory = _factory(tmp_path / "source")
    research_factory = _factory(tmp_path / "research")
    with source_factory() as source:
        forecast = _seed(source, settled=True)
        feature = _verified_feature(forecast, feature_id=72, delay_seconds=1)
        source.add(feature)
        source.flush()
        forecast.feature_json = _feature_payload(feature)
        source.commit()
        with research_factory() as research:
            result = expand_verified_crypto_cohort(
                research, source, max_independent_events=1, scan_limit=10
            )
            assert result.admitted == 0
            assert result.rejection_counts["FEATURE_SOURCE_AFTER_DECISION"] == 1


def test_edge_decomposition_and_concentration_gate(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session:
        forecast = _seed(session, settled=True)
        feature = _verified_feature(forecast, feature_id=73)
        session.add(feature)
        session.flush()
        forecast.feature_json = _feature_payload(feature)
        session.commit()
        audit_crypto_feature_lineage(session, session)
        paper_before = _count(session, PaperOrder)
        replay = run_research_replay(session, model="crypto_v2", require_verified_lineage=True)
        report = build_edge_attribution(session, run_id=replay.run_id)
        assert report["positive_gross_rows"] == 1
        assert report["executable_qualified_rows"] == 1
        assert report["concentration"]["passes_repeatability_gate"] is False
        assert report["latency"]["feature_after_decision_admitted"] == 0
        assert _count(session, PaperOrder) == paper_before


def _verified_feature(forecast, *, feature_id, delay_seconds=-1):
    generated = forecast.forecasted_at + timedelta(seconds=delay_seconds)
    return CryptoFeature(
        id=feature_id,
        symbol="BTC",
        source="stored_prices",
        generated_at=generated,
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
                "source_latest_observed_at": generated.isoformat(),
            }
        ),
        created_at=generated,
    )


def _feature_payload(feature):
    return json.dumps(
        {
            "symbol": "BTC",
            "component_symbols": ["BTC"],
            "component_feature_ids": {"BTC": feature.id},
            "crypto_feature_id": feature.id,
            "point_in_time_validation": {"BTC": {"generated_at": feature.generated_at.isoformat()}},
        }
    )
