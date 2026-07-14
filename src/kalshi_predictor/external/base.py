import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.orm import Session

from kalshi_predictor.features.repository import insert_features
from kalshi_predictor.utils.time import parse_datetime


@dataclass(frozen=True)
class ExternalIngestionResult:
    source: str
    records_inserted: int


class ExternalDataProvider(Protocol):
    source: str

    def ingest_payload(
        self,
        session: Session,
        payload: Mapping[str, Any],
    ) -> ExternalIngestionResult:
        """Store externally supplied JSON without requiring live credentials."""


def load_json_file(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("External input JSON must be an object.")
    return data


def store_external_payload(
    session: Session,
    *,
    source: str,
    payload: Mapping[str, Any],
) -> ExternalIngestionResult:
    ticker = str(payload.get("ticker") or payload.get("market_ticker") or "*")
    feature_payload = payload.get("features")
    features = feature_payload if isinstance(feature_payload, Mapping) else payload
    source_timestamp = parse_datetime(
        payload.get("source_timestamp") or payload.get("timestamp") or payload.get("observed_at")
    )
    insert_features(
        session,
        ticker=ticker,
        feature_set_name=source,
        features=dict(features),
        raw_source=dict(payload),
        source_timestamp=source_timestamp,
    )
    return ExternalIngestionResult(source=source, records_inserted=1)

