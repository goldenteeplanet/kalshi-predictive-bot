from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from kalshi_predictor.synthetic_markets.contracts import (
    DEFAULT_DENY_TERMS,
    EVENT_REJECTED_INSUFFICIENT_DATA,
    EVENT_REJECTED_POLICY,
    EVENT_REJECTED_UNRESOLVABLE,
    ObservationWindow,
    SettlementRule,
    SyntheticContractSpec,
    SyntheticEventSpec,
    SyntheticMarketsConfig,
    canonical_json,
    checksum_payload,
    semantic_hash,
    stable_phase_3r_id,
)
from kalshi_predictor.utils.time import parse_datetime


@dataclass(frozen=True)
class CandidateBuildResult:
    candidate_id: str
    accepted: bool
    event: SyntheticEventSpec | None
    contracts: tuple[SyntheticContractSpec, ...]
    rejection: dict[str, Any] | None


def build_candidate_from_payload(
    payload: Mapping[str, Any],
    *,
    config: SyntheticMarketsConfig,
    generated_at: datetime,
) -> CandidateBuildResult:
    """Validate and normalize one untrusted candidate payload into immutable specs."""

    merged = _event_payload(payload)
    candidate_id = _candidate_id(payload)
    reason_codes = _validate_candidate_payload(merged, payload, config=config, now=generated_at)
    if reason_codes:
        return CandidateBuildResult(
            candidate_id=candidate_id,
            accepted=False,
            event=None,
            contracts=(),
            rejection=_rejection_payload(
                candidate_id=candidate_id,
                reason_codes=reason_codes,
                rejected_at=generated_at,
                raw_payload=payload,
            ),
        )

    category = _text(merged.get("category") or payload.get("source_type") or "GENERAL").upper()
    observation_window = ObservationWindow(
        start_at=parse_datetime(_first_present(merged, "observation_start_at", "start_at"))
        or parse_datetime(_window(merged).get("start_at")),
        end_at=parse_datetime(_first_present(merged, "observation_end_at", "end_at"))
        or parse_datetime(_window(merged).get("end_at")),
        timezone=_text(_window(merged).get("timezone") or merged.get("timezone") or "UTC"),
    )
    settlement_rule = _settlement_rule(merged, payload)
    contract_payloads = _contract_payloads(merged, payload)
    identity_payload = {
        "canonical_title": _canonical_title(merged, payload),
        "category": category,
        "subcategory": _optional_text(merged.get("subcategory")),
        "market_form": _text(merged.get("market_form") or "BINARY").upper(),
        "observation_window": observation_window.as_payload(),
        "settlement_rule": settlement_rule.as_payload(),
        "contracts": [
            {
                "canonical_question": _text(contract.get("canonical_question")),
                "condition": contract.get("condition") or {},
                "outcome_code": _text(contract.get("outcome_code") or "YES"),
            }
            for contract in contract_payloads
        ],
    }
    event_semantic_hash = semantic_hash(identity_payload)
    event_id = stable_phase_3r_id("event", event_semantic_hash)
    event = SyntheticEventSpec(
        synthetic_event_id=event_id,
        synthetic_event_version=1,
        semantic_hash=event_semantic_hash,
        canonical_title=_canonical_title(merged, payload),
        plain_language_summary=_text(
            merged.get("plain_language_summary")
            or merged.get("summary")
            or _canonical_title(merged, payload)
        ),
        category=category,
        subcategory=_optional_text(merged.get("subcategory")),
        market_form=_text(merged.get("market_form") or "BINARY").upper(),
        observation_window=observation_window,
        mutually_exclusive=bool(merged.get("mutually_exclusive", True)),
        collectively_exhaustive=bool(merged.get("collectively_exhaustive", True)),
        settlement_rule=settlement_rule,
        generation_source=_text(
            merged.get("generation_source")
            or payload.get("generation_source")
            or "APPROVED_USER_RESEARCH_REQUEST"
        ),
    )
    contracts = tuple(
        SyntheticContractSpec(
            synthetic_contract_id=stable_phase_3r_id(
                "contract",
                event.synthetic_event_id,
                _text(contract.get("canonical_question")),
                _text(contract.get("outcome_code") or "YES"),
            ),
            synthetic_contract_version=1,
            synthetic_event_id=event.synthetic_event_id,
            canonical_question=_text(contract.get("canonical_question")),
            contract_type=_text(contract.get("contract_type") or "BINARY").upper(),
            outcome_code=_text(contract.get("outcome_code") or "YES").upper(),
            condition=dict(contract.get("condition") or {"type": "BINARY_EVENT"}),
            complement_contract_id=_optional_text(contract.get("complement_contract_id")),
            constraint_group_id=_optional_text(contract.get("constraint_group_id")),
            status=_text(contract.get("status") or "ACTIVE").upper(),
        )
        for contract in contract_payloads
    )
    return CandidateBuildResult(
        candidate_id=candidate_id,
        accepted=True,
        event=event,
        contracts=contracts,
        rejection=None,
    )


