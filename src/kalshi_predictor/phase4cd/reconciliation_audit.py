"""Pure Phase 4CD reconciliation validation helpers."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def normalized_timestamp(value: object) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    parsed = parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    return parsed.isoformat()


def _lineage_timestamp(value: object) -> str | None:
    if value is None:
        return None
    try:
        return normalized_timestamp(value)
    except (TypeError, ValueError):
        return str(value)


def settlement_lineage_hash(values: Mapping[str, Any]) -> str:
    return canonical_hash(
        {
            "ticker": values.get("ticker"),
            "settled_at": _lineage_timestamp(values.get("settled_at")),
            "result": values.get("result"),
            "yes_settlement_value": values.get("yes_settlement_value"),
            "raw_json": values.get("raw_json"),
            "updated_at": _lineage_timestamp(values.get("updated_at")),
        }
    )


CAPTURE_LINEAGE_FIELDS = (
    "capture_id",
    "ticker",
    "event_ticker",
    "snapshot_id",
    "snapshot_timestamp",
    "snapshot_hash",
    "feature_ids_json",
    "feature_hashes_json",
    "source_observations_json",
    "market_probability",
    "crypto_probability",
    "model_versions_json",
    "comparator_lineage_json",
    "range_comparator_verdict_json",
    "best_yes_bid",
    "best_yes_ask",
    "spread",
    "liquidity",
    "bundle_hash",
)


def capture_lineage_hash(values: Mapping[str, Any]) -> str:
    lineage = {field: values.get(field) for field in CAPTURE_LINEAGE_FIELDS}
    lineage["snapshot_timestamp"] = _lineage_timestamp(lineage["snapshot_timestamp"])
    return canonical_hash(lineage)


def binary_outcome(result: object) -> int | None:
    normalized = str(result or "").strip().lower()
    return {"yes": 1, "no": 0}.get(normalized)


def valid_probability(value: object) -> bool:
    try:
        probability = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return probability.is_finite() and Decimal("0") <= probability <= Decimal("1")


def valid_hash(value: object) -> bool:
    rendered = str(value or "")
    return len(rendered) == 64 and all(character in "0123456789abcdef" for character in rendered)


def valid_json_container(value: object, *, allow_empty: bool = False) -> bool:
    try:
        decoded = json.loads(str(value))
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return isinstance(decoded, dict | list) and (allow_empty or bool(decoded))


def executable_evidence_reason(capture: Mapping[str, Any]) -> str | None:
    bid_value, ask_value = capture.get("best_yes_bid"), capture.get("best_yes_ask")
    if bid_value in (None, "") and ask_value in (None, ""):
        return None  # Phase 4CD uses market probability as the calibration midpoint.
    if bid_value in (None, "") or ask_value in (None, ""):
        return "ONE_SIDED_EXECUTABLE_BOOK"
    if not valid_probability(bid_value) or not valid_probability(ask_value):
        return "EXECUTABLE_PRICE_INVALID"
    if Decimal(str(bid_value)) > Decimal(str(ask_value)):
        return "EXECUTABLE_BOOK_CROSSED"
    return None


def calibration_score(probability: Decimal, outcome: int) -> tuple[str, str]:
    epsilon = Decimal("0.000000000001")
    likelihood = probability if outcome else 1 - probability
    return (
        str((probability - outcome) ** 2),
        str(-Decimal(str(math.log(float(max(epsilon, likelihood)))))),
    )


def prospective_evaluation_values(
    capture: Mapping[str, Any],
    settlement: Mapping[str, Any],
    *,
    fees: Decimal = Decimal("0"),
    slippage: Decimal = Decimal("0"),
    minimum_executable_edge: Decimal = Decimal("0.05"),
) -> dict[str, Any]:
    outcome = binary_outcome(settlement.get("result"))
    if outcome is None:
        raise ValueError("SETTLEMENT_RESULT_UNUSABLE")
    market_probability = Decimal(str(capture.get("market_probability")))
    model_probability = Decimal(str(capture.get("crypto_probability")))
    bid_value, ask_value = capture.get("best_yes_bid"), capture.get("best_yes_ask")
    bid = Decimal(str(bid_value)) if bid_value not in (None, "") else None
    ask = Decimal(str(ask_value)) if ask_value not in (None, "") else None
    midpoint = (bid + ask) / 2 if bid is not None and ask is not None else market_probability
    advantage = abs(model_probability - midpoint)
    crossing = (ask - bid) / 2 if bid is not None and ask is not None else Decimal("0")
    gross_edge = advantage - crossing
    net_edge = gross_edge - fees - slippage
    terminal_reason = (
        "EVALUATED_EXECUTABLE"
        if net_edge >= minimum_executable_edge
        else ("NON_POSITIVE_GROSS_EDGE" if gross_edge <= 0 else "CALIBRATION_ONLY")
    )
    market_brier, market_log_loss = calibration_score(market_probability, outcome)
    model_brier, model_log_loss = calibration_score(model_probability, outcome)
    evaluation_id = canonical_hash(
        [
            capture.get("capture_id"),
            normalized_timestamp(settlement.get("settled_at")),
            settlement.get("result"),
        ]
    )
    return {
        "evaluation_id": evaluation_id,
        "capture_id": capture.get("capture_id"),
        "independent_event_id": capture.get("event_ticker"),
        "settled_at": settlement.get("settled_at"),
        "settlement_hash": settlement_lineage_hash(settlement),
        "settlement_updated_at": settlement.get("updated_at"),
        "outcome": outcome,
        "market_probability": str(market_probability),
        "model_probability": str(model_probability),
        "market_brier": market_brier,
        "model_brier": model_brier,
        "market_log_loss": market_log_loss,
        "model_log_loss": model_log_loss,
        "probability_advantage": str(advantage),
        "crossing_spread_cost": str(crossing),
        "fees": str(fees),
        "slippage": str(slippage),
        "gross_edge": str(gross_edge),
        "net_edge": str(net_edge),
        "terminal_reason": terminal_reason,
        "hypothetical_pnl": str(net_edge) if terminal_reason == "EVALUATED_EXECUTABLE" else None,
        "capture_lineage_hash": capture_lineage_hash(capture),
    }
