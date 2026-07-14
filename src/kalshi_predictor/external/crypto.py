from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.crypto.ingestion import ingest_manual_crypto_json
from kalshi_predictor.external.base import ExternalIngestionResult, store_external_payload


class CryptoProvider:
    """Placeholder provider for manually supplied BTC/ETH price history JSON."""

    source = "crypto"

    def ingest_payload(
        self,
        session: Session,
        payload: Mapping[str, Any],
    ) -> ExternalIngestionResult:
        generic_result = store_external_payload(session, source=self.source, payload=payload)
        crypto_result = ingest_manual_crypto_json(session, payload, source="manual")
        return ExternalIngestionResult(
            source=self.source,
            records_inserted=generic_result.records_inserted + crypto_result.prices_inserted,
        )


def ingest_crypto_json(session: Session, payload: Mapping[str, Any]) -> ExternalIngestionResult:
    return CryptoProvider().ingest_payload(session, payload)