def _validate_candidate_payload(
    merged: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    config: SyntheticMarketsConfig,
    now: datetime,
) -> tuple[str, ...]:
    reasons: list[str] = []
    category = _text(merged.get("category") or payload.get("source_type") or "GENERAL").upper()
    if category not in config.approved_source_types:
        reasons.append("source_type_not_approved")
    if not _canonical_title(merged, payload):
        reasons.append("missing_title")
    if _contains_denied_term(merged, payload):
        reasons.append("default_deny_topic")
    window = _window(merged)
    start_at = parse_datetime(
        _first_present(merged, "observation_start_at", "start_at")
    ) or parse_datetime(window.get("start_at"))
    end_at = parse_datetime(
        _first_present(merged, "observation_end_at", "end_at")
    ) or parse_datetime(window.get("end_at"))
    if start_at is None or end_at is None:
        reasons.append("missing_observation_window")
    elif end_at <= start_at:
        reasons.append("invalid_observation_window")
    elif end_at - now > timedelta(days=config.max_horizon_days):
        reasons.append("horizon_too_long")
    settlement = _settlement_payload(merged, payload)
    if not _first_present(settlement, "primary_source_id", "source_id", "source"):
        reasons.append("missing_settlement_source")
    if not _first_present(settlement, "source_field", "field"):
        reasons.append("missing_settlement_field")
    if not _first_present(settlement, "rule_text", "resolution_rule", "settlement_rule_text"):
        reasons.append("missing_settlement_rule")
    contract_payloads = _contract_payloads(merged, payload)
    if not contract_payloads:
        reasons.append("missing_contract")
    if len(contract_payloads) > config.max_contracts_per_event:
        reasons.append("too_many_contracts")
    if any(not _text(contract.get("canonical_question")) for contract in contract_payloads):
        reasons.append("missing_contract_question")
    if any(_injection_term(_text(value)) for value in _text_values(payload)):
        reasons.append("untrusted_prompt_injection_text")
    return tuple(dict.fromkeys(reasons))


def _candidate_id(payload: Mapping[str, Any]) -> str:
    raw_id = payload.get("candidate_id") or payload.get("id")
    if raw_id:
        return str(raw_id)
    return stable_phase_3r_id("candidate", semantic_hash(payload))


def _event_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    event = payload.get("synthetic_event")
    if isinstance(event, Mapping):
        return {**payload, **event}
    return payload


