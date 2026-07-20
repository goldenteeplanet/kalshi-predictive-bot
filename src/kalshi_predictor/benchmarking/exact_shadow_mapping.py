from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from kalshi_predictor.benchmarking.runtime_compatibility import (
    normalize_runtime_export_for_shadow,
)
from kalshi_predictor.benchmarking.shadow_adapter import ExposureGuardShadowAdapter


SUPPORTED_CATEGORIES = ("crypto", "weather", "sports")
REQUIRED_FORECAST_FIELDS = (
    "ticker", "category", "forecast_id", "model_version", "probability"
)
REQUIRED_BOOK_FIELDS = (
    "ticker", "category", "market_snapshot_id", "executable_spread"
)


def _missing(prefix: str, row: Mapping[str, Any], fields: tuple[str, ...]) -> list[str]:
    return [f"{prefix}_FIELD_MISSING:{field}" for field in fields if row.get(field) in (None, "")]


def _decimal(prefix: str, value: Any, diagnostics: list[str]) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        diagnostics.append(f"{prefix}_NOT_NUMERIC")
        return None


def map_exact_shadow_context(fixture: Mapping[str, Any]) -> dict[str, Any]:
    candidate = fixture.get("candidate_forecast") or {}
    reference = fixture.get("reference_forecast") or {}
    current_book = fixture.get("current_book") or {}
    reference_book = fixture.get("reference_book") or {}
    diagnostics: list[str] = []
    diagnostics.extend(_missing("CANDIDATE_FORECAST", candidate, REQUIRED_FORECAST_FIELDS))
    diagnostics.extend(_missing("REFERENCE_FORECAST", reference, REQUIRED_FORECAST_FIELDS))
    diagnostics.extend(_missing("CURRENT_BOOK", current_book, REQUIRED_BOOK_FIELDS))
    diagnostics.extend(_missing("REFERENCE_BOOK", reference_book, REQUIRED_BOOK_FIELDS))

    identities = (candidate, reference, current_book, reference_book)
    tickers = {row.get("ticker") for row in identities if row.get("ticker") not in (None, "")}
    categories = {
        row.get("category") for row in identities if row.get("category") not in (None, "")
    }
    if len(tickers) > 1:
        diagnostics.append("SOURCE_TICKER_MISMATCH")
    if len(categories) > 1:
        diagnostics.append("SOURCE_CATEGORY_MISMATCH")
    category = candidate.get("category")
    if category not in (None, "") and category not in SUPPORTED_CATEGORIES:
        diagnostics.append(f"UNSUPPORTED_CATEGORY:{category}")
    if (
        candidate.get("forecast_id") not in (None, "")
        and reference.get("forecast_id") not in (None, "")
        and candidate.get("forecast_id") == reference.get("forecast_id")
    ):
        diagnostics.append("FORECAST_REFERENCE_NOT_INDEPENDENT")
    if (
        current_book.get("market_snapshot_id") not in (None, "")
        and reference_book.get("market_snapshot_id") not in (None, "")
        and current_book.get("market_snapshot_id") == reference_book.get("market_snapshot_id")
    ):
        diagnostics.append("BOOK_REFERENCE_NOT_DISTINCT")

    candidate_probability = _decimal(
        "CANDIDATE_FORECAST_PROBABILITY", candidate.get("probability"), diagnostics
    ) if candidate.get("probability") not in (None, "") else None
    reference_probability = _decimal(
        "REFERENCE_FORECAST_PROBABILITY", reference.get("probability"), diagnostics
    ) if reference.get("probability") not in (None, "") else None
    current_spread = _decimal(
        "CURRENT_EXECUTABLE_SPREAD", current_book.get("executable_spread"), diagnostics
    ) if current_book.get("executable_spread") not in (None, "") else None
    reference_spread = _decimal(
        "REFERENCE_EXECUTABLE_SPREAD", reference_book.get("executable_spread"), diagnostics
    ) if reference_book.get("executable_spread") not in (None, "") else None

    for name, value in (
        ("CANDIDATE_FORECAST_PROBABILITY", candidate_probability),
        ("REFERENCE_FORECAST_PROBABILITY", reference_probability),
    ):
        if value is not None and not Decimal("0") <= value <= Decimal("1"):
            diagnostics.append(f"{name}_OUT_OF_RANGE")
    for name, value in (
        ("CURRENT_EXECUTABLE_SPREAD", current_spread),
        ("REFERENCE_EXECUTABLE_SPREAD", reference_spread),
    ):
        if value is not None and value < 0:
            diagnostics.append(f"{name}_NEGATIVE")

    context = None
    provenance = None
    if not diagnostics and None not in (
        candidate_probability, reference_probability, current_spread, reference_spread
    ):
        forecast_bias = candidate_probability - reference_probability
        spread_addition = current_spread - reference_spread
        if spread_addition < 0:
            diagnostics.append("SPREAD_ADDITION_NEGATIVE")
        else:
            context = {
                "forecast_bias": str(forecast_bias),
                "spread_addition": str(spread_addition),
            }
            provenance = {
                "forecast_bias": {
                    "formula": "candidate_probability-reference_probability",
                    "candidate_forecast_id": candidate["forecast_id"],
                    "candidate_model_version": candidate["model_version"],
                    "reference_forecast_id": reference["forecast_id"],
                    "reference_model_version": reference["model_version"],
                },
                "spread_addition": {
                    "formula": "current_executable_spread-reference_executable_spread",
                    "current_market_snapshot_id": current_book["market_snapshot_id"],
                    "reference_market_snapshot_id": reference_book["market_snapshot_id"],
                },
            }
    return {
        "mapped": not diagnostics and context is not None,
        "diagnostics": diagnostics,
        "shadow_context": context,
        "mapping_provenance": provenance,
    }


