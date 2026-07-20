from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PASS_PREFIXES = ("PASSED", "VERIFIED", "OK")
FAIL_STATES = {"FAILED", "ERROR"}
BLOCK_STATES = {"BLOCKED", "NOT_READY", "INCOMPLETE"}


@dataclass(frozen=True)
class PhaseSpec:
    phase_id: str
    label: str
    reports: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    approval_required: bool = False


PHASE_SPECS = (
    PhaseSpec("R5-RECOVERY-3", "Finish backup verification and certify one bounded cycle", ("phase_r5_recovery3b/r5_recovery3b_guarded_one_cycle_certification.json",)),
    PhaseSpec("R5-RECOVERY-6", "Three guarded scheduler cycles", ("phase_r5_recovery6/r5_recovery6_three_cycle_certification.json",), ("R5-RECOVERY-3",)),
    PhaseSpec("R5-RECOVERY-8", "Consolidated stability census", ("phase_r5_recovery8/r5_recovery8_three_cycle_stability_census.json",), ("R5-RECOVERY-6",)),
    PhaseSpec("R5-RECOVERY-9-PREVIEW", "Permanent bounded scheduler preview", ("phase_r5_recovery9/r5_recovery9_preview.json",), ("R5-RECOVERY-8",)),
    PhaseSpec("R5-RECOVERY-9", "Permanent bounded scheduler deployment", ("phase_r5_recovery9/r5_recovery9_deployment_certification.json",), ("R5-RECOVERY-9-PREVIEW",), True),
    PhaseSpec("STORAGE-CAP-1", "Backup retention model", ("phase_storage_cap1/storage_cap1_capacity_plan.json",)),
    PhaseSpec("STORAGE-CAP-2", "Verified cold-backup archival", ("phase_storage_cap2/storage_cap2_verified_archival.json", "phase_storage_cap2/storage_cap2_backup_volume_expansion.json"), (), True),
    PhaseSpec("PROV-14B", "Future-attribution certification retry", ("phase_prov14/prov14_certification.json",), ("STORAGE-CAP-2",)),
    PhaseSpec("PROV-14C", "Multi-cycle attribution stability census", ("phase_prov14c/prov14c_multi_cycle_attribution_stability_census.json",), ("PROV-14B",)),
    PhaseSpec("PROV-14D", "Guarded scheduler attribution integration", ("phase_prov14d/prov14d_scheduler_integration_certification.json",), ("PROV-14C",), True),
    PhaseSpec("PROV-16", "Retention, performance, and dashboard parity", ("phase_prov16/prov16_retention_performance_dashboard_parity.json",), ("PROV-14D",)),
    PhaseSpec("PMB-35", "Disabled exposure-guard shadow deployment", ("phase_pmb35/pmb35_shadow_deployment_certification.json",), (), True),
    PhaseSpec("PMB-36", "Multi-cycle shadow comparison census", ("phase_pmb36/pmb36_multi_cycle_shadow_census.json",), ("PMB-35",)),
    PhaseSpec("GH-1V", "Remaining liquidity windows", ("phase_gh1v/gh1v_fresh_near_miss_multi_window_watch.json",)),
    PhaseSpec("GH-1X", "Liquidity and executable-edge census", ("phase_gh1x/gh1x_liquidity_edge_risk_census.json",), ("GH-1V",)),
    PhaseSpec("NYC-W10", "Weather live-shadow stability review", ("phase_nyc_w10/nyc_w10_live_shadow_stability_review.json",)),
    PhaseSpec("NYC-W11", "Weather activation and rollback preview", ("phase_nyc_w11/nyc_w11_activation_preview.json",), ("NYC-W10",)),
    PhaseSpec("UI-OBS-5", "Operational dashboard summaries", ("phase_ui_obs5k/ui_obs5k_render_certification.json",)),
    PhaseSpec("READINESS-1", "Consolidated paper-readiness recheck", ("phase_readiness1/readiness1_gate_recheck.json",)),
    PhaseSpec("READINESS-2", "Failed-gate attribution and remediation plan", ("phase_readiness2/readiness2_failed_gate_attribution_fresh_20260719.json",), ("READINESS-1",)),
)


def publish_exact_phase_roadmap(
    reports_root: Path,
    *,
    approvals: Mapping[str, bool] | None = None,
    runtime_states: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    approvals = approvals or {}
    runtime_states = runtime_states or {}
    rows: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    for spec in PHASE_SPECS:
        evidence = [_read_evidence(reports_root, relative) for relative in spec.reports]
        evidence = [item for item in evidence if item is not None]
        explicit_states = [item["source_status"] for item in evidence]
        state = _state_from_evidence(explicit_states)
        runtime = str(runtime_states.get(spec.phase_id) or "").upper()
        if runtime:
            if runtime not in {"RUNNING", "WAITING", "BLOCKED", "FAILED"}:
                diagnostics.append(f"RUNTIME_PHASE_STATE_INVALID:{spec.phase_id}")
            elif state != "PASSED":
                state = runtime
        approved = approvals.get(spec.phase_id) is True
        if spec.approval_required and not approved and state not in {"PASSED", "FAILED"}:
            state = "APPROVAL_REQUIRED"
        rows.append(
            {
                "id": spec.phase_id,
                "label": spec.label,
                "state": state,
                "approval_required": spec.approval_required,
                "approved": approved,
                "dependencies": list(spec.dependencies),
                "evidence": [
                    {
                        "path": item["path"],
                        "status": item["source_status"],
                        "sha256": item["sha256"],
                    }
                    for item in evidence
                ],
                "blocker": "No explicit passing evidence" if state in {"WAITING", "BLOCKED"} else "",
            }
        )
    return {
        "schema_version": 1,
        "read_only": True,
        "reports_root": str(reports_root),
        "exact_phase_roadmap": rows,
        "diagnostics": diagnostics,
    }


def _read_evidence(reports_root: Path, relative: str) -> dict[str, str] | None:
    path = reports_root / relative
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    status = str(payload.get("status") or "UNVERIFIED").upper()
    return {
        "path": str(path),
        "source_status": status,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _state_from_evidence(states: list[str]) -> str:
    if any(state.startswith(PASS_PREFIXES) for state in states):
        return "PASSED"
    if any(state in FAIL_STATES for state in states):
        return "FAILED"
    if any(state in BLOCK_STATES for state in states):
        return "BLOCKED"
    return "WAITING"
