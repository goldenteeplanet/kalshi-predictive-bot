from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from kalshi_predictor.benchmarking.exact_shadow_mapping import (
    map_exact_shadow_context,
)
from kalshi_predictor.benchmarking.runtime_compatibility import (
    normalize_runtime_export_for_shadow,
)
from kalshi_predictor.benchmarking.shadow_adapter import ExposureGuardShadowAdapter


FORECAST_MAX_AGE_SECONDS = 3600
BOOK_MAX_AGE_SECONDS = 300


def _parse_utc(value: Any, label: str, diagnostics: list[str]) -> datetime | None:
    if value in (None, ""):
        diagnostics.append(f"TIMESTAMP_MISSING:{label}")
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        diagnostics.append(f"TIMESTAMP_INVALID:{label}")
        return None
    if parsed.tzinfo is None:
        diagnostics.append(f"TIMESTAMP_NAIVE:{label}")
        return None
    return parsed.astimezone(timezone.utc)


def _exact_one(
    rows: Sequence[Mapping[str, Any]], field: str, value: Any, label: str,
    diagnostics: list[str],
) -> Mapping[str, Any] | None:
    matches = [row for row in rows if row.get(field) == value]
    if not matches:
        diagnostics.append(f"JOIN_MISSING:{label}:{value}")
        return None
    if len(matches) > 1:
        diagnostics.append(f"JOIN_AMBIGUOUS:{label}:{value}")
        return None
    return matches[0]


def join_exact_runtime_exports(bundle: Mapping[str, Any]) -> dict[str, Any]:
    decision = bundle.get("decision") or {}
    diagnostics: list[str] = []
    required = (
        "ticker", "category", "target_time", "decision_time",
        "candidate_forecast_id", "reference_forecast_id",
        "current_market_snapshot_id", "reference_market_snapshot_id",
    )
    diagnostics.extend(
        f"DECISION_FIELD_MISSING:{field}"
        for field in required if decision.get(field) in (None, "")
    )
    forecasts = bundle.get("forecasts") or []
    books = bundle.get("books") or []
    candidate = _exact_one(
        forecasts, "forecast_id", decision.get("candidate_forecast_id"),
        "candidate_forecast", diagnostics,
    )
    reference = _exact_one(
        forecasts, "forecast_id", decision.get("reference_forecast_id"),
        "reference_forecast", diagnostics,
    )
    current_book = _exact_one(
        books, "market_snapshot_id", decision.get("current_market_snapshot_id"),
        "current_book", diagnostics,
    )
    reference_book = _exact_one(
        books, "market_snapshot_id", decision.get("reference_market_snapshot_id"),
        "reference_book", diagnostics,
    )

    joined = {
        "candidate_forecast": candidate,
        "reference_forecast": reference,
        "current_book": current_book,
        "reference_book": reference_book,
    }
    ticker = decision.get("ticker")
    category = decision.get("category")
    target_time = decision.get("target_time")
    for label, row in joined.items():
        if row is None:
            continue
        if row.get("ticker") != ticker:
            diagnostics.append(f"IDENTITY_TICKER_MISMATCH:{label}")
        if row.get("category") != category:
            diagnostics.append(f"IDENTITY_CATEGORY_MISMATCH:{label}")
        if row.get("target_time") != target_time:
            diagnostics.append(f"IDENTITY_TARGET_TIME_MISMATCH:{label}")

    decision_time = _parse_utc(decision.get("decision_time"), "decision", diagnostics)
    times = {
        "candidate_forecast": _parse_utc(candidate.get("generated_at"), "candidate_forecast", diagnostics)
        if candidate else None,
        "reference_forecast": _parse_utc(reference.get("generated_at"), "reference_forecast", diagnostics)
        if reference else None,
        "current_book": _parse_utc(current_book.get("captured_at"), "current_book", diagnostics)
        if current_book else None,
        "reference_book": _parse_utc(reference_book.get("captured_at"), "reference_book", diagnostics)
        if reference_book else None,
    }
    ages: dict[str, int] = {}
    if decision_time is not None:
        for label, timestamp in times.items():
            if timestamp is None:
                continue
            age = int((decision_time - timestamp).total_seconds())
            ages[label] = age
            if age < 0:
                diagnostics.append(f"SOURCE_FROM_FUTURE:{label}")
            max_age = FORECAST_MAX_AGE_SECONDS if "forecast" in label else BOOK_MAX_AGE_SECONDS
            if age > max_age:
                diagnostics.append(f"SOURCE_STALE:{label}:{age}>{max_age}")
    if current_book and reference_book and times["current_book"] and times["reference_book"]:
        if times["reference_book"] > times["current_book"]:
            diagnostics.append("REFERENCE_BOOK_NEWER_THAN_CURRENT")

    assembled = None
    mapping = None
    compatibility = None
    if not diagnostics and all(joined.values()):
        assembled = {
            "ranking": bundle.get("ranking") or {},
            "risk": bundle.get("risk") or {},
            **joined,
        }
        mapping = map_exact_shadow_context(assembled)
        compatibility = normalize_runtime_export_for_shadow(
            assembled["ranking"], assembled["risk"], mapping["shadow_context"]
        )
        diagnostics.extend(mapping["diagnostics"])
        diagnostics.extend(compatibility["diagnostics"])
    joined_ok = not diagnostics and mapping is not None and mapping["mapped"] and compatibility["compatible"]
    return {
        "joined": joined_ok,
        "diagnostics": diagnostics,
        "source_ages_seconds": ages,
        "assembled_fixture": assembled if joined_ok else None,
        "mapping": mapping if joined_ok else None,
        "normalized": compatibility["normalized"] if joined_ok else None,
    }


