import importlib.util
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import insert_forecast
from kalshi_predictor.config import Settings
from kalshi_predictor.data.schema import (
    Forecast, Market, MarketRanking, MarketSnapshot, RuntimeProvenanceEvent,
)
from kalshi_predictor.opportunities.repository import insert_market_ranking


def _disable_memory_capture(monkeypatch) -> None:
    import kalshi_predictor.memory.capture as capture
    monkeypatch.setattr(capture, "capture_forecast_created", lambda *args, **kwargs: None)
    monkeypatch.setattr(capture, "capture_market_ranking", lambda *args, **kwargs: None)


def _market(session: Session) -> Market:
    now = datetime(2026, 7, 16, 16, tzinfo=timezone.utc)
    market = Market(ticker="SYN", raw_json="{}", first_seen_at=now, last_seen_at=now)
    session.add(market); session.flush()
    return market


def _forecast_payload() -> dict:
    return {"ticker": "SYN", "forecasted_at": datetime(2026, 7, 16, 16, 2,
            tzinfo=timezone.utc), "model_name": "crypto_v2",
            "yes_probability": Decimal("0.60"), "feature_json": {
                "crypto_feature_id": 12,
                "source_observation_ref": {"table": "crypto_prices", "id": 9},
            }}


def test_prov4_disabled_flag_preserves_legacy_writer_without_new_table(
    tmp_path: Path, monkeypatch,
) -> None:
    _disable_memory_capture(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    Market.__table__.create(engine)
    Forecast.__table__.create(engine)
    with Session(engine) as session:
        _market(session)
        record = insert_forecast(session, _forecast_payload(), attribution_enabled=False)
        session.commit()
        assert record.id is not None
    assert "runtime_provenance_events" not in inspect(engine).get_table_names()
    assert Settings().runtime_provenance_dual_write_enabled is False


def test_prov4_enabled_dual_write_is_append_only_and_hash_chained(
    tmp_path: Path, monkeypatch,
) -> None:
    _disable_memory_capture(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'dual.db'}")
    for table in (Market.__table__, MarketSnapshot.__table__, Forecast.__table__,
                  MarketRanking.__table__, RuntimeProvenanceEvent.__table__):
        table.create(engine, checkfirst=True)
    with Session(engine) as session:
        _market(session)
        snapshot = MarketSnapshot(ticker="SYN", captured_at=datetime(
            2026, 7, 16, 16, 2, tzinfo=timezone.utc), raw_market_json="{}")
        session.add(snapshot); session.flush()
        forecast = insert_forecast(
            session, _forecast_payload(), market_snapshot_id=snapshot.id,
            attribution_enabled=True,
        )
        ranking = insert_market_ranking(session, {
            "ticker": "SYN", "forecast_model": "crypto_v2", "forecast_id": forecast.id,
            "market_snapshot_id": snapshot.id, "ranked_at": datetime(
                2026, 7, 16, 16, 3, tzinfo=timezone.utc), "raw_json": {},
        }, attribution_enabled=True)
        ranking_id = ranking.id
        session.commit()
        events = list(session.scalars(select(RuntimeProvenanceEvent).order_by(
            RuntimeProvenanceEvent.id)))
    assert [event.stage for event in events] == ["FORECAST_CREATED", "RANKING_CREATED"]
    assert events[0].previous_digest == "GENESIS"
    assert events[1].previous_digest == events[0].provenance_digest
    assert events[1].ranking_id == ranking_id
    assert events[0].feature_source_id == 12


def test_prov4_local_migration_upgrade_and_rollback(tmp_path: Path) -> None:
    path = Path("alembic/versions/20260716_0012_runtime_provenance_events.py")
    spec = importlib.util.spec_from_file_location("prov4_migration", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    for table in (Market.__table__, MarketSnapshot.__table__, Forecast.__table__,
                  MarketRanking.__table__):
        table.create(engine, checkfirst=True)
    with engine.begin() as connection:
        module.upgrade_bind(connection)
    assert "runtime_provenance_events" in inspect(engine).get_table_names()
    with engine.begin() as connection:
        module.downgrade_bind(connection)
    remaining = inspect(engine).get_table_names()
    assert "runtime_provenance_events" not in remaining
    assert "forecasts" in remaining and "market_rankings" in remaining
