from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

import kalshi_predictor.memory.capture as memory_capture
from kalshi_predictor.data.repositories import insert_forecast
from kalshi_predictor.data.schema import (
    Forecast, Market, MarketRanking, MarketSnapshot, RuntimeProvenanceEvent,
)
from kalshi_predictor.opportunities.repository import insert_market_ranking

memory_capture.capture_forecast_created = lambda *args, **kwargs: None
memory_capture.capture_market_ranking = lambda *args, **kwargs: None
now = datetime(2026, 7, 17, tzinfo=timezone.utc)

def market(session):
    row = Market(ticker="PROV7-SYN", raw_json="{}", first_seen_at=now, last_seen_at=now)
    session.add(row); session.flush(); return row

def payload():
    return {"ticker": "PROV7-SYN", "forecasted_at": now,
            "model_name": "crypto_v2", "yes_probability": Decimal("0.60"),
            "feature_json": {"crypto_feature_id": 12,
                             "source_observation_ref": {"table": "crypto_prices", "id": 9}}}

legacy = Path("/tmp/prov7_legacy.db"); legacy.unlink(missing_ok=True)
engine = create_engine(f"sqlite:///{legacy}")
Market.__table__.create(engine); Forecast.__table__.create(engine)
with Session(engine) as session:
    market(session); insert_forecast(session, payload(), attribution_enabled=False); session.commit()
assert "runtime_provenance_events" not in inspect(engine).get_table_names()

dual = Path("/tmp/prov7_dual.db"); dual.unlink(missing_ok=True)
engine = create_engine(f"sqlite:///{dual}")
for table in (Market.__table__, MarketSnapshot.__table__, Forecast.__table__,
              MarketRanking.__table__, RuntimeProvenanceEvent.__table__):
    table.create(engine, checkfirst=True)
with Session(engine) as session:
    market(session)
    snapshot = MarketSnapshot(ticker="PROV7-SYN", captured_at=now, raw_market_json="{}")
    session.add(snapshot); session.flush()
    forecast = insert_forecast(session, payload(), market_snapshot_id=snapshot.id,
                               attribution_enabled=True)
    ranking = insert_market_ranking(session, {
        "ticker": "PROV7-SYN", "forecast_model": "crypto_v2",
        "forecast_id": forecast.id, "market_snapshot_id": snapshot.id,
        "ranked_at": now, "raw_json": {},
    }, attribution_enabled=True)
    ranking_id = ranking.id
    session.commit()
    events = list(session.scalars(select(RuntimeProvenanceEvent).order_by(RuntimeProvenanceEvent.id)))
assert [event.stage for event in events] == ["FORECAST_CREATED", "RANKING_CREATED"]
assert events[0].previous_digest == "GENESIS"
assert events[1].previous_digest == events[0].provenance_digest
assert events[1].ranking_id == ranking_id
assert events[0].feature_source_id == 12
legacy.unlink(); dual.unlink()
print("PROV7_SYNTHETIC_SMOKE=PASS")