def build_offline_exact_export_join_preview(fixtures_path: Path) -> dict[str, Any]:
    fixtures = json.loads(fixtures_path.read_text(encoding="utf-8"))
    adapter = ExposureGuardShadowAdapter()
    rows = []
    for fixture in fixtures:
        result = join_exact_runtime_exports(fixture)
        rows.append({
            "fixture_id": fixture["fixture_id"],
            "category": (fixture.get("decision") or {}).get("category"),
            "joined": result["joined"],
            "diagnostics": result["diagnostics"],
            "source_ages_seconds": result["source_ages_seconds"],
            "mapping_provenance": (
                result["mapping"]["mapping_provenance"] if result["mapping"] else None
            ),
            "shadow_preview": adapter.preview(result["normalized"]) if result["joined"] else None,
        })
    diagnostic_counts: dict[str, int] = {}
    for row in rows:
        for diagnostic in row["diagnostics"]:
            diagnostic_counts[diagnostic] = diagnostic_counts.get(diagnostic, 0) + 1
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    categories_pass = all(
        any(row["category"] == category and row["joined"] for row in rows)
        for category in ("crypto", "weather", "sports")
    )
    return {
        "phase": "PMB-34B",
        "mode": "LOCAL_OFFLINE_EXACT_EXPORT_JOIN_PREVIEW",
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "runtime_policy_changed": False,
        "join_contract": {
            "identity": ["ticker", "category", "target_time"],
            "forecast_max_age_seconds": FORECAST_MAX_AGE_SECONDS,
            "book_max_age_seconds": BOOK_MAX_AGE_SECONDS,
            "ambiguity_allowed": False,
            "defaults_allowed": False,
        },
        "rows": rows,
        "diagnostic_counts": dict(sorted(diagnostic_counts.items())),
        "summary": {
            "fixtures": len(rows),
            "joined": sum(row["joined"] for row in rows),
            "rejected": sum(not row["joined"] for row in rows),
            "required_categories_pass": categories_pass,
            "real_runtime_exports_certified": False,
            "pmb35_deployment_unblocked": False,
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_offline_exact_export_join_preview(fixtures_path: Path, output_dir: Path) -> Path:
    report = build_offline_exact_export_join_preview(fixtures_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb34b_offline_exact_export_join_preview.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path