def build_exact_shadow_field_mapping_preview(fixtures_path: Path) -> dict[str, Any]:
    fixtures = json.loads(fixtures_path.read_text(encoding="utf-8"))
    adapter = ExposureGuardShadowAdapter()
    rows = []
    for fixture in fixtures:
        mapping = map_exact_shadow_context(fixture)
        compatibility = normalize_runtime_export_for_shadow(
            fixture.get("ranking", {}), fixture.get("risk", {}), mapping["shadow_context"]
        )
        compatible = mapping["mapped"] and compatibility["compatible"]
        diagnostics = mapping["diagnostics"] + compatibility["diagnostics"]
        rows.append({
            "fixture_id": fixture["fixture_id"],
            "category": fixture.get("candidate_forecast", {}).get("category"),
            "mapped": mapping["mapped"],
            "compatible": compatible,
            "diagnostics": diagnostics,
            "shadow_context": mapping["shadow_context"],
            "mapping_provenance": mapping["mapping_provenance"],
            "shadow_preview": adapter.preview(compatibility["normalized"]) if compatible else None,
        })
    diagnostic_counts: dict[str, int] = {}
    for row in rows:
        for diagnostic in row["diagnostics"]:
            diagnostic_counts[diagnostic] = diagnostic_counts.get(diagnostic, 0) + 1
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    required_categories_pass = all(
        any(row["category"] == category and row["compatible"] for row in rows)
        for category in SUPPORTED_CATEGORIES
    )
    return {
        "phase": "PMB-34A",
        "mode": "LOCAL_EXACT_SHADOW_FIELD_SOURCE_MAPPING_PREVIEW",
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "runtime_policy_changed": False,
        "default_or_fabricated_values_allowed": False,
        "source_mapping": {
            "forecast_bias": "candidate forecast probability minus independent reference forecast probability",
            "spread_addition": "current executable spread minus distinct reference executable spread",
        },
        "rows": rows,
        "diagnostic_counts": dict(sorted(diagnostic_counts.items())),
        "summary": {
            "fixtures": len(rows),
            "mapped": sum(row["mapped"] for row in rows),
            "compatible": sum(row["compatible"] for row in rows),
            "rejected": sum(not row["compatible"] for row in rows),
            "required_categories_pass": required_categories_pass,
            "pmb35_deployment_unblocked": required_categories_pass and all(row["compatible"] for row in rows),
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_exact_shadow_field_mapping_preview(fixtures_path: Path, output_dir: Path) -> Path:
    report = build_exact_shadow_field_mapping_preview(fixtures_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb34a_exact_shadow_field_source_mapping_preview.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path
