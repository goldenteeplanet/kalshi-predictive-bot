from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def build_nyc_w11_preview(w10_report: dict[str, Any]) -> dict[str, Any]:
    w10_ready = w10_report.get("summary", {}).get("review_ready") is True
    w10_automatic = w10_report.get("summary", {}).get("automatic_action_taken") is True
    source_execution_disabled = w10_report.get("execution_enabled") is False
    source_flag_unchanged = w10_report.get("feature_flag_changed") is False
    gates = {
        "w10_manual_review_eligible": w10_ready,
        "w10_took_no_automatic_action": not w10_automatic,
        "source_execution_disabled": source_execution_disabled,
        "source_feature_flag_unchanged": source_flag_unchanged,
        "no_threshold_change_planned": True,
        "no_settlement_source_substitution": True,
        "rollback_requires_exact_prior_flag": True,
    }
    eligible = all(gates.values())
    preview: dict[str, Any] = {
        "phase": "NYC-W11",
        "status": "PASSED_LOCAL_PREVIEW" if eligible else "BLOCKED_BY_NYC_W10",
        "mode": "LOCAL_GUARDED_ACTIVATION_AND_ROLLBACK_PREVIEW",
        "cloud_access": False,
        "database_writes": 0,
        "service_changes": 0,
        "threshold_changes": 0,
        "feature_flag_changed": False,
        "execution_enabled": False,
        "activation_eligible": eligible,
        "automatic_activation_allowed": False,
        "gates": gates,
        "activation_plan": {
            "mode": "INERT_PREVIEW",
            "commands_executable": False,
            "flag_before": "WEATHER_V2_KNYC_OBSERVATION_ENABLED=false",
            "flag_after_preview": "WEATHER_V2_KNYC_OBSERVATION_ENABLED=false",
            "future_guarded_flag": "WEATHER_V2_KNYC_OBSERVATION_ENABLED=true",
            "required_preconditions": [
                "NYC-W10 review_ready=true",
                "verified external-volume database backup",
                "exact configuration rollback bundle and SHA-256",
                "authoritative writer and lock clearance",
                "EXECUTION_ENABLED=false",
                "weather_v2 shadow output parity before activation",
            ],
            "scope": [
                "KNYC observations only",
                "exact target-time match within 15 minutes",
                "NOAA retained as non-settlement evidence",
                "The Weather Company retained as settlement source",
            ],
        },
        "rollback_plan": {
            "mode": "INERT_PREVIEW",
            "commands_executable": False,
            "trigger_on": [
                "provenance mismatch",
                "alignment drift",
                "probability adjustment limit failure",
                "runtime timeout or OOM",
                "writer or lock contention",
                "execution enablement",
            ],
            "resulting_flag": "WEATHER_V2_KNYC_OBSERVATION_ENABLED=false",
            "automatic_service_restart": False,
            "automatic_trading_activation": False,
        },
        "decision": (
            "ELIGIBLE_FOR_SEPARATELY_APPROVED_GUARDED_ACTIVATION"
            if eligible
            else "HOLD_UNTIL_NYC_W10_PASSES"
        ),
        "deployment_requires_explicit_approval": True,
        "next_phase": "NYC-W11 Deployment — Guarded Weather Observation Activation",
    }
    preview["report_sha256"] = hashlib.sha256(
        (json.dumps(preview, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    return preview


def write_nyc_w11_preview(w10_path: Path, output_path: Path) -> Path:
    report = build_nyc_w11_preview(json.loads(w10_path.read_text(encoding="utf-8")))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path
