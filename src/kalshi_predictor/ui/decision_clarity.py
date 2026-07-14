from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from kalshi_predictor.config import Settings
from kalshi_predictor.ui.market_display import classify_market_category, summarize_market_title
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now

PASS = "good"
WARN = "caution"
FAIL = "risk"
PENDING = "neutral"
INFO = "info"

WAITING_FOR_DATA = "WAITING_FOR_DATA"
NO_TRADE = "NO TRADE"
WATCH = "WATCH"
TRADE = "TRADE"
INTERESTING_NOT_EXECUTABLE = "INTERESTING_BUT_NOT_EXECUTABLE"


def build_decision_clarity(
    *,
    ranking: Any,
    market: Any | None,
    snapshot: Any | None,
    forecast: Any | None,
    feature_snapshot: Any | None,
    settlement: Any | None,
    market_legs: list[Any],
    sizing_decision: Any | None,
    risk_decision: Any | None,
    expected_value: Any,
    explanation: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    market_structure = build_market_structure(
        title=_field(market, "title") or _field(ranking, "title") or _field(ranking, "ticker"),
        ticker=str(_field(ranking, "ticker") or ""),
        event_ticker=_field(market, "event_ticker"),
        series_ticker=_field(ranking, "series_ticker") or _field(market, "series_ticker"),
        market_type=_field(market, "market_type"),
        market_legs=market_legs,
        market_raw_json=_field(market, "raw_json"),
    )
    values = _decision_values(ranking, snapshot, forecast, expected_value, settings)
    risk_gate = _risk_gate(risk_decision)
    settlement_gate = _settlement_gate(market, settlement, market_structure)
    blockers = _block_reasons(
        values=values,
        market_structure=market_structure,
        risk_gate=risk_gate,
        settlement_gate=settlement_gate,
        settings=settings,
    )
    positive_signal = values["edge"] > Decimal("0") and values["expected_value"] > Decimal("0")
    final_decision = _final_decision(values, blockers, positive_signal, risk_gate)
    final_action_label = _final_action_label(values, final_decision, positive_signal, blockers)
    execution_quality = _execution_quality(values, market_structure, blockers)
    opportunity_reasons = _dedupe_text(
        [
            explanation.get("top_reason"),
            explanation.get("primary_driver"),
            *list(explanation.get("supporting_signals") or []),
        ]
    )
    risk_reasons = _dedupe_text([*blockers, *list(explanation.get("risks") or [])])
    primary_block = (
        "None" if final_decision == TRADE else (risk_reasons[0] if risk_reasons else "None")
    )

    return {
        "phase": "3AI",
        "final_decision": final_decision,
        "final_decision_kind": _decision_kind(final_decision),
        "final_action_label": final_action_label,
        "model_direction": _model_direction(values),
        "primary_block_reason": primary_block,
        "secondary_block_reasons": [reason for reason in risk_reasons if reason != primary_block],
        "decision_waterfall": _decision_waterfall(
            values=values,
            risk_gate=risk_gate,
            settlement_gate=settlement_gate,
            final_action_label=final_action_label,
            final_decision=final_decision,
            settings=settings,
        ),
        "gate_chips": _gate_chips(
            values=values,
            risk_gate=risk_gate,
            settlement_gate=settlement_gate,
            final_decision=final_decision,
            final_action_label=final_action_label,
            settings=settings,
        ),
        "execution_quality": execution_quality,
        "execution_quality_kind": _execution_quality_kind(execution_quality),
        "opportunity_reasons": opportunity_reasons,
        "risk_reasons": risk_reasons,
        "tradable_conditions": _tradable_conditions(
            values=values,
            blockers=blockers,
            market_structure=market_structure,
            risk_gate=risk_gate,
            settlement_gate=settlement_gate,
            settings=settings,
        ),
        "market_structure": market_structure,
        "trace_details": _trace_details(
            ranking=ranking,
            snapshot=snapshot,
            forecast=forecast,
            feature_snapshot=feature_snapshot,
            sizing_decision=sizing_decision,
            risk_decision=risk_decision,
            values=values,
        ),
        "formats": {
            "score": _format_score(values["score"]),
            "model_probability": _format_percent(values["model_probability"]),
            "market_price": _format_cents(values["market_price"]),
            "edge": _format_cents(values["edge"]),
            "expected_value": _format_cents(values["expected_value"]),
            "spread": _format_cents(values["spread"]),
            "liquidity": values["liquidity_label"],
            "time_remaining": _format_time_with_freshness(values),
            "data_freshness": values["freshness_text"],
        },
        "rank_sort": _rank_sort(final_decision, values, final_action_label),
    }


def build_market_structure(
    *,
    title: Any,
    ticker: str,
    series_ticker: Any,
    market_type: Any,
    market_legs: list[Any],
    event_ticker: Any = None,
    market_raw_json: Any = None,
) -> dict[str, Any]:
    clean_title = _clean_text(title) or ticker or "Market"
    market_payload = _json_object(market_raw_json)
    selected_components = _selected_component_legs(market_payload)
    legs = [
        _leg_view(leg, component=_component_for_leg(selected_components, leg, index))
        for index, leg in enumerate(market_legs)
    ]
    categories = sorted(
        {leg["category"] for leg in legs if leg["category"] not in {"", "unknown", "general"}}
    )
    has_general_legs = any(leg["category"] == "general" for leg in legs)
    heuristic_category = classify_market_category(
        f"{clean_title} {ticker}",
        str(series_ticker or ""),
    )

    if len(categories) > 1:
        category = "CROSS_CATEGORY"
        parser_status = "CROSS_CATEGORY"
        confidence = "Conflicting parsed legs"
        reason = "Parsed legs map to more than one category."
    elif len(legs) > 1:
        category = _resolved_category(categories, heuristic_category, has_general_legs)
        parser_status = "UNSUPPORTED_MULTI_LEG"
        confidence = _leg_confidence(legs)
        reason = "Multiple component legs require verified component provenance before trading."
    elif legs:
        category = _resolved_category(categories, heuristic_category, has_general_legs)
        parser_status = "PARSED"
        confidence = _leg_confidence(legs)
        reason = legs[0].get("reason") or "Single parsed leg supplies the category."
    elif heuristic_category == "General":
        category = "UNKNOWN_CATEGORY"
        parser_status = "UNKNOWN_CATEGORY"
        confidence = "Low"
        reason = "No parsed legs or strong series heuristic identified a safe category."
    else:
        category = heuristic_category
        parser_status = "HEURISTIC_CATEGORY"
        confidence = "Heuristic"
        reason = "Category is inferred from market title and series ticker."

    unsupported_reason = ""
    if parser_status in {"CROSS_CATEGORY", "UNKNOWN_CATEGORY", "UNSUPPORTED_MULTI_LEG"}:
        unsupported_reason = reason

    headline = _clean_headline(
        clean_title=clean_title,
        ticker=ticker,
        category=category,
        parser_status=parser_status,
        leg_count=len(legs),
        legs=legs,
    )
    event_value = _clean_text(event_ticker or market_payload.get("event_ticker"))
    series_value = _clean_text(series_ticker or market_payload.get("series_ticker"))
    contract_yes_summary = _contract_yes_summary(legs)
    contract_no_summary = _contract_no_summary(legs)
    kalshi_search_title = _kalshi_search_title(
        raw_title=clean_title,
        ticker=ticker,
        legs=legs,
    )
    kalshi_search_query = _kalshi_search_query(
        raw_title=clean_title,
        ticker=ticker,
        event_ticker=event_value,
        series_ticker=series_value,
        legs=legs,
    )
    component_market_tickers = [
        value
        for value in dict.fromkeys(leg["component_market_ticker"] for leg in legs)
        if value
    ]
    return {
        "clean_title": headline,
        "raw_title": clean_title,
        "kalshi_search_title": kalshi_search_title,
        "kalshi_search_query": kalshi_search_query,
        "component_market_tickers": component_market_tickers,
        "component_market_ticker_text": ", ".join(component_market_tickers),
        "contract_short_title": _contract_short_title(legs),
        "contract_yes_summary": contract_yes_summary,
        "contract_no_summary": contract_no_summary,
        "lookup_guidance": _lookup_guidance(parser_status=parser_status, legs=legs),
        "parser_label": _parser_label(parser_status, len(legs)),
        "category": category,
        "category_confidence": confidence,
        "mapping_reason": reason,
        "parser_status": parser_status,
        "unsupported_reason": unsupported_reason,
        "market_type": str(market_type or "unknown"),
        "event_ticker": event_value,
        "series_ticker": series_value,
        "kalshi_lookup": _kalshi_lookup(
            ticker=ticker,
            event_ticker=event_value,
            search_query=kalshi_search_query,
        ),
        "option_list": legs,
        "leg_count": len(legs),
        "is_unsupported": parser_status
        in {"CROSS_CATEGORY", "UNKNOWN_CATEGORY", "UNSUPPORTED_MULTI_LEG"},
    }


def _decision_values(
    ranking: Any,
    snapshot: Any | None,
    forecast: Any | None,
    expected_value: Any,
    settings: Settings,
) -> dict[str, Any]:
    market_price = to_decimal(_field(ranking, "best_price"))
    model_probability = (
        to_decimal(_field(ranking, "forecast_probability"))
        or to_decimal(_field(forecast, "yes_probability"))
        or Decimal("0")
    )
    edge = to_decimal(_field(ranking, "estimated_edge")) or Decimal("0")
    ev = to_decimal(expected_value) or edge
    spread = to_decimal(_field(ranking, "spread")) or to_decimal(_field(snapshot, "spread"))
    score = to_decimal(_field(ranking, "opportunity_score")) or Decimal("0")
    confidence = to_decimal(_field(ranking, "model_confidence_score")) or Decimal("0")
    liquidity_raw = (
        to_decimal(_field(ranking, "liquidity"))
        or to_decimal(_field(snapshot, "liquidity_dollars"))
    )
    liquidity_score = to_decimal(_field(ranking, "liquidity_score")) or Decimal("0")
    freshness = _freshness(snapshot, settings=settings)
    time_to_close = to_decimal(_field(ranking, "time_to_close_minutes"))
    no_liquidity = liquidity_score <= Decimal("0") and (
        liquidity_raw is None or liquidity_raw <= Decimal("0")
    )
    return {
        "ticker": _field(ranking, "ticker"),
        "side": str(_field(ranking, "best_side") or ""),
        "market_price": market_price,
        "model_probability": model_probability,
        "edge": edge,
        "expected_value": ev,
        "spread": spread,
        "score": score,
        "confidence": confidence,
        "liquidity_raw": liquidity_raw,
        "liquidity_score": liquidity_score,
        "liquidity_label": _liquidity_label(liquidity_raw, liquidity_score),
        "liquidity_detail": _liquidity_detail(liquidity_raw, liquidity_score),
        "no_liquidity": no_liquidity,
        "snapshot_present": snapshot is not None,
        "forecast_present": forecast is not None,
        "fresh": freshness["fresh"],
        "freshness_text": freshness["text"],
        "time_to_close_minutes": time_to_close,
    }


def _block_reasons(
    *,
    values: dict[str, Any],
    market_structure: dict[str, Any],
    risk_gate: dict[str, Any],
    settlement_gate: dict[str, Any],
    settings: Settings,
) -> list[str]:
    reasons: list[str] = []
    if market_structure["is_unsupported"]:
        reasons.append(market_structure["unsupported_reason"] or "Unsupported market structure.")
    if values["side"] not in {"BUY_YES", "BUY_NO"}:
        reasons.append("Model direction is missing or unsupported.")
    if values["market_price"] is None:
        reasons.append("Market price is missing.")
    if not values["forecast_present"] and values["model_probability"] <= Decimal("0"):
        reasons.append("No current model forecast is available.")
    if not values["snapshot_present"]:
        reasons.append("No quote snapshot is available.")
    elif not values["fresh"]:
        reasons.append(values["freshness_text"])
    if values["expected_value"] <= Decimal("0"):
        reasons.append("Expected value is not positive.")
    elif values["expected_value"] < settings.opportunity_min_edge:
        reasons.append(
            "Expected value is below the configured minimum edge "
            f"({_format_cents(settings.opportunity_min_edge)})."
        )
    if values["score"] < settings.opportunity_min_score:
        reasons.append(
            "Opportunity score is below the configured threshold "
            f"({_format_score(settings.opportunity_min_score)})."
        )
    if values["confidence"] < Decimal("30"):
        reasons.append("Model confidence is below the minimum review threshold (30.0).")
    if values["no_liquidity"]:
        reasons.append("No executable liquidity is available.")
    elif (
        values["liquidity_raw"] is not None
        and values["liquidity_raw"] < settings.opportunity_min_liquidity
    ):
        reasons.append(
            "Liquidity is below the configured threshold "
            f"({settings.opportunity_min_liquidity})."
        )
    if values["spread"] is None:
        reasons.append("Spread is missing from the latest quote.")
    elif values["spread"] > settings.opportunity_max_spread:
        reasons.append(
            "Spread is wider than the configured threshold "
            f"({_format_cents(settings.opportunity_max_spread)})."
        )
    if risk_gate["status"] == FAIL:
        reasons.append(risk_gate["detail"])
    elif risk_gate["status"] == PENDING:
        reasons.append("Phase 3N risk result is not available yet.")
    if settlement_gate["status"] == FAIL:
        reasons.append(settlement_gate["detail"])
    return _dedupe_text(reasons)


def _final_decision(
    values: dict[str, Any],
    blockers: list[str],
    positive_signal: bool,
    risk_gate: dict[str, Any],
) -> str:
    missing_data = any(
        text.startswith(("No current model forecast", "No quote snapshot", "Spread is missing"))
        or "Latest data is about" in text
        for text in blockers
    )
    if missing_data:
        return WAITING_FOR_DATA
    if not positive_signal:
        return NO_TRADE
    hard_block = any(
        token in reason
        for reason in blockers
        for token in (
            "Unsupported",
            "Multiple component",
            "more than one category",
            "UNKNOWN",
            "missing or unsupported",
            "Market price is missing",
            "No executable liquidity",
            "wider than",
            "Phase 3N blocked",
            "Expected value is not positive",
        )
    )
    if hard_block:
        return NO_TRADE
    if blockers or risk_gate["status"] != PASS:
        return WATCH
    return TRADE


def _final_action_label(
    values: dict[str, Any],
    final_decision: str,
    positive_signal: bool,
    blockers: list[str],
) -> str:
    if positive_signal and values["no_liquidity"]:
        return INTERESTING_NOT_EXECUTABLE
    if final_decision == TRADE:
        return "PAPER_REVIEW_READY"
    if final_decision == WATCH:
        return "WATCH_FOR_CLEAN_GATES"
    if final_decision == WAITING_FOR_DATA:
        return "WAITING_FOR_DATA"
    if any("wider than" in reason for reason in blockers):
        return "NO_TRADE_WIDE_SPREAD"
    return "NO_TRADE"


def _execution_quality(
    values: dict[str, Any],
    market_structure: dict[str, Any],
    blockers: list[str],
) -> str:
    if market_structure["is_unsupported"]:
        return "Unsupported Market"
    if values["no_liquidity"]:
        return "No Liquidity"
    if not values["fresh"]:
        return "Stale Quote"
    if values["spread"] is not None and any("wider than" in reason for reason in blockers):
        return "Wide Spread"
    if values["liquidity_label"] == "Low":
        return "Thin"
    if blockers:
        return "Not Tradable"
    return "Clean"


def _decision_waterfall(
    *,
    values: dict[str, Any],
    risk_gate: dict[str, Any],
    settlement_gate: dict[str, Any],
    final_action_label: str,
    final_decision: str,
    settings: Settings,
) -> list[dict[str, str]]:
    return [
        _step(
            "Market price",
            _format_cents(values["market_price"]),
            values["market_price"] is not None,
        ),
        _step(
            "Model probability",
            _format_percent(values["model_probability"]),
            values["model_probability"] > Decimal("0"),
            detail=_model_direction(values),
        ),
        _step("Edge", _format_cents(values["edge"]), values["edge"] > Decimal("0")),
        _step(
            "Expected value",
            _format_cents(values["expected_value"]),
            values["expected_value"] >= settings.opportunity_min_edge,
            detail=f"Min {_format_cents(settings.opportunity_min_edge)}",
        ),
        _step(
            "Confidence check",
            f"{_format_score(values['score'])} / {_format_score(values['confidence'])}",
            values["score"] >= settings.opportunity_min_score
            and values["confidence"] >= Decimal("30"),
            detail="score / model confidence",
        ),
        _step(
            "Liquidity check",
            values["liquidity_label"],
            not values["no_liquidity"],
            detail=values["liquidity_detail"],
        ),
        _step(
            "Spread check",
            _format_cents(values["spread"]),
            values["spread"] is not None and values["spread"] <= settings.opportunity_max_spread,
            detail=f"Max {_format_cents(settings.opportunity_max_spread)}",
        ),
        {
            "label": "Risk check",
            "value": risk_gate["label"],
            "detail": risk_gate["detail"],
            "kind": risk_gate["status"],
        },
        {
            "label": "Settlement eligibility",
            "value": settlement_gate["label"],
            "detail": settlement_gate["detail"],
            "kind": settlement_gate["status"],
        },
        {
            "label": "Final action",
            "value": final_action_label,
            "detail": final_decision,
            "kind": _decision_kind(final_decision),
        },
    ]


def _gate_chips(
    *,
    values: dict[str, Any],
    risk_gate: dict[str, Any],
    settlement_gate: dict[str, Any],
    final_decision: str,
    final_action_label: str,
    settings: Settings,
) -> list[dict[str, str]]:
    return [
        _chip(
            "Forecast",
            values["model_probability"] > Decimal("0"),
            "Model probability available",
        ),
        _chip(
            "EV",
            values["expected_value"] >= settings.opportunity_min_edge,
            f"{_format_cents(values['expected_value'])}",
        ),
        _chip(
            "Confidence",
            values["score"] >= settings.opportunity_min_score
            and values["confidence"] >= Decimal("30"),
            f"Score {_format_score(values['score'])}",
        ),
        _chip("Liquidity", not values["no_liquidity"], values["liquidity_detail"]),
        _chip(
            "Spread",
            values["spread"] is not None and values["spread"] <= settings.opportunity_max_spread,
            _format_cents(values["spread"]),
        ),
        {"label": "Risk", "value": risk_gate["label"], "kind": risk_gate["status"]},
        {
            "label": "Settlement eligibility",
            "value": settlement_gate["label"],
            "kind": settlement_gate["status"],
        },
        {
            "label": "Final action",
            "value": final_action_label,
            "kind": _decision_kind(final_decision),
        },
    ]


def _tradable_conditions(
    *,
    values: dict[str, Any],
    blockers: list[str],
    market_structure: dict[str, Any],
    risk_gate: dict[str, Any],
    settlement_gate: dict[str, Any],
    settings: Settings,
) -> list[str]:
    conditions: list[str] = []
    if market_structure["is_unsupported"]:
        conditions.append(
            "valid category mapping and supported single-leg or verified component provenance"
        )
    if values["expected_value"] < settings.opportunity_min_edge:
        conditions.append(
            f"expected value at or above {_format_cents(settings.opportunity_min_edge)}"
        )
    if values["score"] < settings.opportunity_min_score or values["confidence"] < Decimal("30"):
        conditions.append(
            "confidence above configured opportunity-score and model-confidence thresholds"
        )
    if values["no_liquidity"] or (
        values["liquidity_raw"] is not None
        and values["liquidity_raw"] < settings.opportunity_min_liquidity
    ):
        conditions.append("liquidity above the configured threshold with nonzero executable depth")
    if values["spread"] is None or values["spread"] > settings.opportunity_max_spread:
        conditions.append(f"spread below {_format_cents(settings.opportunity_max_spread)}")
    if not values["fresh"]:
        conditions.append("fresh quote snapshot")
    if risk_gate["status"] != PASS:
        conditions.append("Phase 3N risk result of ALLOW")
    if settlement_gate["status"] == FAIL:
        conditions.append("valid close or settlement window for the exact ticker")
    if not conditions and not blockers:
        conditions.append(
            "keep the quote fresh and rerun sizing/risk immediately before paper action"
        )
    return _dedupe_text(conditions)


def _trace_details(
    *,
    ranking: Any,
    snapshot: Any | None,
    forecast: Any | None,
    feature_snapshot: Any | None,
    sizing_decision: Any | None,
    risk_decision: Any | None,
    values: dict[str, Any],
) -> list[dict[str, str]]:
    return [
        _trace("Market ticker", _field(ranking, "ticker")),
        _trace("Model version", _field(ranking, "forecast_model")),
        _trace("Forecast ID", _field(forecast, "id")),
        _trace("Feature snapshot ID", _field(feature_snapshot, "id")),
        _trace("Opportunity ID", _field(ranking, "id")),
        _trace("Phase 3S decision", _phase_3s_decision(ranking, values)),
        _trace("Phase 3M size proposal", _phase_3m_sizing(sizing_decision)),
        _trace("Phase 3N risk result", _phase_3n_risk(risk_decision)),
        _trace("Ranked at", _iso(_field(ranking, "ranked_at"))),
        _trace("Snapshot captured at", _iso(_field(snapshot, "captured_at"))),
        _trace("Data freshness", values["freshness_text"]),
    ]


def _risk_gate(risk_decision: Any | None) -> dict[str, str]:
    if risk_decision is None:
        return {"label": "Missing", "status": PENDING, "detail": "Phase 3N risk result is missing."}
    action = str(_field(risk_decision, "action") or "").upper()
    reasons = ", ".join(_json_list(_field(risk_decision, "reason_codes_json"))[:3])
    if action == "ALLOW":
        return {"label": "ALLOW", "status": PASS, "detail": reasons or "No hard risk block."}
    if action == "REDUCE":
        return {"label": "REDUCE", "status": WARN, "detail": reasons or "Risk reduced size."}
    if action == "BLOCK":
        return {"label": "BLOCK", "status": FAIL, "detail": "Phase 3N blocked this market."}
    return {
        "label": action or "Unknown",
        "status": PENDING,
        "detail": reasons or "Risk state unknown.",
    }


def _settlement_gate(
    market: Any | None,
    settlement: Any | None,
    market_structure: dict[str, Any],
) -> dict[str, str]:
    if market_structure["is_unsupported"]:
        return {
            "label": "Blocked",
            "status": FAIL,
            "detail": "Unsupported structures are not settlement-eligible for upgrade.",
        }
    if settlement is not None:
        return {"label": "Exact", "status": PASS, "detail": "Exact ticker settlement row exists."}
    if _field(market, "close_time") or _field(market, "expected_expiration_time"):
        return {
            "label": "Window known",
            "status": PASS,
            "detail": "Market has a close or expected expiration timestamp.",
        }
    return {"label": "Unknown", "status": WARN, "detail": "No close/settlement window is stored."}


def _rank_sort(final_decision: str, values: dict[str, Any], final_action_label: str) -> float:
    bucket = {
        TRADE: Decimal("4"),
        WATCH: Decimal("3"),
        WAITING_FOR_DATA: Decimal("2"),
        NO_TRADE: Decimal("1"),
    }.get(final_decision, Decimal("0"))
    if final_action_label == INTERESTING_NOT_EXECUTABLE:
        bucket = min(bucket, Decimal("1.5"))
    if values["no_liquidity"]:
        bucket = min(bucket, Decimal("1.25"))
    if not values["fresh"]:
        bucket = min(bucket, Decimal("0.75"))
    ev_cents = max(values["expected_value"], Decimal("0")) * Decimal("100")
    liquidity_bonus = min(values["liquidity_score"], Decimal("100")) / Decimal("1000")
    return float(bucket * Decimal("100000") + ev_cents + liquidity_bonus)


def _leg_view(leg: Any, *, component: dict[str, Any] | None = None) -> dict[str, str]:
    category = str(_field(leg, "category") or "").lower()
    operator = str(_field(leg, "operator") or "").strip()
    threshold = str(_field(leg, "threshold_value") or "").strip()
    unit = str(_field(leg, "unit") or "").strip()
    terms = " ".join(part for part in (operator, threshold, unit) if part)
    component_market_ticker = _clean_text(_field(component, "market_ticker"))
    component_event_ticker = _clean_text(_field(component, "event_ticker"))
    component_side = _clean_text(_field(component, "side"))
    label = _clean_text(_field(leg, "raw_text") or _field(leg, "entity_name")) or "Option"
    placeholder_status = _placeholder_status(label, component_market_ticker, component_event_ticker)
    provenance_status = "VERIFIED_COMPONENT" if component_market_ticker else "UNVERIFIED_COMPONENT"
    unsupported_reason = _leg_unsupported_reason(
        category=category,
        placeholder_status=placeholder_status,
        component_market_ticker=component_market_ticker,
    )
    return {
        "index": str(_field(leg, "leg_index") if _field(leg, "leg_index") is not None else ""),
        "side": str(_field(leg, "side") or ""),
        "label": label,
        "human_label": _human_option_label(label),
        "entity": _clean_text(_field(leg, "entity_name")) or "n/a",
        "market_type": str(_field(leg, "market_type") or "unknown"),
        "terms": terms or "n/a",
        "category": category,
        "confidence": str(_field(leg, "confidence") or "unknown"),
        "reason": _clean_text(_field(leg, "reason")) or "",
        "provenance_status": provenance_status,
        "placeholder_status": placeholder_status,
        "unsupported_reason": unsupported_reason,
        "block_reason": unsupported_reason or "Requires clean team, time, and market-type gate.",
        "component_market_ticker": component_market_ticker,
        "component_event_ticker": component_event_ticker,
        "component_side": component_side,
        "contract_line": _component_contract_line(
            side=component_side or _field(leg, "side"),
            label=label,
        ),
        "component_lookup_url": _kalshi_api_market_lookup_url(component_market_ticker),
        "component_search_url": "",
    }


def _contract_yes_summary(legs: list[dict[str, str]]) -> str:
    if not legs:
        return "No parsed YES-side contract text is available for this market."
    condition = _join_english([leg["contract_line"] for leg in legs])
    if len(legs) == 1:
        return f"YES wins if this selected component is true: {condition}."
    return f"YES wins only if all {len(legs)} selected components are true: {condition}."


def _contract_short_title(legs: list[dict[str, str]]) -> str:
    if not legs:
        return ""
    all_yes_components = all(
        _clean_text(leg.get("component_side") or leg.get("side")).upper() in {"", "YES"}
        for leg in legs
    )
    labels = [
        leg["human_label"] if all_yes_components else leg["contract_line"]
        for leg in legs
    ]
    return _join_english(labels)


def _contract_no_summary(legs: list[dict[str, str]]) -> str:
    if not legs:
        return "NO-side meaning cannot be summarized until the component legs are parsed."
    if len(legs) == 1:
        return "NO wins if the selected component is false."
    return "NO wins if at least one selected component is false."


def _component_contract_line(*, side: Any, label: Any) -> str:
    side_text = _clean_text(side).upper()
    option = _human_option_label(label)
    if side_text in {"YES", "NO"}:
        return f"{side_text} on {option}"
    return option


def _join_english(values: list[str]) -> str:
    cleaned = [value for value in (_clean_text(value) for value in values) if value]
    if not cleaned:
        return "n/a"
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return f"{', '.join(cleaned[:-1])}, and {cleaned[-1]}"


def _lookup_guidance(*, parser_status: str, legs: list[dict[str, str]]) -> str:
    component_count = sum(1 for leg in legs if leg.get("component_market_ticker"))
    if parser_status == "UNSUPPORTED_MULTI_LEG":
        if component_count:
            return (
                "This is a Kalshi multivariate market. If the combined ticker is hard to find "
                "on Kalshi, inspect the combo event API or the component market tickers below."
            )
        return (
            "This is a Kalshi multivariate market. The combined ticker may be easier to inspect "
            "from the combo event API or by copying the ticker into Kalshi search."
        )
    return "Use the exact ticker and event ticker to inspect the Kalshi market evidence."


def _clean_headline(
    *,
    clean_title: str,
    ticker: str,
    category: str,
    parser_status: str,
    leg_count: int,
    legs: list[dict[str, str]],
) -> str:
    if parser_status == "UNSUPPORTED_MULTI_LEG":
        return _multi_leg_headline(category=category, legs=legs, leg_count=leg_count)
    if parser_status == "CROSS_CATEGORY":
        return _multi_leg_headline(category="Cross-category", legs=legs, leg_count=leg_count)
    if parser_status == "UNKNOWN_CATEGORY":
        return summarize_market_title(clean_title or ticker)
    return summarize_market_title(clean_title)


def _multi_leg_headline(
    *,
    category: str,
    legs: list[dict[str, str]],
    leg_count: int,
) -> str:
    label = _multi_leg_category_label(category)
    leg_title = _compact_leg_title(legs, max_items=2)
    if leg_title:
        return f"{label} multi-leg: {leg_title}"
    if category == "CROSS_CATEGORY":
        return f"Unsupported cross-category market ({leg_count} component legs)"
    category_label = label if category != "UNKNOWN_CATEGORY" else "unknown-category"
    return f"Unsupported multi-leg {category_label} market ({leg_count} component legs)"


def _multi_leg_category_label(category: str) -> str:
    if category == "CROSS_CATEGORY":
        return "Cross-category"
    if category == "UNKNOWN_CATEGORY":
        return "Unknown-category"
    return category or "Unknown-category"


def _compact_leg_title(legs: list[dict[str, str]], *, max_items: int = 2) -> str:
    labels = [
        _clean_text(leg.get("human_label") or leg.get("label"))
        for leg in legs
        if _clean_text(leg.get("human_label") or leg.get("label"))
    ]
    if not labels:
        return ""
    visible = labels[:max_items]
    if len(labels) > max_items:
        visible.append(f"{len(labels) - max_items} more")
    return "; ".join(visible)


def _resolved_category(
    categories: list[str],
    heuristic_category: str,
    has_general_legs: bool,
) -> str:
    if categories:
        return _display_category(categories[0])
    if heuristic_category and heuristic_category != "General":
        return heuristic_category
    if has_general_legs:
        return "General"
    return _display_category(heuristic_category)


def _parser_label(parser_status: str, leg_count: int) -> str:
    if parser_status == "UNSUPPORTED_MULTI_LEG":
        return f"Unsupported multi-leg market ({leg_count} options)"
    if parser_status == "CROSS_CATEGORY":
        return f"Cross-category market ({leg_count} options)"
    if parser_status == "UNKNOWN_CATEGORY":
        return "Unknown category"
    return parser_status.replace("_", " ").title()


def _kalshi_lookup(
    *,
    ticker: str,
    event_ticker: str = "",
    search_query: str = "",
) -> dict[str, str]:
    exact_search_query = _clean_text(ticker)
    clean_event_ticker = _clean_text(event_ticker)
    return {
        "market_ticker": ticker,
        "event_ticker": clean_event_ticker,
        "api_url": _kalshi_api_market_lookup_url(ticker),
        "event_api_url": _kalshi_api_event_lookup_url(clean_event_ticker),
        "search_query": exact_search_query,
        "search_basis": "market_ticker",
        "manual_search_query": _clean_text(search_query),
        "search_url": "",
        "web_search_url": "",
        "exact_api_note": (
            "If the exact API returns 404, this local ticker is not currently exposed "
            "as a direct Kalshi trade page. Use the combo event API and component "
            "tickers to inspect what Kalshi currently exposes."
            if exact_search_query
            else ""
        ),
        "web_search_note": (
            "Direct Kalshi web-search links are disabled for this unsupported local "
            "combo ticker because Kalshi does not reliably resolve prefilled ticker "
            "queries to the exact trade."
            if exact_search_query
            else ""
        ),
    }


def _kalshi_api_market_lookup_url(ticker: str) -> str:
    text = _clean_text(ticker)
    if not text:
        return ""
    return f"https://external-api.kalshi.com/trade-api/v2/markets/{quote(text, safe='')}"


def _kalshi_api_event_lookup_url(event_ticker: str) -> str:
    text = _clean_text(event_ticker)
    if not text:
        return ""
    return f"https://external-api.kalshi.com/trade-api/v2/events/{quote(text, safe='')}"


def _kalshi_search_title(
    *,
    raw_title: str,
    ticker: str,
    legs: list[dict[str, str]],
) -> str:
    leg_title = _compact_leg_title(legs, max_items=3)
    if leg_title:
        return leg_title
    title = _clean_text(raw_title)
    if title and title != ticker:
        return title
    return ticker


def _kalshi_search_query(
    *,
    raw_title: str,
    ticker: str,
    event_ticker: str,
    series_ticker: str,
    legs: list[dict[str, str]],
) -> str:
    parts = [
        ticker,
        event_ticker,
        series_ticker,
        _kalshi_search_title(raw_title=raw_title, ticker=ticker, legs=legs),
        _clean_text(raw_title),
    ]
    return " ".join(dict.fromkeys(part for part in parts if part))


def _placeholder_status(*values: Any) -> str:
    joined = " ".join(_clean_text(value).lower() for value in values if value)
    placeholder_tokens = ("rd16", "rd32", "placeholder", "winner", "tbd", "to be decided")
    if any(token in joined for token in placeholder_tokens):
        return "PLACEHOLDER_BLOCKED"
    return "NONE"


def _leg_unsupported_reason(
    *,
    category: str,
    placeholder_status: str,
    component_market_ticker: str,
) -> str:
    if placeholder_status == "PLACEHOLDER_BLOCKED":
        return "Bracket placeholder must resolve to a real team before upgrade."
    if category in {"unknown", "", "general"}:
        return "Category mapping is not specific enough for an execution gate."
    if not component_market_ticker:
        return "Component provenance is not verified for this leg."
    return ""


def _selected_component_legs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    components = payload.get("mve_selected_legs")
    if isinstance(components, list):
        return [component for component in components if isinstance(component, dict)]

    custom_strike = payload.get("custom_strike")
    if not isinstance(custom_strike, dict):
        return []
    market_tickers = _split_component_csv(custom_strike.get("Associated Markets"))
    event_tickers = _split_component_csv(custom_strike.get("Associated Events"))
    sides = _split_component_csv(custom_strike.get("Associated Market Sides"))
    inferred_components: list[dict[str, Any]] = []
    for index, market_ticker in enumerate(market_tickers):
        if not market_ticker:
            continue
        inferred_components.append(
            {
                "event_ticker": event_tickers[index] if index < len(event_tickers) else "",
                "market_ticker": market_ticker,
                "side": sides[index] if index < len(sides) else "",
                "source": "kalshi_custom_strike",
            }
        )
    return inferred_components


def _split_component_csv(value: Any) -> list[str]:
    text = _clean_text(value)
    if not text:
        return []
    return [_clean_text(part) for part in text.split(",") if _clean_text(part)]


def _component_for_leg(
    components: list[dict[str, Any]],
    leg: Any,
    fallback_index: int,
) -> dict[str, Any] | None:
    if not components:
        return None
    leg_index = _field(leg, "leg_index")
    try:
        raw_index = int(leg_index)
    except (TypeError, ValueError):
        raw_index = fallback_index
    candidate_indexes: list[int] = []
    if raw_index == fallback_index:
        candidate_indexes.append(raw_index)
    candidate_indexes.extend([raw_index - 1, raw_index, fallback_index])
    for component_index in candidate_indexes:
        if 0 <= component_index < len(components):
            return components[component_index]
    return None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _human_option_label(value: Any) -> str:
    text = _clean_text(value)
    lowered = text.lower()
    for prefix in ("yes ", "no "):
        if lowered.startswith(prefix):
            return text[len(prefix) :].strip() or text
    return text


def _short_option_label(value: Any, *, limit: int = 58) -> str:
    text = _human_option_label(value)
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3].rstrip()}..."


