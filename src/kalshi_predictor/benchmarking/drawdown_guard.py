from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.oos_policy import _metrics
from kalshi_predictor.benchmarking.stress_guard import build_stress_aware_allocation_guard_preview


ORDERINGS = ("original", "stress_priority", "category_round_robin")
PER_CATEGORY_CAPS: tuple[Decimal | None, ...] = (
    None, Decimal("150"), Decimal("200"), Decimal("250"),
)
POSITION_SCALES = (Decimal("1"), Decimal("0.95"), Decimal("0.75"))
DECIMAL_COMPARISON_TOLERANCE = Decimal("1e-24")


def build_drawdown_aware_guard_refinement() -> dict[str, Any]:
    source = build_stress_aware_allocation_guard_preview()
    source_rows = [row for row in source["guarded_policy"]["rows"] if row["allocated"]]
    required_roc = Decimal(source["guarded_policy"]["return_on_capital"])
    maximum_drawdown = Decimal(source["baseline"]["max_drawdown"])
    candidates = []
    for ordering in ORDERINGS:
        ordered = _order_rows(source_rows, ordering)
        for position_scale in POSITION_SCALES:
            scaled = _scale_rows(ordered, position_scale)
            for cap in PER_CATEGORY_CAPS:
                selected, cap_rejections = _apply_category_cap(scaled, cap)
                metrics = _metrics(
                    f"{ordering}|scale={position_scale}|cap={cap}", selected
                )
                capital = Decimal(metrics["capital_usage"])
                roc = Decimal(metrics["total_pnl"]) / capital if capital > 0 else Decimal("0")
                metrics["return_on_capital"] = str(roc)
                roc_preserved = roc + DECIMAL_COMPARISON_TOLERANCE >= required_roc
                qualifies = roc_preserved and Decimal(metrics["max_drawdown"]) <= maximum_drawdown
                candidates.append({
                    "ordering": ordering,
                    "position_scale": str(position_scale),
                    "per_category_cap": str(cap) if cap is not None else None,
                    "cap_rejections": cap_rejections,
                    "metrics": metrics,
                    "return_on_capital_preserved": roc_preserved,
                    "drawdown_regression_prevented": Decimal(metrics["max_drawdown"]) <= maximum_drawdown,
                    "qualifies": qualifies,
                    "selection_uses_settlement_outcomes": False,
                })
    qualified = [row for row in candidates if row["qualifies"]]
    recommended = max(
        qualified,
        key=lambda row: (
            Decimal(row["metrics"]["total_pnl"]),
            -Decimal(row["metrics"]["max_drawdown"]),
            row["ordering"],
        ),
        default=None,
    )
    canonical = json.dumps(candidates, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "PMB-24",
        "mode": "LOCAL_SYNTHETIC_DRAWDOWN_AWARE_GUARD_REFINEMENT_PREVIEW",
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "runtime_policy_changed": False,
        "qualification_requirements": {
            "minimum_return_on_capital": str(required_roc),
            "maximum_drawdown": str(maximum_drawdown),
            "settlement_outcomes_may_drive_selection": False,
            "decimal_comparison_tolerance": str(DECIMAL_COMPARISON_TOLERANCE),
        },
        "candidates": candidates,
        "recommended_preview": recommended,
        "summary": {
            "candidate_count": len(candidates),
            "qualified_candidates": len(qualified),
            "recommendation_available": recommended is not None,
            "all_candidates_settlement_blind": all(
                not row["selection_uses_settlement_outcomes"] for row in candidates
            ),
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_drawdown_aware_guard_refinement(output_dir: Path) -> Path:
    report = build_drawdown_aware_guard_refinement()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb24_drawdown_aware_guard_refinement_preview.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _stress_key(row: dict[str, Any]) -> tuple[Decimal, str, int]:
    magnitude = abs(Decimal(row["forecast_bias"])) + Decimal(row["spread_addition"])
    return magnitude, row["category"], int(row["episode_index"])


def _order_rows(rows: list[dict[str, Any]], ordering: str) -> list[dict[str, Any]]:
    if ordering == "original":
        return sorted(rows, key=lambda row: int(row["episode_index"]))
    if ordering == "stress_priority":
        return sorted(rows, key=_stress_key)
    if ordering == "category_round_robin":
        groups: dict[str, deque[dict[str, Any]]] = {}
        for category in sorted({row["category"] for row in rows}):
            groups[category] = deque(sorted(
                (row for row in rows if row["category"] == category), key=_stress_key
            ))
        ordered = []
        while any(groups.values()):
            for category in sorted(groups):
                if groups[category]:
                    ordered.append(groups[category].popleft())
        return ordered
    raise ValueError(f"Unknown ordering: {ordering}")


def _apply_category_cap(
    rows: list[dict[str, Any]], cap: Decimal | None
) -> tuple[list[dict[str, Any]], int]:
    if cap is None:
        return list(rows), 0
    used: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    selected = []
    rejected = 0
    for row in rows:
        capital = Decimal(row["capital_used"])
        if used[row["category"]] + capital > cap:
            rejected += 1
            continue
        used[row["category"]] += capital
        selected.append(row)
    return selected, rejected


def _scale_rows(
    rows: list[dict[str, Any]], scale: Decimal
) -> list[dict[str, Any]]:
    scaled = []
    for row in rows:
        copy = dict(row)
        copy["capital_used"] = str(Decimal(row["capital_used"]) * scale)
        copy["settlement_pnl"] = str(Decimal(row["settlement_pnl"]) * scale)
        copy["position_scale"] = str(scale)
        scaled.append(copy)
    return scaled
