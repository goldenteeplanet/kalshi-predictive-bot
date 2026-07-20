from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from kalshi_predictor.benchmarking.shadow_adapter import ExposureGuardShadowAdapter


REQUIRED_RANKING_FIELDS = (
    "ticker", "category", "opportunity_score", "forecast_model",
    "forecast_id", "feature_ref", "observation_ref", "market_snapshot_id",
    "model_version",
)
REQUIRED_RISK_FIELDS = ("ticker", "risk_gate_passed", "requested_capital")
REQUIRED_SHADOW_CONTEXT_FIELDS = ("forecast_bias", "spread_addition")


def normalize_runtime_export_for_shadow(
    ranking: Mapping[str, Any],
    risk: Mapping[str, Any],
    shadow_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    diagnostics = []
    diagnostics.extend(
        f"RANKING_FIELD_MISSING:{field}"
        for field in REQUIRED_RANKING_FIELDS if ranking.get(field) in (None, "")
    )
    diagnostics.extend(
        f"RISK_FIELD_MISSING:{field}"
        for field in REQUIRED_RISK_FIELDS if risk.get(field) in (None, "")
    )
    context = dict(shadow_context or {})
    diagnostics.extend(
        f"SHADOW_CONTEXT_MISSING:{field}"
        for field in REQUIRED_SHADOW_CONTEXT_FIELDS if context.get(field) in (None, "")
    )
    if ranking.get("ticker") and risk.get("ticker") and ranking["ticker"] != risk["ticker"]:
        diagnostics.append("TICKER_MISMATCH")
    for field, value in (
        ("opportunity_score", ranking.get("opportunity_score")),
        ("requested_capital", risk.get("requested_capital")),
        ("forecast_bias", context.get("forecast_bias")),
        ("spread_addition", context.get("spread_addition")),
    ):
        if value not in (None, ""):
            try:
                float(value)
            except (TypeError, ValueError):
                diagnostics.append(f"FIELD_NOT_NUMERIC:{field}")
    normalized = None
    if not diagnostics:
        normalized = {
            "ticker": ranking["ticker"],
            "category": ranking["category"],
            "opportunity_score": str(ranking["opportunity_score"]),
            "forecast_bias": str(context["forecast_bias"]),
            "spread_addition": str(context["spread_addition"]),
            "requested_capital": str(risk["requested_capital"]),
            "risk_gate_passed": bool(risk["risk_gate_passed"]),
            "attribution": {
                "forecast_id": ranking["forecast_id"],
                "feature_ref": ranking["feature_ref"],
                "observation_ref": ranking["observation_ref"],
                "market_snapshot_id": ranking["market_snapshot_id"],
                "model_version": ranking["model_version"],
            },
        }
    return {
        "ticker": ranking.get("ticker") or risk.get("ticker"),
        "compatible": not diagnostics,
        "diagnostics": diagnostics,
        "normalized": normalized,
    }


def build_runtime_field_compatibility_preview(fixtures_path: Path) -> dict[str, Any]:
    fixtures = json.loads(fixtures_path.read_text(encoding="utf-8"))
    adapter = ExposureGuardShadowAdapter()
    rows = []
    for fixture in fixtures:
        result = normalize_runtime_export_for_shadow(
            fixture.get("ranking", {}), fixture.get("risk", {}),
            fixture.get("shadow_context"),
        )
        result["fixture_id"] = fixture["fixture_id"]
        result["shadow_preview"] = (
            adapter.preview(result["normalized"]) if result["compatible"] else None
        )
        rows.append(result)
    diagnostic_counts: dict[str, int] = {}
    for row in rows:
        for diagnostic in row["diagnostics"]:
            diagnostic_counts[diagnostic] = diagnostic_counts.get(diagnostic, 0) + 1
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    missing_context = sorted({
        diagnostic.split(":", 1)[1]
        for row in rows for diagnostic in row["diagnostics"]
        if diagnostic.startswith("SHADOW_CONTEXT_MISSING:")
    })
    return {
        "phase": "PMB-34",
        "mode": "LOCAL_OFFLINE_RUNTIME_FIELD_COMPATIBILITY_PREVIEW",
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "runtime_policy_changed": False,
        "required_schema": {
            "ranking": list(REQUIRED_RANKING_FIELDS),
            "risk": list(REQUIRED_RISK_FIELDS),
            "shadow_context": list(REQUIRED_SHADOW_CONTEXT_FIELDS),
        },
        "rows": rows,
        "diagnostic_counts": dict(sorted(diagnostic_counts.items())),
        "exact_runtime_gaps": missing_context,
        "summary": {
            "fixtures": len(rows),
            "compatible": sum(row["compatible"] for row in rows),
            "incompatible": sum(not row["compatible"] for row in rows),
            "shadow_previews_generated": sum(row["shadow_preview"] is not None for row in rows),
            "runtime_activation_ready": all(row["compatible"] for row in rows),
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_runtime_field_compatibility_preview(
    fixtures_path: Path, output_dir: Path
) -> Path:
    report = build_runtime_field_compatibility_preview(fixtures_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb34_runtime_field_compatibility_preview.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path
