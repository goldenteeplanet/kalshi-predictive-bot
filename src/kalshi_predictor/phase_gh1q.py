from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from kalshi_predictor.utils.time import utc_now


EXPECTED_INPUT_REASONS = {
    "no crypto features",
    "no crypto features for linked component",
    "no weather features",
    "crypto features have insufficient history",
    "weather features are stale",
}
SELECTION_DEFECT_REASONS = {
    "no crypto market link",
    "no weather market link",
    "crypto market terms ambiguous or unsupported",
}


def write_gh1q_report(*, database_path: Path, output_dir: Path, skip_limit: int = 30) -> Path:
    """Attribute the latest bounded GH-1P skips without mutating the database."""
    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id, model_name, ticker, skipped_at, reason, required_data, "
            "available_data FROM forecast_skip_log "
            "WHERE model_name IN ('crypto_v2', 'weather_v2') "
            "ORDER BY skipped_at DESC, id DESC LIMIT ?",
            (skip_limit,),
        ).fetchall()
        attributed = [_attribute(dict(row)) for row in rows]
    finally:
        connection.close()

    reason_counts = Counter(row["reason"] for row in attributed)
    class_counts = Counter(row["classification"] for row in attributed)
    likely_adapter_defect = class_counts["LIKELY_REFRESH_SELECTION_DEFECT"] > 0
    report = {
        "phase": "GH-1Q",
        "generated_at": utc_now().isoformat(),
        "mode": "READ_ONLY_SKIP_ATTRIBUTION_AND_REPAIR_PREVIEW",
        "database_writes": 0,
        "thresholds_changed": False,
        "execution_enabled": False,
        "skip_rows": attributed,
        "summary": {
            "skip_rows_attributed": len(attributed),
            "reason_counts": dict(sorted(reason_counts.items())),
            "classification_counts": dict(sorted(class_counts.items())),
            "likely_adapter_defect": likely_adapter_defect,
            "safe_to_repeat_refresh": not likely_adapter_defect and bool(attributed),
        },
        "exact_repair_preview": {
            "apply_now": False,
            "scope": "GH-1P market eligibility selection only",
            "changes": [
                "Select forecast candidates only when an exact model-domain link exists.",
                "Require model-compatible feature availability before opening the writer session.",
                "Report ineligible public markets as preview skips instead of writing snapshots and forecast skip rows.",
                "Preserve current edge, score, liquidity, spread, time, and risk thresholds.",
            ] if likely_adapter_defect else [],
            "next_command": (
                "Implement and test the exact GH-1P eligibility filter before another guarded refresh."
                if likely_adapter_defect else
                "Refresh the missing model inputs, then repeat the guarded GH-1P refresh and GH-1O."
            ),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "gh1q_independent_forecast_skip_attribution.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _attribute(row: dict[str, Any]) -> dict[str, Any]:
    reason = str(row.get("reason") or "UNKNOWN")
    if reason in SELECTION_DEFECT_REASONS:
        classification = "LIKELY_REFRESH_SELECTION_DEFECT"
        action = "Exclude until an exact link and supported terms exist."
    elif reason in EXPECTED_INPUT_REASONS or "feature" in reason:
        classification = "EXPECTED_MISSING_OR_INELIGIBLE_INPUT"
        action = "Refresh or certify the exact model feature input; do not alter thresholds."
    elif "midpoint" in reason:
        classification = "EXPECTED_NON_EXECUTABLE_BOOK"
        action = "Wait for an executable quoted book."
    else:
        classification = "REQUIRES_EXACT_REVIEW"
        action = "Inspect the recorded required and available data before repair."
    return {
        **row,
        "required_data": _decode(row.get("required_data")),
        "available_data": _decode(row.get("available_data")),
        "classification": classification,
        "recommended_action": action,
    }


def _decode(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value
