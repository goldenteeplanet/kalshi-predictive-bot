from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import INACTIVE_MARKET_STATUSES
from kalshi_predictor.data.repositories import decode_json, insert_forecast
from kalshi_predictor.data.schema import (
    CryptoMarketLink,
    EconomicMarketLink,
    Market,
    MarketSnapshot,
    NewsMarketLink,
    SportsMarketLink,
    WeatherMarketLink,
)
from kalshi_predictor.features.builder import build_feature_snapshot
from kalshi_predictor.forecasting.base import ForecastInput, ForecastOutput
from kalshi_predictor.forecasting.crypto_v1 import CryptoV1Forecaster
from kalshi_predictor.forecasting.crypto_v2 import CryptoV2Forecaster
from kalshi_predictor.forecasting.economic_v1 import EconomicV1Forecaster
from kalshi_predictor.forecasting.ensemble_v1 import EnsembleV1Forecaster
from kalshi_predictor.forecasting.ensemble_v2 import EnsembleV2Forecaster
from kalshi_predictor.forecasting.market_implied import MarketImpliedForecaster
from kalshi_predictor.forecasting.meta_ensemble_v1 import MetaEnsembleV1Forecaster
from kalshi_predictor.forecasting.meta_model_v1 import MetaModelV1Forecaster
from kalshi_predictor.forecasting.microstructure_v1 import MicrostructureV1Forecaster
from kalshi_predictor.forecasting.mlb_v1 import MLBV1Forecaster
from kalshi_predictor.forecasting.nba_v1 import NBAV1Forecaster
from kalshi_predictor.forecasting.news_v1 import NewsV1Forecaster
from kalshi_predictor.forecasting.nfl_v1 import NFLV1Forecaster
from kalshi_predictor.forecasting.nhl_v1 import NHLV1Forecaster
from kalshi_predictor.forecasting.sports_v1 import SportsV1Forecaster
from kalshi_predictor.forecasting.weather_v1 import WeatherV1Forecaster
from kalshi_predictor.forecasting.weather_v2 import WeatherV2Forecaster
from kalshi_predictor.memory.capture import capture_forecast_attempt
from kalshi_predictor.paper.ledger import get_latest_snapshot_for_ticker
from kalshi_predictor.signals.attribution import attribute_forecast_signals
from kalshi_predictor.signals.registry import ensure_builtin_signals
from kalshi_predictor.utils.time import utc_now

MODEL_NAMES = (
    "market_implied_v1",
    "weather_v1",
    "weather_v2",
    "crypto_v1",
    "crypto_v2",
    "economic_v1",
    "news_v1",
    "mlb_v1",
    "nba_v1",
    "nfl_v1",
    "nhl_v1",
    "sports_v1",
    "microstructure_v1",
    "meta_model_v1",
    "meta_ensemble_v1",
    "ensemble_v1",
    "ensemble_v2",
)

_MODEL_LINK_TABLES = {
    "weather_v2": WeatherMarketLink,
    "crypto_v1": CryptoMarketLink,
    "crypto_v2": CryptoMarketLink,
    "economic_v1": EconomicMarketLink,
    "news_v1": NewsMarketLink,
    "sports_v1": SportsMarketLink,
}


@dataclass(frozen=True)
class ForecastRunSummary:
    snapshots_scanned: int
    forecasts_inserted: int
    skipped: int


class MarketImpliedModelAdapter:
    model_name = "market_implied_v1"

    def __init__(self) -> None:
        self._forecaster = MarketImpliedForecaster()

    def forecast(self, session: Session, snapshot: MarketSnapshot) -> ForecastOutput | None:
        del session
        return self._forecaster.forecast(
            ForecastInput(
                ticker=snapshot.ticker,
                captured_at=snapshot.captured_at,
                market_json=decode_json(snapshot.raw_market_json),
                orderbook_json=decode_json(snapshot.raw_orderbook_json),
            )
        )


def get_forecaster(model_name: str):
    if model_name == "market_implied_v1":
        return MarketImpliedModelAdapter()
    if model_name == "weather_v1":
        return WeatherV1Forecaster()
    if model_name == "weather_v2":
        return WeatherV2Forecaster()
    if model_name == "crypto_v1":
        return CryptoV1Forecaster()
    if model_name == "crypto_v2":
        return CryptoV2Forecaster()
    if model_name == "economic_v1":
        return EconomicV1Forecaster()
    if model_name == "news_v1":
        return NewsV1Forecaster()
    if model_name == "mlb_v1":
        return MLBV1Forecaster()
    if model_name == "nba_v1":
        return NBAV1Forecaster()
    if model_name == "nfl_v1":
        return NFLV1Forecaster()
    if model_name == "nhl_v1":
        return NHLV1Forecaster()
    if model_name == "sports_v1":
        return SportsV1Forecaster()
    if model_name == "microstructure_v1":
        return MicrostructureV1Forecaster()
    if model_name == "meta_model_v1":
        return MetaModelV1Forecaster()
    if model_name == "meta_ensemble_v1":
        return MetaEnsembleV1Forecaster()
    if model_name == "ensemble_v1":
        return EnsembleV1Forecaster()
    if model_name == "ensemble_v2":
        return EnsembleV2Forecaster()
    raise ValueError(f"Unknown forecast model: {model_name}")