def _model_direction(values: dict[str, Any]) -> str:
    if values["side"] == "BUY_YES":
        return "BUY YES"
    if values["side"] == "BUY_NO":
        return "BUY NO"
    return "NONE"


def _liquidity_label(raw: Decimal | None, score: Decimal) -> str:
    if raw is None and score <= Decimal("0"):
        return "None"
    if score >= Decimal("70"):
        return "High"
    if score >= Decimal("30"):
        return "Medium"
    if score > Decimal("0") or (raw is not None and raw > Decimal("0")):
        return "Low"
    return "None"


def _liquidity_detail(raw: Decimal | None, score: Decimal) -> str:
    raw_text = "raw n/a" if raw is None else f"raw {raw}"
    return f"{raw_text}; score {_format_score(score)}"


def _freshness(snapshot: Any | None, *, settings: Settings) -> dict[str, Any]:
    if snapshot is None:
        return {"fresh": False, "text": "No market snapshot is available."}
    captured_at = _field(snapshot, "captured_at")
    if not isinstance(captured_at, datetime):
        return {"fresh": False, "text": "Latest quote snapshot has no timestamp."}
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=UTC)
    age_minutes = Decimal(str(max((utc_now() - captured_at).total_seconds() / 60, 0)))
    fresh = age_minutes <= Decimal(str(settings.autopilot_require_fresh_data_minutes))
    return {
        "fresh": fresh,
        "text": (
            f"Latest data is about {age_minutes.quantize(Decimal('1'))} minutes old; "
            f"freshness limit is {settings.autopilot_require_fresh_data_minutes} minutes."
        ),
    }


