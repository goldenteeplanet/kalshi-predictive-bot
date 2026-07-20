from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


STAGES = {
    "UI-OBS-2D": Path("ui_obs2d/ui_obs2d_incident_resolution_preview.json"),
    "UI-OBS-2E": Path("ui_obs2e/ui_obs2e_notification_routing_preview.json"),
    "UI-OBS-2F": Path("ui_obs2f/ui_obs2f_local_delivery_simulator.json"),
    "UI-OBS-2G": Path("ui_obs2g/ui_obs2g_notification_receipt_audit.json"),
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_notification_pipeline_bundle(reports_root: Path) -> dict[str, Any]:
    diagnostics = []
    payloads = {}
    artifacts = []
    for phase, relative in STAGES.items():
        path = reports_root / relative
        if not path.is_file():
            diagnostics.append(f"ARTIFACT_MISSING:{phase}:{relative.as_posix()}")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            diagnostics.append(f"ARTIFACT_INVALID_JSON:{phase}")
            continue
        if payload.get("phase") != phase:
            diagnostics.append(f"PHASE_MISMATCH:{phase}:{payload.get('phase')}")
        payloads[phase] = payload
        artifacts.append({"phase": phase, "path": relative.as_posix(), "sha256": _sha(path)})
    if len(payloads) == len(STAGES):
        d, e, f, g = (payloads[name] for name in STAGES)
        d_preview = d.get("preview") or {}
        d_total = int((d_preview.get("summary") or {}).get("total") or 0)
        e_decisions = list(e.get("decisions") or [])
        f_receipts = list(f.get("new_receipts") or [])
        g_summary = g.get("summary") or {}
        expected_attempts = sum(
            len(item.get("channels") or [])
            for item in e_decisions if item.get("action") in {"DELIVER_NOW", "TIMELINE_ONLY"}
        )
        critical_channels = sum(
            len(item.get("channels") or [])
            for item in e_decisions
            if item.get("severity") == "CRITICAL" and item.get("action") == "DELIVER_NOW"
        )
        checks = {
            "incident_to_routing_count": d_total == len(e_decisions),
            "routing_to_delivery_attempts": expected_attempts == int((f.get("summary") or {}).get("attempts") or -1),
            "delivery_receipts_to_audit": len(f_receipts) == int(g_summary.get("actual_receipts") or -1),
            "critical_channel_count": critical_channels == int(g_summary.get("critical_channels_expected") or -1),
            "critical_coverage_complete": g_summary.get("critical_coverage_complete") is True,
            "reconciliation_passed": g_summary.get("reconciliation_passed") is True,
            "no_actual_notifications": f.get("actual_notifications_sent") == 0 and f.get("actual_audio_played") is False,
            "no_external_services": e.get("external_services_contacted") is False and f.get("external_services_contacted") is False and g.get("external_services_contacted") is False,
            "no_network": e.get("network_access") is False and f.get("network_access") is False and g.get("network_access") is False,
            "no_database_writes": all((payload.get("database_writes") == 0) for payload in payloads.values()),
            "execution_unchanged": all((payload.get("execution_changed") is False) for payload in payloads.values()),
        }
        diagnostics.extend(f"CROSS_STAGE_CHECK_FAILED:{name}" for name, passed in checks.items() if not passed)
    else:
        checks = {}
    artifacts.sort(key=lambda item: item["phase"])
    canonical = json.dumps({"artifacts": artifacts, "checks": checks}, sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema_version": 1,
        "phases": list(STAGES),
        "artifacts": artifacts,
        "cross_stage_checks": checks,
        "diagnostics": diagnostics,
        "bundle_digest": hashlib.sha256(canonical).hexdigest(),
        "pipeline_passed": not diagnostics and len(artifacts) == len(STAGES),
    }


def run_notification_pipeline_ci(reports_root: Path, golden_path: Path) -> dict[str, Any]:
    bundle = build_notification_pipeline_bundle(reports_root)
    try:
        golden = json.loads(golden_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        golden = None
    except (OSError, json.JSONDecodeError):
        golden = None
    diagnostics = list(bundle["diagnostics"])
    if golden is None:
        diagnostics.append("GOLDEN_MANIFEST_MISSING_OR_INVALID")
        golden_match = False
    else:
        golden_match = (
            golden.get("bundle_digest") == bundle["bundle_digest"]
            and golden.get("artifacts") == bundle["artifacts"]
            and golden.get("cross_stage_checks") == bundle["cross_stage_checks"]
        )
        if not golden_match:
            diagnostics.append("GOLDEN_DRIFT_DETECTED")
    passed = bundle["pipeline_passed"] and golden_match and not diagnostics
    return {
        "phase": "UI-OBS-2H",
        "mode": "OFFLINE_NOTIFICATION_PIPELINE_GOLDEN_CI_GATE",
        "status": "PASSED" if passed else "FAILED",
        "exit_code": 0 if passed else 1,
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "network_access": False,
        "actual_notifications_sent": 0,
        "execution_changed": False,
        "golden_match": golden_match,
        "diagnostics": diagnostics,
        "bundle": bundle,
    }
