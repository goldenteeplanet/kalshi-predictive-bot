"""Bind the existing liquidity score to fresh, archived catalog inputs."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from kalshi_predictor.opportunities.scoring import score_liquidity

FIELDS = ("volume_fp", "open_interest_fp", "liquidity_dollars")
METHOD = "CATALOG_OPPORTUNITY_LIQUIDITY_V1"


def catalog_liquidity(
    market: Mapping[str, Any],
    *,
    catalog_sha256: str,
    received_at: datetime,
    decision_at: datetime,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "method_version": METHOD,
        "catalog_sha256": catalog_sha256,
        "received_at": received_at.isoformat(),
        "decision_at": decision_at.isoformat(),
        "max_age_seconds": 60,
        "score": None,
        "status": "UNKNOWN",
        "inputs": {},
    }
    if not re.fullmatch(r"[0-9a-f]{64}", catalog_sha256):
        result["reason"] = "CATALOG_HASH_REQUIRED"
        return result
    if received_at.utcoffset() is None or decision_at.utcoffset() is None:
        result["reason"] = "AWARE_CATALOG_CLOCKS_REQUIRED"
        return result
    if not 0 <= (decision_at - received_at).total_seconds() <= 60:
        result["reason"] = "FRESH_CATALOG_REQUIRED"
        return result
    values = {}
    for key in FIELDS:
        raw = market.get(key)
        if raw is None or isinstance(raw, bool):
            result["reason"] = "COMPLETE_CATALOG_LIQUIDITY_INPUTS_REQUIRED"
            return result
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            result["reason"] = "FINITE_NONNEGATIVE_CATALOG_INPUTS_REQUIRED"
            return result
        if not value.is_finite() or value < 0:
            result["reason"] = "FINITE_NONNEGATIVE_CATALOG_INPUTS_REQUIRED"
            return result
        values[key] = value
    result["inputs"] = {key: str(value) for key, value in values.items()}
    result["score"] = score_liquidity(
        volume=values["volume_fp"],
        open_interest=values["open_interest_fp"],
        liquidity=values["liquidity_dollars"],
    )
    result["status"] = "DERIVED_FROM_FRESH_CATALOG"
    result["reason"] = "EXISTING_OPPORTUNITY_SCORING_FORMULA"
    return result
