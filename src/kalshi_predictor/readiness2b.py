from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def build_remediation_roadmap(objective_path: Path, readiness_path: Path, liquidity_path: Path) -> dict[str, Any]:
    objective = json.loads(objective_path.read_text(encoding="utf-8"))
    readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
    liquidity = json.loads(liquidity_path.read_text(encoding="utf-8"))
    phases = objective.get("phases", [])
    open_phases = [row for row in phases if row.get("status") in {"WAITING", "BLOCKED", "APPROVAL_REQUIRED", "RUNNING"}]
    blockers = readiness.get("blocker_attribution", [])
    remediation = [
        {"order": 1, "phase": "PROV-14B", "reason": "Produce exact future observation and market-snapshot attribution references.", "activation_allowed": False},
        {"order": 2, "phase": "PROV-14C/14D", "reason": "Prove multi-cycle attribution stability before scheduler integration.", "activation_allowed": False},
        {"order": 3, "phase": "PMB-35A/35", "reason": "Supply the exact missing weather reference probability; never synthesize defaults.", "activation_allowed": False},
        {"order": 4, "phase": "PMB-36", "reason": "Collect disabled-shadow comparisons only after exact compatibility passes.", "activation_allowed": False},
        {"order": 5, "phase": "READINESS-1 recheck", "reason": "Recompute unchanged edge, liquidity, spread, risk, and paper gates from fresh evidence.", "activation_allowed": False},
    ]
    liquidity_summary = liquidity.get("summary", {})
    report: dict[str, Any] = {
        "phase": "READINESS-2B",
        "status": "PASSED_READ_ONLY_REMEDIATION_ROADMAP",
        "mode": "LOCAL_CROSS_WORKSTREAM_REMEDIATION",
        "sources": {"objective": str(objective_path), "readiness": str(readiness_path), "liquidity": str(liquidity_path)},
        "current": {"open_phases": open_phases, "readiness_blockers": blockers, "liquidity_summary": liquidity_summary},
        "remediation_order": remediation,
        "decision": "CONTINUE_EXACT_REPAIRS_WITH_UNCHANGED_GATES",
        "guardrails": {"database_writes": 0, "threshold_changes": 0, "paper_activation": False, "live_activation": False, "execution_enabled": False},
    }
    report["report_sha256"] = hashlib.sha256((json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
    return report


def write_remediation_roadmap(output: Path, report: dict[str, Any]) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)
    return output
