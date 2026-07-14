from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from kalshi_predictor.feature_discovery.contracts import (
    ALLOWED_FEATURE_SOURCES,
    FORBIDDEN_SOURCE_TOKENS,
    CandidateDefinition,
    FeatureDiscoveryConfig,
    checksum_payload,
    stable_phase_3q_id,
)


class CandidateValidationError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def canonical_expression(expression: Mapping[str, Any]) -> dict[str, Any]:
    operator = str(expression.get("operator") or "raw").strip().lower()
    sources = tuple(sorted(str(source).strip() for source in expression.get("sources") or ()))
    canonical = {
        "operator": operator,
        "sources": list(sources),
    }
    if "window_seconds" in expression:
        canonical["window_seconds"] = int(expression["window_seconds"])
    if "window_position" in expression:
        canonical["window_position"] = str(expression["window_position"]).lower()
    if "zero_policy" in expression:
        canonical["zero_policy"] = str(expression["zero_policy"]).lower()
    if "interaction_depth" in expression:
        canonical["interaction_depth"] = int(expression["interaction_depth"])
    return canonical


def candidate_from_expression(
    expression: Mapping[str, Any],
    *,
    config: FeatureDiscoveryConfig,
    lineage: Mapping[str, Any] | None = None,
) -> CandidateDefinition:
    canonical = canonical_expression(expression)
    audit_expression(canonical, config=config, lineage=lineage or {})
    sources = tuple(canonical["sources"])
    family = _family_for(canonical)
    feature_name = _feature_name(canonical)
    definition_hash = checksum_payload(canonical)
    feature_definition_id = stable_phase_3q_id("feature_definition", definition_hash)
    candidate_id = stable_phase_3q_id("candidate", config.candidate_policy_id, definition_hash)
    return CandidateDefinition(
        candidate_id=candidate_id,
        feature_definition_id=feature_definition_id,
        feature_name=feature_name,
        feature_family=family,
        expression=canonical,
        source_fields=sources,
        origin="RAW" if canonical["operator"] == "raw" else "TRANSFORM",
        lineage=dict(lineage or {}),
    )


def generate_candidate_definitions(
    source_fields: Iterable[str],
    *,
    config: FeatureDiscoveryConfig,
) -> list[CandidateDefinition]:
    config.validate()
    candidates: dict[str, CandidateDefinition] = {}
    for source in sorted(set(source_fields)):
        if source not in ALLOWED_FEATURE_SOURCES:
            continue
        candidate = candidate_from_expression(
            {"operator": "raw", "sources": [source]},
            config=config,
            lineage={"source": source, "availability": "decision_time"},
        )
        candidates[candidate.candidate_id] = candidate
        if source.endswith("_score") or source in {"predicted_probability", "opportunity_score"}:
            transformed = candidate_from_expression(
                {"operator": "rank", "sources": [source]},
                config=config,
                lineage={"source": source, "availability": "fold_local_transform"},
            )
            candidates[transformed.candidate_id] = transformed
        if len(candidates) >= config.max_candidates:
            break
    return list(candidates.values())[: config.max_candidates]


def audit_expression(
    expression: Mapping[str, Any],
    *,
    config: FeatureDiscoveryConfig,
    lineage: Mapping[str, Any] | None = None,
) -> None:
    operator = str(expression.get("operator") or "").lower()
    if operator not in {"raw", "rank", "trailing_mean", "safe_divide", "interaction"}:
        raise CandidateValidationError("unsupported_operator", f"Unsupported operator: {operator}")
    sources = [str(source).lower() for source in expression.get("sources") or ()]
    if not sources:
        raise CandidateValidationError("missing_source", "Candidate requires at least one source.")
    for source in sources:
        _reject_forbidden(source)
        if source not in ALLOWED_FEATURE_SOURCES:
            raise CandidateValidationError("unknown_feature_source", f"Unknown source: {source}")
    lineage_text = _flatten_text(lineage or {})
    _reject_forbidden(lineage_text)
    if operator == "trailing_mean":
        window = int(expression.get("window_seconds") or 0)
        if window <= 0:
            raise CandidateValidationError("invalid_window", "Trailing window must be positive.")
        if str(expression.get("window_position", "trailing")).lower() == "centered":
            raise CandidateValidationError(
                "centered_window_rejected",
                "Centered windows leak future data.",
            )
    if operator == "safe_divide":
        if len(sources) != 2:
            raise CandidateValidationError("invalid_ratio", "safe_divide requires two sources.")
        if not expression.get("zero_policy"):
            raise CandidateValidationError(
                "missing_zero_policy",
                "Division requires a zero policy.",
            )
    if operator == "interaction":
        depth = int(expression.get("interaction_depth") or len(sources))
        if depth > config.max_interaction_depth:
            raise CandidateValidationError(
                "interaction_depth_limit",
                "Interaction depth limit exceeded.",
            )


def _reject_forbidden(text: str) -> None:
    normalized = text.lower()
    for token in FORBIDDEN_SOURCE_TOKENS:
        if token in normalized:
            raise CandidateValidationError(
                "forbidden_leakage_source",
                f"Forbidden leakage source token: {token}",
            )


def _flatten_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return " ".join(f"{key} {_flatten_text(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten_text(item) for item in value)
    return str(value)


def _family_for(expression: Mapping[str, Any]) -> str:
    sources = "+".join(expression.get("sources") or [])
    operator = expression.get("operator")
    return f"{operator}:{sources}"


def _feature_name(expression: Mapping[str, Any]) -> str:
    sources = "_".join(expression.get("sources") or [])
    operator = expression.get("operator")
    return f"{operator}_{sources}"
