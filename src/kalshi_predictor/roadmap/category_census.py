from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.roadmap.category_adapters import adapt_category_evidence
from kalshi_predictor.roadmap.category_contract import CATEGORY_NAMES
from kalshi_predictor.utils.time import parse_datetime, utc_now

STAGES: tuple[tuple[str, str], ...] = (
    ("verified_link", "NO_VERIFIED_LINK"),
    ("fresh_snapshot", "NO_FRESH_SNAPSHOT"),
    ("fresh_features", "NO_FRESH_FEATURES"),
    ("forecast", "NO_FORECAST"),
    ("ranking", "NO_RANKING"),
    ("opportunity", "NO_OPPORTUNITY"),
    ("risk_evidence", "NO_RISK_EVIDENCE"),
    ("paper_trace", "NO_COMPLETE_PAPER_TRACE"),
)


def build_category_ingestion_census(
    payloads: Mapping[str, Mapping[str, Any]],
    *,
    generated_at: datetime | str | None = None,
    missing_ticker_limit: int = 50,
) -> dict[str, Any]:
    generated = parse_datetime(generated_at) or utc_now()
    bounded_limit = max(1, missing_ticker_limit)
    category_rows: list[dict[str, Any]] = []
    market_gaps: list[dict[str, Any]] = []
    overall_first_blockers: Counter[str] = Counter()

    unknown = sorted(set(payloads) - set(CATEGORY_NAMES))
    if unknown:
        raise ValueError(f"Unknown categories: {', '.join(unknown)}")

    for category in CATEGORY_NAMES:
        payload = payloads.get(category)
        if payload is None:
            category_rows.append(_missing_category(category))
            overall_first_blockers["CATEGORY_EVIDENCE_MISSING"] += 1
            continue
        evidence = adapt_category_evidence(category, payload, generated_at=generated)
        markets = _market_rows(payload.get("markets"))
        active = [row for row in markets if _truthy(row.get("active"), default=True)]
        gaps = [_market_gap(category, row) for row in active]
        blocked_gaps = [row for row in gaps if row["first_blocker"]]
        market_gaps.extend(blocked_gaps)
        overall_first_blockers.update(
            row["first_blocker"] for row in blocked_gaps if row["first_blocker"]
        )
        category_rows.append(
            _category_summary(
                category,
                evidence.as_payload(),
                active,
                gaps,
                missing_ticker_limit=bounded_limit,
            )
        )

    return {
        "schema_version": "category-ingestion-census-v1",
        "generated_at": generated.astimezone(UTC).isoformat(),
        "mode": "READ_ONLY_CATEGORY_CENSUS",
        "safety": {
            "creates_links": False,
            "creates_forecasts": False,
            "creates_paper_orders": False,
            "enables_authenticated_calls": False,
            "enables_live_trading": False,
            "lowers_thresholds": False,
        },
        "categories": category_rows,
        "market_gaps": sorted(
            market_gaps,
            key=lambda row: (
                CATEGORY_NAMES.index(row["category"]),
                row["first_blocker"] or "",
                row["ticker"],
            ),
        ),
        "priority_blockers": [
            {"blocker": blocker, "affected_markets": count}
            for blocker, count in sorted(
                overall_first_blockers.items(), key=lambda item: (-item[1], item[0])
            )
        ],
    }


def write_category_ingestion_census(path: Path, payload: Mapping[str, Any]) -> Path:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    envelope = {
        "schema_version": "roadmap-evidence-envelope-v1",
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "payload": dict(payload),
    }
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return path


def _category_summary(
    category: str,
    evidence: dict[str, Any],
    active: list[Mapping[str, Any]],
    gaps: list[dict[str, Any]],
    *,
    missing_ticker_limit: int,
) -> dict[str, Any]:
    denominator = len(active)
    detail_available = bool(active)
    stages: dict[str, Any] = {}
    for field, blocker in STAGES:
        numerator = sum(1 for row in active if _truthy(row.get(field)))
        missing = sorted(
            str(row.get("ticker") or "UNKNOWN")
            for row in active
            if not _truthy(row.get(field))
        )
        stages[field] = {
            "numerator": numerator,
            "denominator": denominator,
            "coverage_pct": round((100 * numerator / denominator), 2) if denominator else None,
            "threshold": "100% for a complete category decision trace",
            "blocker": blocker,
            "missing_tickers": missing[:missing_ticker_limit],
            "missing_tickers_truncated": len(missing) > missing_ticker_limit,
        }
    first_blockers = Counter(
        row["first_blocker"] for row in gaps if row.get("first_blocker")
    )
    blockers = list(evidence.get("deterministic_blockers") or ())
    if not detail_available:
        blockers.append("DETAIL_EVIDENCE_MISSING")
    return {
        "category": category,
        "source_name": evidence.get("source_name"),
        "source_state": evidence.get("source_state"),
        "source_available_at": evidence.get("source_available_at"),
        "evidence_granularity": "MARKET" if detail_available else "AGGREGATE_ONLY",
        "active_market_denominator": denominator,
        "aggregate_contract": evidence,
        "stage_coverage": stages,
        "first_blocker_counts": dict(sorted(first_blockers.items())),
        "deterministic_blockers": list(dict.fromkeys(blockers)),
        "paper_only": category not in {"crypto", "weather"},
    }


def _market_gap(category: str, row: Mapping[str, Any]) -> dict[str, Any]:
    ticker = str(row.get("ticker") or "").strip()
    if not ticker:
        raise ValueError(f"{category} market evidence is missing ticker")
    blockers = [blocker for field, blocker in STAGES if not _truthy(row.get(field))]
    supplied = row.get("blockers") or ()
    if isinstance(supplied, str):
        supplied = [supplied]
    if isinstance(supplied, Sequence):
        blockers.extend(_blocker(item) for item in supplied if str(item).strip())
    return {
        "category": category,
        "ticker": ticker,
        "first_blocker": blockers[0] if blockers else None,
        "blockers": list(dict.fromkeys(blockers)),
        "source_identity": row.get("source_identity"),
        "source_available_at": row.get("source_available_at"),
    }


def _missing_category(category: str) -> dict[str, Any]:
    return {
        "category": category,
        "source_name": None,
        "source_state": "NO_DATA",
        "source_available_at": None,
        "evidence_granularity": "NONE",
        "active_market_denominator": 0,
        "aggregate_contract": None,
        "stage_coverage": {},
        "first_blocker_counts": {"CATEGORY_EVIDENCE_MISSING": 1},
        "deterministic_blockers": ["CATEGORY_EVIDENCE_MISSING"],
        "paper_only": category not in {"crypto", "weather"},
    }


def _market_rows(value: Any) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise ValueError("markets must be a list of objects")
    return value


def _truthy(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "ready", "fresh", "verified"}
    return bool(value)


def _blocker(value: Any) -> str:
    return str(value).strip().upper().replace(" ", "_")
