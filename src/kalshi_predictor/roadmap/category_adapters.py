"""Adapters from domain-specific reports to the roadmap category contract.

The adapters are deliberately pure: they normalize already-collected evidence and
never fetch data, write records, or relax a downstream trading gate.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from kalshi_predictor.roadmap.category_contract import (
    CATEGORY_NAMES,
    LIVE_V1_CATEGORIES,
    CategoryPipelineEvidence,
    PipelineState,
)
from kalshi_predictor.utils.time import parse_datetime, utc_now

_COUNT_ALIASES: dict[str, tuple[str, ...]] = {
    "active_markets": ("active_markets", "active_market_count", "markets"),
    "verified_links": ("verified_links", "verified_link_count", "links_verified"),
    "fresh_snapshots": ("fresh_snapshots", "snapshot_count", "snapshots"),
    "fresh_features": ("fresh_features", "feature_count", "features"),
    "forecasts": ("forecasts", "forecast_count", "forecasts_inserted"),
    "rankings": ("rankings", "ranking_count", "rankings_inserted"),
    "opportunity_rows": ("opportunity_rows", "opportunities", "positive_ev_rows"),
    "risk_evidence_rows": ("risk_evidence_rows", "risk_rows", "risk_evidence"),
    "complete_paper_traces": (
        "complete_paper_traces",
        "paper_traces",
        "complete_decision_traces",
    ),
}

_FUNNEL_BLOCKERS = (
    ("active_markets", "NO_ACTIVE_MARKETS"),
    ("verified_links", "NO_VERIFIED_LINKS"),
    ("fresh_snapshots", "NO_FRESH_SNAPSHOTS"),
    ("fresh_features", "NO_FRESH_FEATURES"),
    ("forecasts", "NO_FORECASTS"),
    ("rankings", "NO_RANKINGS"),
    ("opportunity_rows", "NO_OPPORTUNITIES"),
    ("risk_evidence_rows", "NO_RISK_EVIDENCE"),
    ("complete_paper_traces", "NO_COMPLETE_PAPER_TRACE"),
)


def adapt_category_evidence(
    category: str,
    payload: Mapping[str, Any],
    *,
    generated_at: datetime | str | None = None,
) -> CategoryPipelineEvidence:
    """Normalize a category report into fail-closed pipeline evidence."""
    normalized_category = category.strip().lower()
    if normalized_category not in CATEGORY_NAMES:
        raise ValueError(f"Unknown category: {category}")

    source = _mapping(payload.get("source"))
    source_name = str(
        source.get("name")
        or source.get("source")
        or payload.get("source_name")
        or payload.get("provider")
        or "unknown"
    ).strip()
    source_kind = str(
        source.get("kind") or payload.get("source_kind") or source_name
    ).strip().lower()

    published_at = _time(payload, source, "published_at", "event_time", "observed_at")
    available_at = _time(payload, source, "available_at", "source_available_at")
    ingested_at = _time(payload, source, "ingested_at", "collected_at")
    timestamp_blockers = _timestamp_blockers(published_at, available_at, ingested_at)

    counts = {
        field: _nonnegative_int(_first(payload, aliases))
        for field, aliases in _COUNT_ALIASES.items()
    }
    supplied_blockers = payload.get("deterministic_blockers") or payload.get("blockers") or ()
    blockers = list(timestamp_blockers)
    blockers.extend(_normalize_blockers(supplied_blockers))

    state = _source_state(payload, source)
    if state != "READY":
        blockers.append(f"SOURCE_{state}")
    for field, blocker in _FUNNEL_BLOCKERS:
        if counts[field] <= 0:
            blockers.append(blocker)

    manual_or_synthetic = (
        normalized_category == "composite"
        or source_kind in {"manual", "file", "synthetic", "composite", "derived"}
        or bool(payload.get("synthetic") or payload.get("manual_only"))
    )
    if manual_or_synthetic:
        blockers.append("NON_EXTERNAL_EVIDENCE")
    if normalized_category == "composite":
        blockers.append("COMPOSITE_PAPER_ONLY")

    generated = parse_datetime(generated_at) or _time(payload, source, "generated_at") or utc_now()
    live_allowed = (
        normalized_category in LIVE_V1_CATEGORIES
        and not manual_or_synthetic
        and not timestamp_blockers
        and bool(payload.get("live_v1_allowed", True))
    )
    lineage = dict(_mapping(payload.get("source_lineage")))
    lineage.update(
        {
            "source_kind": source_kind,
            "published_at": _iso(published_at),
            "available_at": _iso(available_at),
            "ingested_at": _iso(ingested_at),
            "timestamp_order_valid": not timestamp_blockers,
        }
    )
    return CategoryPipelineEvidence(
        category=normalized_category,
        generated_at=generated.isoformat(),
        source_state=state,
        source_name=source_name,
        source_available_at=_iso(available_at),
        **counts,
        deterministic_blockers=tuple(dict.fromkeys(blockers)),
        live_v1_allowed=live_allowed,
        synthetic_or_manual_only=manual_or_synthetic,
        source_lineage=lineage,
    )


def adapt_all_category_evidence(
    payloads: Mapping[str, Mapping[str, Any]],
    *,
    generated_at: datetime | str | None = None,
) -> tuple[CategoryPipelineEvidence, ...]:
    """Normalize category payloads in the contract's stable category order."""
    return tuple(
        adapt_category_evidence(category, payloads[category], generated_at=generated_at)
        for category in CATEGORY_NAMES
        if category in payloads
    )


def _timestamp_blockers(
    published_at: datetime | None,
    available_at: datetime | None,
    ingested_at: datetime | None,
) -> list[str]:
    blockers: list[str] = []
    if available_at is None:
        blockers.append("SOURCE_AVAILABLE_AT_MISSING")
    if ingested_at is None:
        blockers.append("INGESTED_AT_MISSING")
    if published_at and available_at and published_at > available_at:
        blockers.append("PUBLISHED_AFTER_AVAILABLE")
    if available_at and ingested_at and available_at > ingested_at:
        blockers.append("AVAILABLE_AFTER_INGESTED")
    return blockers


def _source_state(payload: Mapping[str, Any], source: Mapping[str, Any]) -> PipelineState:
    raw = str(source.get("state") or payload.get("source_state") or "").strip().upper()
    if raw in {"READY", "BLOCKED", "NO_DATA", "STALE", "ERROR"}:
        return raw  # type: ignore[return-value]
    if payload.get("errors") or source.get("errors"):
        return "ERROR"
    return "READY" if bool(payload.get("source_ready", source.get("ready", True))) else "BLOCKED"


def _time(payload: Mapping[str, Any], source: Mapping[str, Any], *keys: str) -> datetime | None:
    for key in keys:
        parsed = parse_datetime(source.get(key) if key in source else payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _first(payload: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    counts = _mapping(payload.get("counts"))
    summary = _mapping(payload.get("summary"))
    for container in (payload, counts, summary):
        for key in keys:
            if key in container:
                return container[key]
    return 0


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _normalize_blockers(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip().upper().replace(" ", "_") for item in value if str(item).strip()]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
