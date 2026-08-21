#!/usr/bin/env python3
"""Rebuild linked crypto volatility fields with point-in-time interval normalization."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from kalshi_predictor.crypto.features import calculate_crypto_features
from kalshi_predictor.data.schema import CryptoFeature, CryptoPrice


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    engine = create_engine(f"sqlite:///{args.database}", connect_args={"timeout": 120})
    rows: list[dict[str, Any]] = []
    with Session(engine) as session:
        feature_ids = list(
            session.scalars(
                text(
                    """
                    SELECT DISTINCT CAST(
                      json_extract(feature_json,'$.crypto_feature_id') AS INTEGER
                    )
                    FROM forecasts f JOIN settlements s ON s.ticker=f.ticker
                    WHERE f.model_name='crypto_v2' AND s.result IN ('yes','no')
                      AND json_extract(f.feature_json,'$.crypto_feature_id') IS NOT NULL
                    """
                )
            )
        )
        for feature_id in feature_ids:
            feature = session.get(CryptoFeature, feature_id)
            if feature is None:
                rows.append({"feature_id": feature_id, "status": "FEATURE_NOT_FOUND"})
                continue
            rows.append(_rebuild_one(session, feature))
        session.commit()
    engine.dispose()

    payload = {
        "mode": "POINT_IN_TIME_INTERVAL_NORMALIZED_VOLATILITY_REBUILD",
        "future_prices_allowed": False,
        "features_reviewed": len(rows),
        "status_counts": dict(Counter(row["status"] for row in rows)),
        "rows": rows,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: value for key, value in payload.items() if key != "rows"}))
    return 0


def _rebuild_one(session: Session, feature: CryptoFeature) -> dict[str, Any]:
    raw = json.loads(feature.raw_json or "{}")
    cutoff = _cutoff(raw, feature.generated_at)
    start = cutoff - timedelta(minutes=max(1500, feature.window_minutes + 60))
    prices = list(
        session.scalars(
            select(CryptoPrice)
            .where(
                CryptoPrice.symbol == feature.symbol,
                CryptoPrice.observed_at >= start,
                CryptoPrice.observed_at <= cutoff,
            )
            .order_by(CryptoPrice.observed_at, CryptoPrice.id)
        )
    )
    calculated = calculate_crypto_features(prices, window_minutes=feature.window_minutes)
    keys = ("volatility_1h", "volatility_4h", "volatility_24h")
    if any(calculated.get(key) is None for key in keys):
        return {
            "feature_id": feature.id,
            "symbol": feature.symbol,
            "cutoff": cutoff.isoformat(),
            "price_rows": len(prices),
            "status": "INSUFFICIENT_POINT_IN_TIME_PRICES",
        }
    before = {key: getattr(feature, key) for key in keys}
    for key in keys:
        setattr(feature, key, str(calculated[key]))
    raw.update(
        {
            "feature_version": "crypto_features_v3_interval_normalized",
            "volatility_unit": "simple_return_per_sqrt_minute",
            "volatility_rebuild": {
                "cutoff": cutoff.isoformat(),
                "future_prices_allowed": False,
                "source_price_rows": len(prices),
                "previous_values": before,
            },
        }
    )
    feature.raw_json = json.dumps(raw, separators=(",", ":"), sort_keys=True)
    return {
        "feature_id": feature.id,
        "symbol": feature.symbol,
        "cutoff": cutoff.isoformat(),
        "price_rows": len(prices),
        "status": "REBUILT",
        "before": before,
        "after": {key: getattr(feature, key) for key in keys},
    }


def _cutoff(raw: dict[str, Any], generated_at: datetime) -> datetime:
    value = raw.get("source_latest_observed_at")
    if value:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return generated_at


if __name__ == "__main__":
    raise SystemExit(main())
