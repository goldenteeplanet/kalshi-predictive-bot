import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.schema import (
    CanonicalEvaluation,
    Forecast,
    Market,
    MarketSnapshot,
    PaperOrder,
    ResearchCheckpoint,
    Settlement,
    ShadowDecision,
)
from kalshi_predictor.phase4cd.domain import (
    EvidenceLevel,
    evidence_level,
    independent_event_id,
    validate_point_in_time,
)
from kalshi_predictor.phase4cd.replay import run_research_replay
from kalshi_predictor.phase4cd.reports import ensemble_audit
from kalshi_predictor.phase4cd.rights import corpus_access_status, require_corpus_access
from kalshi_predictor.phase4cd.shadow import (
    capture_shadow_decisions,
    reconcile_shadow_settlements,
)
from kalshi_predictor.utils.time import utc_now


def test_no_lookahead_rejects_future_inputs_and_early_settlement() -> None:
    decision = utc_now()
    with pytest.raises(ValueError, match="LOOKAHEAD:feature_timestamp"):
        validate_point_in_time(
            feature_timestamp=decision + timedelta(seconds=1),
            source_timestamp=decision,
            snapshot_timestamp=decision,
            decision_timestamp=decision,
            settlement_timestamp=decision + timedelta(hours=1),
        )
    with pytest.raises(ValueError, match="LOOKAHEAD:settlement_timestamp"):
        validate_point_in_time(
            feature_timestamp=decision,
            source_timestamp=decision,
            snapshot_timestamp=decision,
            decision_timestamp=decision,
            settlement_timestamp=decision,
        )


def test_independent_event_groups_sibling_threshold_contracts() -> None:
    left = independent_event_id(
        ticker="BTC-ABOVE-115K", event_ticker="BTC-2026-08-23", series_ticker="BTC"
    )
    right = independent_event_id(
        ticker="BTC-ABOVE-117K", event_ticker="BTC-2026-08-23", series_ticker="BTC"
    )
    assert left == right == "BTC-2026-08-23"


