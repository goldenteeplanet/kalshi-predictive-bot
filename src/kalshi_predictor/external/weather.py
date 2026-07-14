from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.external.base import ExternalIngestionResult, store_external_payload
from kalshi_predictor.weather.ingestion import ingest_manual_weather_json


class WeatherProvider:
    """Store manually supplied weather JSON in generic and weather-specific tables."""

    source = "weather"

    def ingest_payload(
        self,
        session: Session,
        payload: Mapping[str, Any],
    ) -> ExternalIngestionResult:
        generic_result = store_external_payload(session, source=self.source, payload=payload)
        weather_result = ingest_manual_weather_json(session, payload, source="manual")
        return ExternalIngestionResult(
            source=self.source,
            records_inserted=(
                generic_result.records_inserted
                + weather_result.forecasts_inserted
                + weather_result.observations_inserted
            ),
        )


def ingest_weather_json(session: Session, payload: Mapping[str, Any]) -> ExternalIngestionResult:
    return WeatherProvider().ingest_payload(session, payload)
