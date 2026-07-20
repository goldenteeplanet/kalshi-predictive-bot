"""PMB-36 read-only multi-cycle exposure-guard shadow census."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any


def build_shadow_census(cycles: list[dict[str, Any]], *, minimum_cycles: int = 3) -> dict[str, Any]:
    if minimum_cycles < 1:
        raise ValueError("minimum_cycles must be positive")
    normalized = [_cycle(row) for row in cycles]
    cycle_ids = [row["cycle_id"] for row in normalized]
    distinct = len(cycle_ids) == len(set(cycle_ids))
    rows = [item for cycle in normalized for item in cycle["rows"]]
    baseline_eligible = sum(item["baseline_eligible"] for item in rows)
    shadow_eligible = sum(item["shadow_eligible"] for item in rows)
    baseline_capital = sum(
        (item["requested_capital"] for item in rows if item["baseline_eligible"]),
        Decimal(0),
    )
    shadow_capital = sum((item["shadow_capital"] for item in rows), Decimal(0))
    blockers = Counter(item["blocker"] for item in rows if item["blocker"])
    by_category: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"rows": 0, "baseline_eligible": 0, "shadow_eligible": 0}
    )
    for item in rows:
        category = by_category[item["category"]]
        category["rows"] += 1
        category["baseline_eligible"] += int(item["baseline_eligible"])
        category["shadow_eligible"] += int(item["shadow_eligible"])
    gates = {
        "minimum_distinct_cycles": distinct and len(normalized) >= minimum_cycles,
        "policy_disabled": all(not row["policy_enabled"] for row in normalized),
        "runtime_unchanged": all(not row["runtime_policy_changed"] for row in normalized),
        "execution_disabled": all(not row["execution_enabled"] for row in normalized),
        "thresholds_unchanged": all(not row["thresholds_changed"] for row in normalized),
        "complete_attribution": all(row["attribution_complete"] for row in normalized),
    }
    report: dict[str, Any] = {
        "phase": "PMB-36",
        "mode": "READ_ONLY_MULTI_CYCLE_SHADOW_CENSUS",
        "status": "PASSED" if all(gates.values()) else "FAILED",
        "cycle_ids": cycle_ids,
        "counts": {
            "cycles": len(normalized),
            "rows": len(rows),
            "baseline_eligible": baseline_eligible,
            "shadow_eligible": shadow_eligible,
            "shadow_rejections": baseline_eligible - shadow_eligible,
        },
        "capital": {
            "baseline_requested": str(baseline_capital),
            "shadow_allocated": str(shadow_capital),
            "reduction": str(baseline_capital - shadow_capital),
        },
        "blocker_counts": dict(sorted(blockers.items())),
        "categories": dict(sorted(by_category.items())),
        "gates": gates,
        "decision": (
            "SHADOW_EVIDENCE_READY_FOR_REVIEW" if all(gates.values()) else "DO_NOT_ACTIVATE"
        ),
        "guardrails": {
            "cloud_access": False,
            "database_writes": 0,
            "execution_enabled": False,
            "policy_activation": False,
            "threshold_changes": 0,
        },
    }
    report["report_sha256"] = hashlib.sha256(_canonical(report).encode()).hexdigest()
    return report


def write_report(report: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb36_multi_cycle_shadow_census.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(_canonical(report), encoding="utf-8")
    temporary.replace(path)
    return path


def _cycle(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("each cycle must be an object")
    rows = value.get("rows")
    if not isinstance(rows, list):
        raise ValueError("each cycle must contain rows")
    return {
        "cycle_id": str(value.get("cycle_id") or value.get("generated_at") or "MISSING"),
        "policy_enabled": bool(value.get("policy_enabled")),
        "runtime_policy_changed": bool(value.get("runtime_policy_changed")),
        "execution_enabled": bool(value.get("execution_enabled")),
        "thresholds_changed": bool(value.get("thresholds_changed")),
        "attribution_complete": bool(value.get("summary", {}).get("all_attribution_complete")),
        "rows": [_row(row) for row in rows],
    }


def _row(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("shadow row must be an object")
    baseline = value.get("baseline", {})
    shadow = value.get("shadow", {})
    return {
        "category": str(value.get("category")),
        "baseline_eligible": bool(baseline.get("eligible")),
        "shadow_eligible": bool(shadow.get("eligible")),
        "requested_capital": Decimal(str(baseline.get("requested_capital", "0"))),
        "shadow_capital": Decimal(str(shadow.get("allocated_capital", "0"))),
        "blocker": shadow.get("blocker"),
    }


def _canonical(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"
