import json
from datetime import timedelta
from decimal import Decimal

from kalshi_predictor.config import get_settings
from kalshi_predictor.data.schema import (
    CalibrationObservation,
    Forecast,
    PaperOrder,
    ReplayDisposition,
)
from kalshi_predictor.phase4cd.replay import (
    DISPOSITIONS,
    FEES_CONSUME_EDGE,
    MISSING_POINT_IN_TIME_FEATURES,
    run_research_replay,
)
from sqlalchemy import func, select

from tests.test_phase4cd import _count, _factory, _seed


def test_disposition_is_exhaustive_and_calibration_is_not_trade(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session:
        _seed(session, settled=True)
        session.commit()
        paper_before = _count(session, PaperOrder)
        result = run_research_replay(session, model="crypto_v2")
        assert sum(result.disposition_counts.values()) == result.processed == 1
        assert result.disposition_counts[MISSING_POINT_IN_TIME_FEATURES] == 1
        assert result.calibration_observations == 1
        assert _count(session, CalibrationObservation) == 1
        assert _count(session, PaperOrder) == paper_before
        assert set(result.disposition_counts) == set(DISPOSITIONS)


def test_fee_rejection_is_attributed_after_positive_gross_edge(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session:
        _seed(session, settled=True, model="market_implied_v1")
        session.commit()
        settings = get_settings().model_copy(
            update={"paper_default_fee_per_contract": Decimal("0.35")}
        )
        result = run_research_replay(session, model="market_implied_v1", settings=settings)
        assert result.disposition_counts[FEES_CONSUME_EDGE] == 1
        row = session.scalar(select(ReplayDisposition))
        assert Decimal(json.loads(row.details_json)["gross_edge"]) > 0


def test_composite_cursor_does_not_skip_forecasts_inside_event(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session:
        first = _seed(session, settled=True)
        session.add(
            Forecast(
                ticker=first.ticker,
                forecasted_at=first.forecasted_at + timedelta(seconds=1),
                model_name=first.model_name,
                yes_probability=first.yes_probability,
                market_mid_probability=first.market_mid_probability,
                best_yes_bid=first.best_yes_bid,
                best_yes_ask=first.best_yes_ask,
                feature_json="{}",
                notes=None,
            )
        )
        session.commit()
        one = run_research_replay(session, model="crypto_v2", limit=1, resume=False)
        two = run_research_replay(session, model="crypto_v2", limit=1, resume=True)
        assert one.run_id == two.run_id
        assert two.processed == 2
        assert int(session.scalar(select(func.count()).select_from(ReplayDisposition)) or 0) == 2
