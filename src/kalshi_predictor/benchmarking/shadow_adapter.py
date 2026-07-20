from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class ExposureGuardPolicy:
    max_forecast_bias_magnitude: Decimal = Decimal("0.008")
    max_spread_addition: Decimal = Decimal("0.008")
    position_scale: Decimal = Decimal("0.95")
    enabled: bool = False


class ExposureGuardShadowAdapter:
    def __init__(self, policy: ExposureGuardPolicy | None = None) -> None:
        self.policy = policy or ExposureGuardPolicy()

    def preview(self, ranking: Mapping[str, Any]) -> dict[str, Any]:
        source = deepcopy(dict(ranking))
        baseline_eligible = bool(source["risk_gate_passed"])
        forecast_bias = abs(Decimal(str(source["forecast_bias"])))
        spread_addition = Decimal(str(source["spread_addition"]))
        inside_buffer = (
            forecast_bias <= self.policy.max_forecast_bias_magnitude
            and spread_addition <= self.policy.max_spread_addition
        )
        requested = Decimal(str(source["requested_capital"]))
        shadow_capital = (
            requested * self.policy.position_scale
            if baseline_eligible and inside_buffer else Decimal("0")
        )
        blocker = (
            None if baseline_eligible and inside_buffer
            else "STRESS_BUFFER_EXCEEDED" if baseline_eligible
            else "BASELINE_RISK_GATE_FAILED"
        )
        return {
            "ticker": source["ticker"],
            "category": source["category"],
            "baseline": {
                "eligible": baseline_eligible,
                "requested_capital": str(requested),
                "opportunity_score": str(source["opportunity_score"]),
            },
            "shadow": {
                "eligible": baseline_eligible and inside_buffer,
                "allocated_capital": str(shadow_capital),
                "position_scale": str(self.policy.position_scale),
                "inside_certified_buffer": inside_buffer,
                "blocker": blocker,
            },
            "attribution": deepcopy(source["attribution"]),
            "runtime_effect": "NONE_DISABLED_SHADOW",
            "policy_enabled": self.policy.enabled,
            "source_unchanged": source == dict(ranking),
        }


SYNTHETIC_RANKINGS = tuple(
    {
        "ticker": f"PMB33-{category.upper()}-{index}",
        "category": category,
        "opportunity_score": str(70 + index),
        "forecast_bias": bias,
        "spread_addition": spread,
        "requested_capital": "10",
        "risk_gate_passed": risk,
        "attribution": {
            "forecast_id": 1000 + index,
            "feature_ref": f"feature:{category}:{index}",
            "observation_ref": f"observation:{category}:{index}",
            "market_snapshot_id": 2000 + index,
            "model_version": "synthetic-v1",
        },
    }
    for index, (category, bias, spread, risk) in enumerate((
        ("crypto", "-0.004", "0.004", True),
        ("crypto", "-0.010", "0.004", True),
        ("crypto", "-0.002", "0.002", False),
        ("weather", "-0.006", "0.008", True),
        ("weather", "-0.004", "0.010", True),
        ("weather", "0", "0", False),
        ("sports", "-0.008", "0.008", True),
        ("sports", "-0.010", "0.010", True),
        ("sports", "-0.002", "0.006", True),
    ))
)


def build_exposure_guard_shadow_adapter_preview() -> dict[str, Any]:
    policy = ExposureGuardPolicy()
    adapter = ExposureGuardShadowAdapter(policy)
    rows = [adapter.preview(row) for row in SYNTHETIC_RANKINGS]
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "PMB-33",
        "mode": "LOCAL_SYNTHETIC_EXPOSURE_GUARD_SHADOW_ADAPTER_PREVIEW",
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "policy_enabled": policy.enabled,
        "runtime_policy_changed": False,
        "policy": {
            "max_forecast_bias_magnitude": str(policy.max_forecast_bias_magnitude),
            "max_spread_addition": str(policy.max_spread_addition),
            "position_scale": str(policy.position_scale),
        },
        "rows": rows,
        "summary": {
            "rows": len(rows),
            "baseline_eligible": sum(row["baseline"]["eligible"] for row in rows),
            "shadow_eligible": sum(row["shadow"]["eligible"] for row in rows),
            "buffer_rejections": sum(
                row["shadow"]["blocker"] == "STRESS_BUFFER_EXCEEDED" for row in rows
            ),
            "all_sources_unchanged": all(row["source_unchanged"] for row in rows),
            "all_attribution_complete": all(
                all(row["attribution"].get(key) for key in (
                    "forecast_id", "feature_ref", "observation_ref",
                    "market_snapshot_id", "model_version",
                )) for row in rows
            ),
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_exposure_guard_shadow_adapter_preview(output_dir: Path) -> Path:
    report = build_exposure_guard_shadow_adapter_preview()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb33_exposure_guard_shadow_adapter_preview.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path