def test_replay_checkpoints_and_resume_are_idempotent(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session:
        _seed(session, settled=True)
        session.commit()
        first = run_research_replay(
            session, model="crypto_v2", limit=1, checkpoint_every=1, resume=False
        )
        evaluation_n = _count(session, CanonicalEvaluation)
        assert first.status == "CHECKPOINTED"
        assert _count(session, ResearchCheckpoint) >= 1
        second = run_research_replay(
            session, model="crypto_v2", limit=1, checkpoint_every=1, resume=True
        )
        assert _count(session, CanonicalEvaluation) == evaluation_n
        assert second.run_id == first.run_id


def test_shadow_never_creates_paper_order_and_reconciles_exact_settlement(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session:
        forecast = _seed(session, settled=False)
        session.commit()
        paper_before = _count(session, PaperOrder)
        result = capture_shadow_decisions(
            session, model="crypto_v2", max_age_minutes=60, slippage=Decimal("0")
        )
        assert result.created == 1
        assert _count(session, PaperOrder) == paper_before
        decision = session.scalar(select(ShadowDecision))
        session.add(
            Settlement(
                ticker=forecast.ticker,
                settled_at=forecast.forecasted_at + timedelta(hours=1),
                result="yes",
                yes_settlement_value="1",
                raw_json="{}",
                updated_at=forecast.forecasted_at + timedelta(hours=1),
            )
        )
        session.commit()
        reconciled = reconcile_shadow_settlements(session)
        assert reconciled.evaluated == 1
        evaluation = session.scalar(select(CanonicalEvaluation))
        assert evaluation.source_lane == "SHADOW"
        assert evaluation.market_ticker == decision.ticker
        assert _count(session, PaperOrder) == paper_before


def test_fee_adjusted_ev_cannot_become_positive_by_lowering_gate(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session:
        _seed(session, settled=True)
        session.commit()
        settings = get_settings().model_copy(
            update={"paper_default_fee_per_contract": Decimal("0.35")}
        )
        result = run_research_replay(
            session, model="crypto_v2", limit=10, settings=settings
        )
        assert result.evaluated == 0


def test_ensemble_contributions_and_effective_count(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session:
        forecast = _seed(session, settled=False, model="ensemble_v2")
        forecast.feature_json = json.dumps(
            {
                "components": [
                    {"model": "market_implied_v1", "probability": "0.60", "weight": "0.7"},
                    {"model": "crypto_v2", "probability": "0.80", "weight": "0.3"},
                ]
            }
        )
        session.commit()
        payload = ensemble_audit(session)
        assert payload["effective_model_count"] == 2
        assert payload["components"][0]["weighted_contribution"] == "0.420"


@pytest.mark.parametrize(
    ("count", "level"),
    [
        (0, EvidenceLevel.NO_EVIDENCE),
        (1, EvidenceLevel.EARLY),
        (10, EvidenceLevel.PRELIMINARY),
        (30, EvidenceLevel.USEFUL),
        (100, EvidenceLevel.STRONG),
    ],
)
def test_model_evidence_levels(count: int, level: EvidenceLevel) -> None:
    assert evidence_level(count) is level


def test_rights_gated_corpus_access() -> None:
    assert corpus_access_status("PredictionMarketBench") == "BLOCKED_PENDING_RIGHTS"
    with pytest.raises(PermissionError, match="BLOCKED_PENDING_RIGHTS"):
        require_corpus_access("Prediction_Markets_Public")
    require_corpus_access("PredictionMarketBench", authorization_registered=True)


def test_canonical_evaluation_has_database_lane_constraint() -> None:
    names = {constraint.name for constraint in CanonicalEvaluation.__table__.constraints}
    assert "ck_canonical_evaluations_source_lane" in names


def _factory(tmp_path: Path):
    return get_session_factory(init_db(f"sqlite:///{tmp_path / 'phase4cd.db'}"))


def _seed(session, *, settled: bool, model: str = "crypto_v2") -> Forecast:
    now = utc_now()
    forecast_time = now - timedelta(minutes=2)
    market = Market(
        ticker=f"T-{model}",
        event_ticker=f"E-{model}",
        series_ticker="BTC",
        title="BTC above threshold",
        subtitle=None,
        market_type="binary",
        status="open",
        result=None,
        open_time=now - timedelta(days=1),
        close_time=now + timedelta(hours=1),
        expected_expiration_time=now + timedelta(hours=1),
        expiration_time=now + timedelta(hours=1),
        settlement_ts=None,
        settlement_value_dollars=None,
        volume_fp="100",
        open_interest_fp="100",
        liquidity_dollars="100",
        rules_primary="Settles yes if BTC is above threshold.",
        rules_secondary=None,
        raw_json="{}",
        first_seen_at=now - timedelta(days=1),
        last_seen_at=now,
    )
    session.merge(market)
    session.flush()
    snapshot = MarketSnapshot(
        ticker=market.ticker,
        captured_at=forecast_time - timedelta(seconds=1),
        status="open",
        yes_bid_dollars="0.39",
        yes_ask_dollars="0.40",
        no_bid_dollars="0.59",
        no_ask_dollars="0.60",
        best_yes_bid="0.39",
        best_yes_ask="0.40",
        best_no_bid="0.59",
        best_no_ask="0.60",
        spread="0.01",
        last_price_dollars="0.40",
        volume_fp="100",
        volume_24h_fp="100",
        open_interest_fp="100",
        raw_market_json="{}",
        raw_orderbook_json="{}",
    )
    session.add(snapshot)
    forecast = Forecast(
        ticker=market.ticker,
        forecasted_at=forecast_time,
        model_name=model,
        yes_probability="0.70",
        market_mid_probability="0.40",
        best_yes_bid="0.39",
        best_yes_ask="0.40",
        feature_json="{}",
        notes=None,
    )
    session.add(forecast)
    session.flush()
    if settled:
        session.merge(
            Settlement(
                ticker=market.ticker,
                settled_at=forecast_time + timedelta(hours=1),
                result="yes",
                yes_settlement_value="1",
                raw_json="{}",
                updated_at=forecast_time + timedelta(hours=1),
            )
        )
    return forecast


def _count(session, model) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)
