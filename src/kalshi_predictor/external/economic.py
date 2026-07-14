from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.external.base import ExternalIngestionResult, store_external_payload


class EconomicProvider:
    """Placeholder provider for manually supplied CPI/Fed/jobs/economic event JSON."""

    source = "economic"

    def ingest_payload(
        self,
        session: Session,
        payload: Mapping[str, Any],
    ) -> ExternalIngestionResult:
        return store_external_payload(session, source=self.source, payload=payload)


def ingest_economic_json(session: Session, payload: Mapping[str, Any]) -> ExternalIngestionResult:
    return EconomicProvider().ingest_payload(session, payload)

