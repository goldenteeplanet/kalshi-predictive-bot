"""Offline, original-bound economics audit. Never grants execution authority."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any


def decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("BOOLEAN_IS_NOT_COST")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("FINITE_VALUE_REQUIRED")
    return result


def component_value(component: dict[str, Any], *, fee: bool = False) -> Decimal | None:
    status = component.get("status", "UNKNOWN")
    value = decimal(component.get("value"))
    if status == "UNKNOWN":
        if value is not None:
            raise ValueError("UNKNOWN_MUST_REMAIN_NULL")
        return None
    allowed = {"CERTIFIED"} if fee else {"CERTIFIED", "ESTIMATED"}
    if status not in allowed:
        return None
    hashes = component.get("evidence_hashes", [])
    if value is None or value < 0 or not hashes or not component.get("method_version"):
        raise ValueError("COST_PROVENANCE_REQUIRED")
    if any(len(h) != 64 or any(c not in "0123456789abcdef" for c in h) for h in hashes):
        raise ValueError("COST_EVIDENCE_HASH_REQUIRED")
    return value


def build_funnel(report: dict[str, Any], source_sha256: str) -> dict[str, Any]:
    rows = []
    for index, case in enumerate(report["economics"]):
        gross = decimal(case["gross_edge"])
        probability = decimal(case["probability"])
        price = decimal(case["executable_price"])
        if gross is None or probability is None or price is None:
            raise ValueError("COMPLETE_FROZEN_PRICE_AND_PROBABILITY_REQUIRED")
        if not 0 <= probability <= 1 or not 0 < price < 1:
            raise ValueError("UNIT_PROBABILITY_AND_EXECUTABLE_PRICE_REQUIRED")
        if abs(probability - price - gross) > Decimal("0.000000000001"):
            raise ValueError("FROZEN_GROSS_ARITHMETIC_MISMATCH")
        fee = component_value(case["fee"], fee=True)
        slippage = component_value(case["slippage"])
        uncertainty = component_value(case["uncertainty"])
        after_fee = None if fee is None else gross - fee
        after_slippage = None if after_fee is None or slippage is None else after_fee - slippage
        net = (
            None if after_slippage is None or uncertainty is None else after_slippage - uncertainty
        )
        blockers = list(case.get("blockers", []))
        if fee is None:
            blockers.append("FEE_APPLICABILITY_UNKNOWN")
        if slippage is None:
            blockers.append("SLIPPAGE_UNKNOWN")
        if uncertainty is None:
            blockers.append("UNCERTAINTY_UNKNOWN")
        # A diagnostic worst-case bound is not calibrated uncertainty or a cost override.
        # With no independent evidence, true probability can be zero: subtracting p
        # leaves -price even before fees/slippage. Preserve the original UNKNOWN.
        insufficient_bound = probability if uncertainty is None else None
        rows.append(
            {
                "case_index": index,
                "event": case["event"],
                "ticker": case["ticker"],
                "model": case["model"],
                "side": case["side"],
                "decision_id": case["decision_id"],
                "rule_version": case["rule_version"],
                "source_sha256": source_sha256,
                "probability": probability,
                "executable_price": price,
                "gross_edge": gross,
                "fee": case["fee"],
                "slippage": case["slippage"],
                "uncertainty": case["uncertainty"],
                "after_fee": after_fee,
                "after_slippage": after_slippage,
                "after_uncertainty": net,
                "full_net_ev": net,
                "status": "FULL_NET_EV_UNKNOWN" if net is None else "FULL_NET_EV_KNOWN",
                "shortfall_to_five_cents": None
                if net is None
                else max(Decimal(0), Decimal("0.05") - net),
                "evidence_tier": "INSUFFICIENT" if uncertainty is None else "REQUIRES_TIER_REVIEW",
                "insufficient_evidence_probability_bound": insufficient_bound,
                "worst_case_edge_before_fee_and_slippage": -price
                if insufficient_bound is not None
                else None,
                "phase_3m": "NOT_ESTABLISHED",
                "phase_3n": "NOT_ESTABLISHED",
                "paper_eligible": False,
                "blockers": sorted(set(blockers)),
            }
        )

    def stage(key: str, threshold: Decimal = Decimal(0)) -> dict[str, int]:
        known = [r[key] for r in rows if r[key] is not None]
        return {
            "known": len(known),
            "unknown": len(rows) - len(known),
            "positive": sum(value > threshold for value in known),
        }

    known_rows = [
        r for r in rows if r["full_net_ev"] is not None and r["full_net_ev"] <= Decimal("0.05")
    ]
    known_rows.sort(key=lambda r: r["full_net_ev"], reverse=True)
    unknown_rows = [r for r in rows if r["full_net_ev"] is None]
    unknown_rows.sort(key=lambda r: r["gross_edge"], reverse=True)
    return {
        "schema": "frozen-full-net-ev-funnel-v1",
        "source_sha256": source_sha256,
        "total_cases": len(rows),
        "event_n": len({r["event"] for r in rows}),
        "independent_event_n": None,
        "stages": {
            key: stage(key)
            for key in (
                "gross_edge",
                "after_fee",
                "after_slippage",
                "after_uncertainty",
                "full_net_ev",
            )
        },
        "full_net_gt_five_cents": stage("full_net_ev", Decimal("0.05")),
        "phase_3m_nonzero": {"verified": 0, "unknown": len(rows)},
        "phase_3n_allow": {"verified": 0, "unknown": len(rows)},
        "paper_eligible": 0,
        "execution_authority": False,
        "blocker_counts": dict(Counter(b for r in rows for b in r["blockers"])),
        "near_misses": known_rows[:10],
        "gross_only_cost_unknown_shortlist": unknown_rows[:10],
        "cases": rows,
        "limitations": (
            "Historical audit, not current admission. Worst-case bounds are not calibration. "
            "Unknown costs remain null; source strata and dependent cases are not IID trials."
        ),
    }


def audit_file(source: Path, expected_sha256: str, output: Path) -> dict[str, Any]:
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError("FROZEN_REPORT_HASH_MISMATCH")
    result = build_funnel(json.loads(raw), digest)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, default=str, allow_nan=False)
    return {
        k: v
        for k, v in result.items()
        if k not in {"cases", "near_misses", "gross_only_cost_unknown_shortlist"}
    }
