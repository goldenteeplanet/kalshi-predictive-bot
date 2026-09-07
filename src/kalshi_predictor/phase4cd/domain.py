from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

HISTORICAL_REPLAY = "HISTORICAL_REPLAY"
SHADOW = "SHADOW"
GUARDED_PAPER = "GUARDED_PAPER"
SOURCE_LANES = frozenset({HISTORICAL_REPLAY, SHADOW, GUARDED_PAPER})


class EvidenceLevel(StrEnum):
    NO_EVIDENCE = "NO_EVIDENCE"
    EARLY = "EARLY"
    PRELIMINARY = "PRELIMINARY"
    USEFUL = "USEFUL"
    STRONG = "STRONG"


def evidence_level(independent_event_n: int) -> EvidenceLevel:
    """Descriptive support level, not a claim of statistical certainty."""
    if independent_event_n <= 0:
        return EvidenceLevel.NO_EVIDENCE
    if independent_event_n < 10:
        return EvidenceLevel.EARLY
    if independent_event_n < 30:
        return EvidenceLevel.PRELIMINARY
    if independent_event_n < 100:
        return EvidenceLevel.USEFUL
    return EvidenceLevel.STRONG


def independent_event_id(
    *, ticker: str, event_ticker: str | None, series_ticker: str | None
) -> str:
    """Group sibling threshold contracts into one independent event."""
    if event_ticker:
        return event_ticker
    normalized = re.sub(r"(?:-|_)?(?:above|below|gt|lt)?\d+(?:\.\d+)?[km]?\b", "", ticker.lower())
    normalized = re.sub(r"[-_]+", "-", normalized).strip("-")
    return f"{series_ticker or 'UNKNOWN'}:{normalized or ticker.lower()}"


def correlation_cluster_id(*, independent_id: str, series_ticker: str | None) -> str:
    return f"{series_ticker or 'UNKNOWN'}:{independent_id}"


def deterministic_id(namespace: str, *parts: Any) -> str:
    raw = "|".join([namespace, *(str(part or "") for part in parts)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def settlement_value(result: str | None) -> int | None:
    normalized = (result or "").strip().lower()
    if normalized in {"yes", "y", "1", "true"}:
        return 1
    if normalized in {"no", "n", "0", "false"}:
        return 0
    return None


def score_probability(probability: Decimal, outcome: int) -> tuple[Decimal, Decimal]:
    clipped = min(max(float(probability), 1e-15), 1 - 1e-15)
    brier = (clipped - outcome) ** 2
    log_loss = -(outcome * math.log(clipped) + (1 - outcome) * math.log(1 - clipped))
    return Decimal(str(brier)), Decimal(str(log_loss))


def validate_point_in_time(
    *,
    feature_timestamp: datetime | None,
    source_timestamp: datetime | None,
    snapshot_timestamp: datetime,
    decision_timestamp: datetime,
    settlement_timestamp: datetime,
) -> None:
    checks = {
        "feature_timestamp": feature_timestamp,
        "source_timestamp": source_timestamp,
        "snapshot_timestamp": snapshot_timestamp,
    }
    for name, timestamp in checks.items():
        if timestamp is not None and timestamp > decision_timestamp:
            raise ValueError(f"LOOKAHEAD:{name}")
    if settlement_timestamp <= decision_timestamp:
        raise ValueError("LOOKAHEAD:settlement_timestamp")


@dataclass(frozen=True)
class EvidenceCounts:
    contract_n: int
    ticker_n: int
    event_n: int
    series_n: int
    independent_event_n: int
    correlation_cluster_n: int
