from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.economic.repository import insert_economic_event
from kalshi_predictor.utils.time import parse_datetime, utc_now


@dataclass(frozen=True)
class EconomicIngestionSummary:
    source: str
    events_inserted: int
    errors: list[str]


def ingest_economic_file_payload(
    session: Session,
    payload: Mapping[str, Any],
    *,
    source: str = "manual",
) -> EconomicIngestionSummary:
    rows = payload.get("events")
    if rows is None:
        rows = [payload]
    if not isinstance(rows, list):
        return EconomicIngestionSummary(
            source=source,
            events_inserted=0,
            errors=["events must be a list"],
        )

    inserted = 0
    errors: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            errors.append(f"event {index} is not an object")
            continue
        event_time = parse_datetime(
            row.get("event_time")
            or row.get("time")
            or row.get("timestamp")
            or row.get("released_at")
        )
        if event_time is None:
            event_time = utc_now()
        event_key = str(row.get("event_key") or row.get("key") or row.get("category") or "economic")
        title = str(row.get("title") or event_key)
        insert_economic_event(
            session,
            event_key=event_key,
            source=str(row.get("source") or source),
            event_time=event_time,
            category=str(row.get("category") or event_key),
            title=title,
            actual_value=row.get("actual_value") or row.get("actual"),
            forecast_value=row.get("forecast_value") or row.get("forecast"),
            previous_value=row.get("previous_value") or row.get("previous"),
            raw_json=dict(row),
        )
        inserted += 1
    return EconomicIngestionSummary(source=source, events_inserted=inserted, errors=errors)