def _phase_3s_decision(ranking: Any, values: dict[str, Any]) -> str:
    return (
        f"{_model_direction(values)}; edge {_format_cents(values['edge'])}; "
        f"score {_format_score(_field(ranking, 'opportunity_score'))}"
    )


def _phase_3m_sizing(sizing: Any | None) -> str:
    if sizing is None:
        return "Missing"
    return (
        f"{_field(sizing, 'proposed_contracts')} proposed; "
        f"tier {_field(sizing, 'tier')}; mode {_field(sizing, 'mode')}"
    )


def _phase_3n_risk(risk: Any | None) -> str:
    if risk is None:
        return "Missing"
    reasons = ", ".join(_json_list(_field(risk, "reason_codes_json"))[:3])
    suffix = f"; reasons {reasons}" if reasons else ""
    return f"{_field(risk, 'action')}; mode {_field(risk, 'mode')}{suffix}"


def _chip(label: str, passed: bool, value: str) -> dict[str, str]:
    return {"label": label, "value": value, "kind": PASS if passed else FAIL}


def _step(label: str, value: str, passed: bool, *, detail: str = "") -> dict[str, str]:
    return {"label": label, "value": value, "detail": detail, "kind": PASS if passed else FAIL}


def _trace(label: str, value: Any) -> dict[str, str]:
    return {"label": label, "value": str(value if value not in (None, "") else "n/a")}


