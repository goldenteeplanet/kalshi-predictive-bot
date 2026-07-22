from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

CATEGORY_NAMES = (
    "crypto",
    "weather",
    "sports",
    "economic",
    "news",
    "general",
    "composite",
)
LIVE_V1_CATEGORIES = frozenset({"crypto", "weather"})
PipelineState = Literal["READY", "BLOCKED", "NO_DATA", "STALE", "ERROR"]


@dataclass(frozen=True)
class CategoryPipelineEvidence:
    category: str
    generated_at: str
    source_state: PipelineState
    source_name: str
    source_available_at: str | None = None
    active_markets: int = 0
    verified_links: int = 0
    fresh_snapshots: int = 0
    fresh_features: int = 0
    forecasts: int = 0
    rankings: int = 0
    opportunity_rows: int = 0
    risk_evidence_rows: int = 0
    complete_paper_traces: int = 0
    deterministic_blockers: tuple[str, ...] = ()
    live_v1_allowed: bool = False
    synthetic_or_manual_only: bool = False
    source_lineage: dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


def certify_category_pipeline(
    evidence: CategoryPipelineEvidence,
    *,
    max_age_minutes: int = 30,
) -> dict[str, Any]:
    generated_at = _parse_time(evidence.generated_at)
    age_minutes = (
        max(0.0, (datetime.now(UTC) - generated_at).total_seconds() / 60)
        if generated_at
        else None
    )
    checks = {
        "known_category": evidence.category in CATEGORY_NAMES,
        "source_ready": evidence.source_state == "READY",
        "source_current": age_minutes is not None and age_minutes <= max_age_minutes,
        "availability_time_preserved": bool(evidence.source_available_at),
        "active_markets": evidence.active_markets > 0,
        "verified_links": evidence.verified_links > 0,
        "fresh_snapshots": evidence.fresh_snapshots > 0,
        "fresh_features": evidence.fresh_features > 0,
        "forecasts": evidence.forecasts > 0,
        "rankings": evidence.rankings > 0,
        "opportunities": evidence.opportunity_rows > 0,
        "risk_evidence": evidence.risk_evidence_rows > 0,
        "complete_paper_trace": evidence.complete_paper_traces > 0,
        "not_manual_or_synthetic_only": not evidence.synthetic_or_manual_only,
    }
    passed = all(checks.values())
    live_scope_valid = (
        passed
        and evidence.category in LIVE_V1_CATEGORIES
        and evidence.live_v1_allowed
    )
    return {
        "schema_version": "category-pipeline-certification-v1",
        "category": evidence.category,
        "generated_at": evidence.generated_at,
        "age_minutes": round(age_minutes, 3) if age_minutes is not None else None,
        "checks": checks,
        "paper_pipeline_certified": passed,
        "live_v1_scope_certified": live_scope_valid,
        "blockers": [name for name, ok in checks.items() if not ok]
        + list(evidence.deterministic_blockers),
        "evidence": evidence.as_payload(),
    }


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