def _contract_payloads(
    merged: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    raw_contracts = merged.get("contracts") or payload.get("contracts")
    if isinstance(raw_contracts, list) and raw_contracts:
        return [contract for contract in raw_contracts if isinstance(contract, Mapping)]
    question = (
        merged.get("canonical_question")
        or payload.get("canonical_question")
        or payload.get("question")
        or _canonical_title(merged, payload)
    )
    if not question:
        return []
    return [
        {
            "canonical_question": question,
            "contract_type": merged.get("contract_type") or "BINARY",
            "outcome_code": merged.get("outcome_code") or "YES",
            "condition": merged.get("condition") or {"type": "BINARY_EVENT"},
        }
    ]


def _settlement_rule(merged: Mapping[str, Any], payload: Mapping[str, Any]) -> SettlementRule:
    settlement = _settlement_payload(merged, payload)
    rule_payload = {
        "primary_source_id": _text(
            _first_present(settlement, "primary_source_id", "source_id", "source")
        ),
        "primary_source_locator": _optional_text(
            _first_present(settlement, "primary_source_locator", "source_locator", "url")
        ),
        "source_field": _text(_first_present(settlement, "source_field", "field")),
        "revision_policy": _text(settlement.get("revision_policy") or "FIRST_PUBLISHED_VALUE"),
        "rounding_policy": _text(settlement.get("rounding_policy") or "EXACT_PUBLISHED_VALUE"),
        "cancellation_policy": _text(
            settlement.get("cancellation_policy") or "MARK_CANCELLED_IF_SOURCE_CANCELS"
        ),
        "postponement_policy": _text(
            settlement.get("postponement_policy") or "USE_FIRST_OFFICIAL_PUBLICATION"
        ),
        "rule_text": _text(
            _first_present(settlement, "rule_text", "resolution_rule", "settlement_rule_text")
        ),
    }
    rule_hash = settlement.get("rule_hash") or checksum_payload(rule_payload)
    return SettlementRule(
        settlement_rule_id=_text(
            settlement.get("settlement_rule_id")
            or stable_phase_3r_id("settlement-rule", rule_hash)
        ),
        settlement_rule_version=int(settlement.get("settlement_rule_version") or 1),
        primary_source_id=rule_payload["primary_source_id"],
        primary_source_locator=rule_payload["primary_source_locator"],
        source_field=rule_payload["source_field"],
        revision_policy=rule_payload["revision_policy"],
        rounding_policy=rule_payload["rounding_policy"],
        cancellation_policy=rule_payload["cancellation_policy"],
        postponement_policy=rule_payload["postponement_policy"],
        rule_text=rule_payload["rule_text"],
        rule_hash=_text(rule_hash),
    )


def _settlement_payload(
    merged: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    settlement = merged.get("settlement_rule") or payload.get("settlement_rule")
    if isinstance(settlement, Mapping):
        return settlement
    return {
        "primary_source_id": _first_present(merged, "primary_source_id", "settlement_source"),
        "primary_source_locator": _first_present(
            merged,
            "primary_source_locator",
            "settlement_source_url",
        ),
        "source_field": _first_present(merged, "source_field", "settlement_field"),
        "rule_text": _first_present(merged, "rule_text", "resolution_rule"),
    }


def _rejection_payload(
    *,
    candidate_id: str,
    reason_codes: tuple[str, ...],
    rejected_at: datetime,
    raw_payload: Mapping[str, Any],
) -> dict[str, Any]:
    if "default_deny_topic" in reason_codes or "source_type_not_approved" in reason_codes:
        status = EVENT_REJECTED_POLICY
    elif "missing_settlement_rule" in reason_codes or "missing_observation_window" in reason_codes:
        status = EVENT_REJECTED_UNRESOLVABLE
    else:
        status = EVENT_REJECTED_INSUFFICIENT_DATA
    return {
        "candidate_id": candidate_id,
        "status": status,
        "reason_codes": list(reason_codes),
        "rejected_at": rejected_at.isoformat(),
        "raw_json": canonical_json(raw_payload),
    }


def _canonical_title(merged: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    return _text(
        merged.get("canonical_title")
        or payload.get("canonical_title")
        or merged.get("title")
        or payload.get("title")
        or merged.get("question")
        or payload.get("question")
    )


def _window(merged: Mapping[str, Any]) -> Mapping[str, Any]:
    window = merged.get("observation_window")
    return window if isinstance(window, Mapping) else {}


def _contains_denied_term(merged: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    text = " ".join(_text_values({"merged": merged, "payload": payload})).lower()
    return any(term in text for term in DEFAULT_DENY_TERMS)


def _injection_term(text: str) -> bool:
    lowered = text.lower()
    return any(
        term in lowered
        for term in (
            "ignore previous",
            "ignore all previous",
            "system prompt",
            "developer message",
            "reveal hidden",
        )
    )


def _text_values(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        values: list[str] = []
        for item in value.values():
            values.extend(_text_values(item))
        return values
    if isinstance(value, list | tuple):
        values = []
        for item in value:
            values.extend(_text_values(item))
        return values
    return [value] if isinstance(value, str) else []


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None
