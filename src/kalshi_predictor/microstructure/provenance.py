"""Explicit receipt-bound inputs for prospective microstructure research.

Receipts must come from the capture layer. These checks bind supplied evidence;
they do not attest clocks externally or qualify a trading model.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.data.schema import Forecast, MarketSnapshot, MicrostructureFeature


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("AWARE_CLOCK_REQUIRED")
    return value.astimezone(UTC)


def record_time(value: datetime) -> datetime:
    # SQLAlchemy's SQLite DateTime loses UTC offsets on stored UTC records.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else aware(value)


def bound_close_time(raw_market: dict[str, Any]) -> datetime:
    value = raw_market.get("close_time")
    if not isinstance(value, str):
        raise ValueError("BOUND_SNAPSHOT_CLOSE_TIME_REQUIRED")
    return aware(datetime.fromisoformat(value.replace("Z", "+00:00")))


def record_hash(record: MarketSnapshot | Forecast | MicrostructureFeature) -> str:
    values = {}
    for column in record.__table__.columns:
        value = getattr(record, column.name)
        values[column.name] = (
            record_time(value).isoformat() if isinstance(value, datetime) else value
        )
    return hashlib.sha256(json.dumps(values, sort_keys=True, allow_nan=False).encode()).hexdigest()


def settings_hash(settings: Settings) -> str:
    values = {
        key: str(value)
        for key, value in settings.model_dump().items()
        if key.startswith("microstructure_")
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class RecordReceipt:
    record_id: int
    record_sha256: str
    received_at: datetime

    def verify(self, record: MarketSnapshot | Forecast, cutoff: datetime) -> dict[str, Any]:
        if type(self.record_id) is not int or self.record_id <= 0:
            raise ValueError("POSITIVE_RECORD_ID_REQUIRED")
        observed = (
            record.captured_at if isinstance(record, MarketSnapshot) else record.forecasted_at
        )
        if (
            record.id != self.record_id
            or record_hash(record) != self.record_sha256
            or not record_time(observed) <= aware(self.received_at) <= aware(cutoff)
        ):
            raise ValueError("INPUT_HASH_OR_VISIBILITY_MISMATCH")
        return {
            "id": self.record_id,
            "sha256": self.record_sha256,
            "received_at": aware(self.received_at).isoformat(),
            "observed_at": record_time(observed).isoformat(),
        }


@dataclass(frozen=True)
class FeatureBuildContext:
    model_input_as_of: datetime
    reference_at: datetime
    snapshots: tuple[RecordReceipt, ...]
    ensemble: RecordReceipt | None

    def bind(
        self,
        session: Session,
        snapshots: list[MarketSnapshot],
        lookback_minutes: int,
        settings: Settings,
    ) -> tuple[dict[str, Any], Forecast]:
        cutoff = aware(self.model_input_as_of)
        if (
            type(lookback_minutes) is not int
            or type(settings.microstructure_min_snapshots) is not int
            or settings.microstructure_min_snapshots <= 0
        ):
            raise ValueError("POSITIVE_INTEGER_WINDOW_AND_QUORUM_REQUIRED")
        if cutoff != aware(self.reference_at):
            raise ValueError("FEATURE_CLOCK_ORDER")
        if (
            lookback_minutes <= 0
            or len(snapshots) < settings.microstructure_min_snapshots
            or len(snapshots) != len(self.snapshots)
            or not snapshots
        ):
            raise ValueError("INSUFFICIENT_BOUND_SNAPSHOTS")
        if len({s.id for s in snapshots}) != len(snapshots):
            raise ValueError("DUPLICATE_SNAPSHOT")
        times = [record_time(s.captured_at) for s in snapshots]
        if times != sorted(times) or len(set(times)) != len(times):
            raise ValueError("SNAPSHOT_TIME_ORDER")
        if times[0] < cutoff - timedelta(minutes=lookback_minutes):
            raise ValueError("SNAPSHOT_OUTSIDE_LOOKBACK")
        ticker = snapshots[-1].ticker
        if any(s.ticker != ticker for s in snapshots):
            raise ValueError("SNAPSHOT_TICKER_MISMATCH")
        proofs = [r.verify(s, cutoff) for r, s in zip(self.snapshots, snapshots, strict=True)]
        for snapshot in snapshots:
            for name in (
                "best_yes_bid",
                "best_yes_ask",
                "best_no_bid",
                "best_no_ask",
                "spread",
                "last_price_dollars",
                "volume_fp",
            ):
                value = getattr(snapshot, name)
                if value is not None and not Decimal(value).is_finite():
                    raise ValueError("NONFINITE_SNAPSHOT_VALUE")
        if self.ensemble is None:
            raise ValueError("BOUND_ENSEMBLE_REQUIRED_NO_ABSENCE_PROOF")
        if type(self.ensemble.record_id) is not int or self.ensemble.record_id <= 0:
            raise ValueError("POSITIVE_RECORD_ID_REQUIRED")
        ensemble = session.get(Forecast, self.ensemble.record_id)
        if ensemble is None or ensemble.ticker != ticker or ensemble.model_name != "ensemble_v2":
            raise ValueError("BOUND_ENSEMBLE_REQUIRED")
        ensemble_proof = self.ensemble.verify(ensemble, cutoff)
        return {
            "schema": "microstructure-cutoff-lineage-v1",
            "ticker": ticker,
            "model_input_as_of": cutoff.isoformat(),
            "reference_at": aware(self.reference_at).isoformat(),
            "snapshots": proofs,
            "ensemble": ensemble_proof,
            "settings_sha256": settings_hash(settings),
            "clock_authority": "SUPPLIED_CAPTURE_RECEIPTS_NOT_EXTERNAL_ATTESTATION",
            "release_certified": False,
        }, ensemble
