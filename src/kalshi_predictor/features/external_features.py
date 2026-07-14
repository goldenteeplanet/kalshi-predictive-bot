from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.features.repository import (
    feature_payload,
    get_latest_features_for_ticker,
)

EXTERNAL_FEATURE_SETS = ("weather", "crypto", "economic")


def build_external_features(session: Session, ticker: str) -> dict[str, Any]:
    payloads: dict[str, Any] = {}
    for feature_set in EXTERNAL_FEATURE_SETS:
        record = get_latest_features_for_ticker(
            session,
            ticker,
            feature_set_name=feature_set,
            include_global=True,
        )
        payload = feature_payload(record)
        if payload:
            payloads[feature_set] = payload
    return payloads

