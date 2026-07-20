from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed


def reconcile_notification_receipts(
    routing: Mapping[str, Any], delivery: Mapping[str, Any], *, max_latency_seconds: int = 60,
) -> dict[str, Any]:
    decisions = list(routing.get("decisions") or [])
    receipts = list(delivery.get("new_receipts") or [])
    expected = {}
    for decision in decisions:
        if decision.get("action") not in {"DELIVER_NOW", "TIMELINE_ONLY"}:
            continue
        for channel in decision.get("channels") or []:
            expected[(decision.get("incident_id"), channel)] = decision
    grouped: dict[tuple[Any, Any], list[Mapping[str, Any]]] = {}
    for receipt in receipts:
        grouped.setdefault((receipt.get("incident_id"), receipt.get("channel")), []).append(receipt)
    rows = []
    missing, duplicate, late, unexpected, rate_limited = [], [], [], [], []
    routed_at = _time(str(routing.get("policy", {}).get("as_of_local")))
    for key, decision in sorted(expected.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))):
        matches = grouped.get(key, [])
        delivered = [row for row in matches if row.get("status") == "SIMULATED_DELIVERED"]
        limited = [row for row in matches if row.get("status") == "SIMULATED_RATE_LIMITED"]
        if not matches:
            missing.append(f"{key[0]}:{key[1]}")
        if len(matches) > 1:
            duplicate.append(f"{key[0]}:{key[1]}")
        if limited:
            rate_limited.append(f"{key[0]}:{key[1]}")
        latencies = [max(0, int((_time(str(row["delivered_at"])) - routed_at).total_seconds())) for row in delivered]
        if any(value > max_latency_seconds for value in latencies):
            late.append(f"{key[0]}:{key[1]}")
        rows.append({
            "incident_id": key[0], "channel": key[1], "severity": decision.get("severity"),
            "expected": True, "receipt_count": len(matches), "delivered_count": len(delivered),
            "rate_limited_count": len(limited), "latency_seconds": latencies,
            "coverage_passed": len(delivered) == 1 and len(matches) == 1 and all(value <= max_latency_seconds for value in latencies),
        })
    for key, matches in sorted(grouped.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))):
        if key not in expected:
            unexpected.append(f"{key[0]}:{key[1]}")
            rows.append({
                "incident_id": key[0], "channel": key[1], "severity": matches[0].get("severity"),
                "expected": False, "receipt_count": len(matches), "delivered_count": sum(row.get("status") == "SIMULATED_DELIVERED" for row in matches),
                "rate_limited_count": sum(row.get("status") == "SIMULATED_RATE_LIMITED" for row in matches),
                "latency_seconds": [], "coverage_passed": False,
            })
    critical_rows = [row for row in rows if row["expected"] and row["severity"] == "CRITICAL"]
    critical_coverage = bool(critical_rows) and all(row["coverage_passed"] for row in critical_rows)
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "UI-OBS-2G",
        "mode": "LOCAL_NOTIFICATION_RECEIPT_AUDIT_RECONCILIATION_PREVIEW",
        "external_services_contacted": False,
        "network_access": False,
        "database_access": False,
        "database_writes": 0,
        "execution_changed": False,
        "max_latency_seconds": max_latency_seconds,
        "rows": rows,
        "findings": {
            "missing": missing, "duplicate": duplicate, "late": late,
            "unexpected": unexpected, "rate_limited": rate_limited,
        },
        "summary": {
            "expected_receipts": len(expected), "actual_receipts": len(receipts),
            "missing": len(missing), "duplicate": len(duplicate), "late": len(late),
            "unexpected": len(unexpected), "rate_limited": len(rate_limited),
            "critical_channels_expected": len(critical_rows),
            "critical_coverage_complete": critical_coverage,
            "reconciliation_passed": not any((missing, duplicate, late, unexpected, rate_limited)) and critical_coverage,
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_notification_receipt_audit(routing_path: Path, delivery_path: Path, output_dir: Path) -> Path:
    report = reconcile_notification_receipts(
        json.loads(routing_path.read_text(encoding="utf-8")),
        json.loads(delivery_path.read_text(encoding="utf-8")),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "ui_obs2g_notification_receipt_audit.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