def selected_model_names(model_name: str) -> tuple[str, ...]:
    if model_name == "all":
        return MODEL_NAMES
    if model_name not in MODEL_NAMES:
        raise ValueError(f"Unknown forecast model: {model_name}")
    return (model_name,)


def run_forecast_models(
    session: Session,
    *,
    model_name: str,
    snapshots: Iterable[MarketSnapshot],
) -> ForecastRunSummary:
    model_names = selected_model_names(model_name)
    forecasters = [(name, get_forecaster(name)) for name in model_names]
    for _, forecaster in forecasters:
        begin_run = getattr(forecaster, "begin_forecast_run", None)
        if callable(begin_run):
            begin_run()
    snapshot_list = list(snapshots)
    inserted = 0
    skipped = 0
    builtin_signals_ensured = False

    try:
        for snapshot in snapshot_list:
            build_feature_snapshot(session, snapshot)
            for name, forecaster in forecasters:
                forecast = forecaster.forecast(session, snapshot)
                if forecast is None:
                    capture_forecast_attempt(
                        session,
                        snapshot=snapshot,
                        model_name=name,
                        forecast=None,
                    )
                    skipped += 1
                    continue
                record = insert_forecast(
                    session, forecast, market_snapshot_id=snapshot.id,
                )
                if not builtin_signals_ensured:
                    ensure_builtin_signals(session)
                    builtin_signals_ensured = True
                attribute_forecast_signals(
                    session,
                    record,
                    snapshot=snapshot,
                    ensure_builtin=False,
                )
                inserted += 1
    finally:
        for _, forecaster in forecasters:
            end_run = getattr(forecaster, "end_forecast_run", None)
            if callable(end_run):
                end_run()

    return ForecastRunSummary(
        snapshots_scanned=len(snapshot_list),
        forecasts_inserted=inserted,
        skipped=skipped,
    )


def latest_snapshots_for_forecasts(
    session: Session,
    tickers: Iterable[str],
) -> list[MarketSnapshot]:
    snapshots: list[MarketSnapshot] = []
    for ticker in tickers:
        snapshot = get_latest_snapshot_for_ticker(session, ticker)
        if snapshot is not None:
            snapshots.append(snapshot)
    return snapshots


def latest_snapshots_for_model(
    session: Session,
    *,
    model_name: str,
    limit: int = 100,
    as_of: datetime | None = None,
) -> list[MarketSnapshot] | None:
    """Return latest snapshots scoped to a model's domain links when possible."""
    link_table = _MODEL_LINK_TABLES.get(model_name)
    if link_table is None:
        return None

    linked_tickers = select(link_table.ticker).distinct().subquery()
    market_status = func.lower(func.coalesce(Market.status, ""))
    snapshot_status = func.lower(func.coalesce(MarketSnapshot.status, ""))
    eligibility = [
        ~market_status.in_(tuple(sorted(INACTIVE_MARKET_STATUSES))),
        ~snapshot_status.in_(tuple(sorted(INACTIVE_MARKET_STATUSES))),
    ]
    if model_name in {"weather_v1", "weather_v2"}:
        eligibility.extend((
            Market.close_time.is_not(None),
            Market.close_time > (as_of or utc_now()),
        ))
    latest_per_ticker = (
        select(
            MarketSnapshot.ticker.label("ticker"),
            func.max(MarketSnapshot.captured_at).label("captured_at"),
        )
        .join(linked_tickers, MarketSnapshot.ticker == linked_tickers.c.ticker)
        .outerjoin(Market, Market.ticker == MarketSnapshot.ticker)
        .where(*eligibility)
        .group_by(MarketSnapshot.ticker)
        .subquery()
    )
    statement = (
        select(MarketSnapshot)
        .join(
            latest_per_ticker,
            and_(
                MarketSnapshot.ticker == latest_per_ticker.c.ticker,
                MarketSnapshot.captured_at == latest_per_ticker.c.captured_at,
            ),
        )
        .order_by(desc(MarketSnapshot.captured_at))
        .limit(limit)
    )
    return list(session.scalars(statement))
