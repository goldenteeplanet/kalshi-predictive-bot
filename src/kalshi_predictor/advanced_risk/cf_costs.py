"""Bind CF risk costs to persisted forecast inputs and replay original evidence."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_predictor.crypto.cost_record import (
    cost_decision_from_qualification,
    replay_cost_record,
)
from kalshi_predictor.crypto.public_paper_costs import replayed_fee_supports_paper
from kalshi_predictor.data.schema import Forecast, Market
from kalshi_predictor.overnight_paper.cf_source import CLOCK_BASIS
from kalshi_predictor.paper.models import PaperDecision


def cf_risk_cost_scope(
    *, forecast: Forecast, market: Market, decision: PaperDecision, at: datetime,
    account_identity_sha256: str,
) -> dict[str, Any]:
    """Freeze pre-risk identity without circular sizing/risk result references.

    The final qualification record may add downstream provenance. Its cost
    evidence must be replayed again for that final decision by the assembler.
    This scope does not certify source semantics or authorize execution.
    """
    features = json.loads(forecast.feature_json)
    if (
        not isinstance(features, dict) or features.get("source_kind") != CLOCK_BASIS
        or decision.forecast_id != forecast.id or decision.ticker != forecast.ticker
        or market.ticker != forecast.ticker or market.series_ticker != "KXSOLE"
        or not market.event_ticker or decision.model_name != forecast.model_name
        or decision.probability != Decimal(forecast.yes_probability)
        or type(decision.quantity) is not int or decision.quantity != 1
        or at.utcoffset() is None
    ):
        raise ValueError("CF_RISK_PERSISTED_FORECAST_BINDING_REQUIRED")
    hashes = features.get("source_hashes")
    required_hashes = (
        account_identity_sha256,
        features.get("model_artifact_sha256"), features.get("cf_input_sha256"),
        features.get("cf_target_sha256"),
    )
    if (
        not isinstance(hashes, list) or not 0 < len(hashes) <= 200
        or any(not isinstance(h, str) or len(h) != 64
               or any(c not in "0123456789abcdef" for c in h)
               for h in (*required_hashes, *hashes))
        or len(set(hashes)) != len(hashes)
    ):
        raise ValueError("CF_RISK_PERSISTED_SOURCE_BINDING_REQUIRED")
    return cost_decision_from_qualification(dict(
        ticker=forecast.ticker, event_id=market.event_ticker, series=market.series_ticker,
        forecast_id=forecast.id, model_name=forecast.model_name,
        forecast_probability=str(decision.probability), side=decision.side,
        executable_price=str(decision.limit_price), decision_at=at.isoformat(),
        source_kind=CLOCK_BASIS, model_artifact_sha256=features["model_artifact_sha256"],
        account_identity_sha256=account_identity_sha256,
        cf_input_sha256=features["cf_input_sha256"],
        cf_target_sha256=features["cf_target_sha256"], source_hashes=list(hashes),
        calibration_segment=features.get("calibration_segment") or "UNDECLARED",
    ))


def replay_cf_risk_costs(
    *, record: dict[str, Any], forecast: Forecast, market: Market,
    decision: PaperDecision, at: datetime,
    account_identity_sha256: str,
) -> tuple[Decimal, Decimal, Decimal]:
    """No configured fee/slippage/tail defaults and no trusted stored verdicts."""
    scope = cf_risk_cost_scope(
        forecast=forecast, market=market, decision=decision, at=at,
        account_identity_sha256=account_identity_sha256,
    )
    if record.get("request", {}).get("account_identity_sha256") != account_identity_sha256:
        raise ValueError("CF_RISK_ACCOUNT_IDENTITY_MISMATCH")
    assessed = replay_cost_record(record, expected_decision=scope)
    values = []
    execution_component = ('execution_price_impact' if 'execution_price_impact' in assessed
                           else 'observed_book_stress')
    for name in ("exchange_fee", execution_component, "uncertainty"):
        component = assessed[name]
        if (
            component["value"] is None or component["paper_support"] is not True
            or component["status"] not in ("CERTIFIED", "ESTIMATED_WITH_SUPPORT")
            or (name == "exchange_fee" and not replayed_fee_supports_paper(component))
        ):
            raise ValueError("CF_RISK_SUPPORTED_COST_REQUIRED:" + name)
        value = Decimal(component["value"])
        if not value.is_finite() or value < 0:
            raise ValueError("CF_RISK_FINITE_NONNEGATIVE_COST_REQUIRED")
        values.append(value)
    if assessed["full_net_ev_status"] != "FULL_NET_EV_KNOWN":
        raise ValueError("CF_RISK_FULL_COST_EVIDENCE_REQUIRED")
    return values[0], values[1], values[2]