def _format_score(value: Any) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return "n/a"
    return str(decimal_value.quantize(Decimal("0.1")))


def _format_percent(value: Any) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return "n/a"
    return f"{(decimal_value * Decimal('100')).quantize(Decimal('0.1'))}%"


def _format_cents(value: Any) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return "n/a"
    return f"{(decimal_value * Decimal('100')).quantize(Decimal('0.1'))} cents"


def _format_time(minutes: Any) -> str:
    value = to_decimal(minutes)
    if value is None:
        return "n/a"
    if value < Decimal("0"):
        return "closed"
    if value < Decimal("60"):
        return f"{value.quantize(Decimal('1'))} minutes"
    hours = value / Decimal("60")
    if hours < Decimal("48"):
        return f"{hours.quantize(Decimal('0.1'))} hours"
    return f"{(hours / Decimal('24')).quantize(Decimal('0.1'))} days"


def _format_time_with_freshness(values: dict[str, Any]) -> str:
    formatted = _format_time(values["time_to_close_minutes"])
    if formatted == "n/a" or values.get("fresh"):
        return formatted
    return f"stale local: {formatted}"


def _leg_confidence(legs: list[dict[str, str]]) -> str:
    values = sorted({leg["confidence"] for leg in legs if leg["confidence"]})
    return ", ".join(values[:3]) if values else "unknown"


def _display_category(value: str) -> str:
    if not value:
        return "UNKNOWN_CATEGORY"
    return value[:1].upper() + value[1:]


def _decision_kind(final_decision: str) -> str:
    if final_decision == TRADE:
        return PASS
    if final_decision in {WATCH, WAITING_FOR_DATA}:
        return WARN
    return FAIL


def _execution_quality_kind(value: str) -> str:
    if value == "Clean":
        return PASS
    if value in {"Thin", "Wide Spread", "Stale Quote"}:
        return WARN
    return FAIL


def _json_list(value: Any) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return [str(value)]
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return [str(parsed)]


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value if value else "n/a")


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\n", " ").split())


def _field(row: Any, name: str) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _dedupe_text(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = _clean_text(value)
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result
