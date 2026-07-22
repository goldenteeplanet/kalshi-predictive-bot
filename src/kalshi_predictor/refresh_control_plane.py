from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "refresh-control-plane-v1"


def write_refresh_control_plane_bundle(
    payload: dict[str, Any],
    *,
    output_dir: Path,
    candidate_manifest_path: Path,
) -> dict[str, str]:
    root = output_dir / "control_plane"
    history_dir = root / "cycle_history"
    reports_dir = root / "reports"
    history_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    manifest = _read_json(candidate_manifest_path)
    current = _normalized_cycle(payload, manifest)
    previous_path = _latest_history_path(history_dir)
    previous = _read_json(previous_path) if previous_path else {}
    changes = build_cycle_changes(previous, current)
    lifecycle = update_candidate_lifecycle(
        root / "candidate_lifecycle.json", previous, current
    )
    blockers = build_blocker_intelligence(
        root / "blocker_intelligence.json", current
    )
    scorecard = build_data_quality_scorecard(current)
    alerts = update_incident_history(root / "incident_history.json", current, scorecard)
    cycle_id = str(current["cycle_id"])
    history_path = history_dir / f"{cycle_id}.json"
    current_path = root / "current_cycle.json"
    changes_path = root / "cycle_changes.json"
    scorecard_path = root / "data_quality_scorecard.json"
    lifecycle_path = root / "candidate_lifecycle.json"
    blocker_path = root / "blocker_intelligence.json"
    incident_path = root / "incident_history.json"
    _write_json(history_path, current)
    _write_json(current_path, current)
    _write_json(changes_path, changes)
    _write_json(lifecycle_path, lifecycle)
    _write_json(blocker_path, blockers)
    _write_json(scorecard_path, scorecard)
    _write_json(incident_path, alerts)
    executive_path = reports_dir / "executive_summary.md"
    operator_path = reports_dir / "operator_report.md"
    audit_path = reports_dir / "audit_evidence.json"
    executive_path.write_text(_executive_report(current, changes, scorecard), encoding="utf-8")
    operator_path.write_text(
        _operator_report(current, changes, blockers, scorecard, alerts), encoding="utf-8"
    )
    _write_json(
        audit_path,
        {
            "schema_version": SCHEMA_VERSION,
            "cycle": current,
            "changes": changes,
            "lifecycle": lifecycle,
            "blockers": blockers,
            "scorecard": scorecard,
            "incidents": alerts,
        },
    )
    return {
        "current_cycle": str(current_path),
        "cycle_changes": str(changes_path),
        "candidate_lifecycle": str(lifecycle_path),
        "blocker_intelligence": str(blocker_path),
        "data_quality_scorecard": str(scorecard_path),
        "incident_history": str(incident_path),
        "executive_report": str(executive_path),
        "operator_report": str(operator_path),
        "audit_evidence": str(audit_path),
    }


def verify_authoritative_cloud_snapshot(path: Path) -> dict[str, Any]:
    envelope = _read_json(path)
    snapshot = envelope.get("snapshot") if isinstance(envelope.get("snapshot"), dict) else {}
    expected = str(envelope.get("sha256") or "")
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    actual = hashlib.sha256(canonical).hexdigest()
    required = {
        "deployment_commit_sha",
        "host_id",
        "environment",
        "service_status",
        "timer_status",
        "last_successful_refresh",
        "collected_at",
        "artifact_hashes",
    }
    missing = sorted(required - set(snapshot))
    verified = bool(snapshot) and not missing and expected == actual
    return {
        "state": "VERIFIED_CLOUD" if verified else "UNVERIFIED_OR_MISSING",
        "verified": verified,
        "path": str(path),
        "expected_sha256": expected or None,
        "actual_sha256": actual if snapshot else None,
        "missing_fields": missing,
        "snapshot": snapshot if verified else {},
    }


def write_authoritative_cloud_snapshot(snapshot: dict[str, Any], path: Path) -> Path:
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    _write_json(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "sha256": hashlib.sha256(canonical).hexdigest(),
            "snapshot": snapshot,
        },
    )
    return path


def build_cycle_changes(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    previous_candidates = _candidate_map(previous)
    current_candidates = _candidate_map(current)
    added = sorted(set(current_candidates) - set(previous_candidates))
    removed = sorted(set(previous_candidates) - set(current_candidates))
    transitions = []
    for ticker in sorted(set(previous_candidates) & set(current_candidates)):
        before, after = previous_candidates[ticker], current_candidates[ticker]
        changed = {}
        for field in ("lifecycle", "fresh", "estimated_edge", "blocking_gates"):
            if before.get(field) != after.get(field):
                changed[field] = {"before": before.get(field), "after": after.get(field)}
        if changed:
            transitions.append({"ticker": ticker, "changes": changed})
    previous_blockers = Counter(previous.get("blocker_counts") or {})
    current_blockers = Counter(current.get("blocker_counts") or {})
    return {
        "schema_version": SCHEMA_VERSION,
        "from_cycle_id": previous.get("cycle_id"),
        "to_cycle_id": current.get("cycle_id"),
        "comparable": bool(previous),
        "candidates_added": added,
        "candidates_removed": removed,
        "candidate_transitions": transitions,
        "blockers_added": sorted((current_blockers - previous_blockers).elements()),
        "blockers_cleared": sorted((previous_blockers - current_blockers).elements()),
        "soak_reset": bool(previous)
        and int(current.get("soak", {}).get("consecutive_healthy_cycles") or 0)
        < int(previous.get("soak", {}).get("consecutive_healthy_cycles") or 0),
        "source_reconnect_delta": int(current.get("source_reconnects") or 0)
        - int(previous.get("source_reconnects") or 0),
    }


def update_candidate_lifecycle(
    path: Path, previous: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    ledger = _read_json(path)
    histories = ledger.get("candidates") if isinstance(ledger.get("candidates"), dict) else {}
    previous_candidates = _candidate_map(previous)
    for ticker, row in _candidate_map(current).items():
        record = histories.setdefault(ticker, {"ticker": ticker, "transitions": []})
        before = previous_candidates.get(ticker, {}).get("lifecycle")
        after = row.get("lifecycle")
        if not record["transitions"] or before != after:
            record["transitions"].append(
                {
                    "cycle_id": current.get("cycle_id"),
                    "at": current.get("generated_at"),
                    "from": before,
                    "to": after,
                    "reason": row.get("blocking_gates") or [row.get("selection_tier")],
                    "source": current.get("source_authority"),
                }
            )
        record["current_state"] = after
        record["last_seen_at"] = current.get("generated_at")
    return {"schema_version": SCHEMA_VERSION, "candidates": histories}


def build_blocker_intelligence(path: Path, current: dict[str, Any]) -> dict[str, Any]:
    previous = _read_json(path)
    previous_rows = {
        str(row.get("blocker")): row
        for row in previous.get("rows") or []
        if isinstance(row, dict)
    }
    total = len(current.get("candidates") or [])
    counts = Counter(current.get("blocker_counts") or {})
    category_counts: dict[str, Counter[str]] = {}
    ticker_map: dict[str, list[str]] = {}
    for row in current.get("candidates") or []:
        category = str(row.get("category") or "unknown")
        category_counts.setdefault(category, Counter()).update(row.get("blocking_gates") or [])
        for blocker in row.get("blocking_gates") or []:
            ticker_map.setdefault(str(blocker), []).append(str(row.get("ticker")))
    rows = []
    for blocker, count in sorted(counts.items()):
        old = previous_rows.get(blocker, {})
        rows.append(
            {
                "blocker": blocker,
                "count": count,
                "denominator": total,
                "first_seen_at": old.get("first_seen_at") or current.get("generated_at"),
                "last_seen_at": current.get("generated_at"),
                "consecutive_cycles": int(old.get("consecutive_cycles") or 0) + 1,
                "tickers": sorted(ticker_map.get(blocker, [])),
                "categories": {
                    category: values.get(blocker, 0)
                    for category, values in category_counts.items()
                    if values.get(blocker, 0)
                },
                "next_action": _next_action(blocker),
            }
        )
    return {"schema_version": SCHEMA_VERSION, "cycle_id": current.get("cycle_id"), "rows": rows}


def build_data_quality_scorecard(current: dict[str, Any]) -> dict[str, Any]:
    candidates = current.get("candidates") or []
    total = len(candidates)
    fresh = sum(bool(row.get("fresh")) for row in candidates)
    with_snapshot = sum(row.get("lifecycle") != "SNAPSHOT_NEEDED" for row in candidates)
    ranked = sum(row.get("lifecycle") not in {"DISCOVERED", "WARMING"} for row in candidates)
    risk = sum("risk_missing" not in row.get("blocking_gates", []) for row in candidates)
    metrics = [
        _quality_metric("candidate_manifest_freshness", fresh, total, 0.60, "candidate manifest"),
        _quality_metric("orderbook_coverage", with_snapshot, total, 0.80, "candidate lifecycle"),
        _quality_metric("ranking_coverage", ranked, total, 0.80, "candidate lifecycle"),
        _quality_metric("risk_evidence_coverage", risk, total, 0.80, "candidate gates"),
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "cycle_id": current.get("cycle_id"),
        "metrics": metrics,
        "passed": bool(metrics) and all(row["passed"] for row in metrics),
    }


def update_incident_history(
    path: Path, current: dict[str, Any], scorecard: dict[str, Any]
) -> dict[str, Any]:
    ledger = _read_json(path)
    incidents = ledger.get("incidents") if isinstance(ledger.get("incidents"), list) else []
    active_codes = set()
    if current.get("status") == "CYCLE_NEEDS_ATTENTION":
        active_codes.add("UNHEALTHY_CYCLE")
    if not scorecard.get("passed"):
        active_codes.add("QUALITY_COVERAGE_BELOW_THRESHOLD")
    if current.get("safety_changed"):
        active_codes.add("UNEXPECTED_SAFETY_STATE_CHANGE")
    by_code = {str(row.get("code")): row for row in incidents if isinstance(row, dict)}
    for code in active_codes:
        incident = by_code.get(code)
        if incident and incident.get("state") == "OPEN":
            incident["last_seen_at"] = current.get("generated_at")
            incident["occurrences"] = int(incident.get("occurrences") or 0) + 1
        else:
            incidents.append(
                {
                    "code": code,
                    "state": "OPEN",
                    "first_seen_at": current.get("generated_at"),
                    "last_seen_at": current.get("generated_at"),
                    "occurrences": 1,
                    "cycle_id": current.get("cycle_id"),
                }
            )
    for incident in incidents:
        if incident.get("state") == "OPEN" and incident.get("code") not in active_codes:
            incident["state"] = "RESOLVED"
            incident["resolved_at"] = current.get("generated_at")
    return {"schema_version": SCHEMA_VERSION, "incidents": incidents[-200:]}


def _normalized_cycle(payload: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    candidates = [
        _normalize_candidate(row)
        for row in manifest.get("candidates") or []
        if isinstance(row, dict)
    ]
    blockers = Counter(
        gate for row in candidates for gate in row.get("blocking_gates") or []
    )
    websocket = payload.get("websocket_drain") or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "cycle_id": payload.get("cycle_id") or payload.get("generated_at"),
        "generated_at": payload.get("generated_at"),
        "status": payload.get("status"),
        "source_authority": "GH2_SINGLE_WRITER_CYCLE_ARTIFACT",
        "telemetry": payload.get("cycle_telemetry") or {},
        "soak": payload.get("soak") or {},
        "soak_quality": payload.get("soak_quality") or {},
        "safety": payload.get("safety") or {},
        "candidates": candidates,
        "blocker_counts": dict(blockers),
        "source_reconnects": int(websocket.get("reconnects") or 0),
        "errors": payload.get("errors") or [],
        "safety_changed": any(
            bool((payload.get("safety") or {}).get(key))
            for key in (
                "paper_order_creation_enabled",
                "live_execution_enabled",
                "autopilot_enabled",
            )
        ),
    }


def _normalize_candidate(row: dict[str, Any]) -> dict[str, Any]:
    gates = [str(item) for item in row.get("blocking_gates") or []]
    if row.get("selection_tier") == "MISSING_SNAPSHOT_RECOVERY":
        lifecycle = "SNAPSHOT_NEEDED"
    elif not row.get("fresh"):
        lifecycle = "WARMING"
    elif not row.get("positive_edge"):
        lifecycle = "RANKED"
    elif gates:
        lifecycle = "RISK_CHECKED_BLOCKED"
    elif row.get("executable"):
        lifecycle = "PAPER_READY"
    else:
        lifecycle = "POSITIVE_EV"
    ticker = str(row.get("ticker") or "unknown")
    category = (
        "weather"
        if ticker.startswith("KXTEMP")
        else "crypto"
        if ticker.startswith("KX")
        else "other"
    )
    return {
        "ticker": ticker,
        "category": category,
        "lifecycle": lifecycle,
        "selection_tier": row.get("selection_tier"),
        "fresh": bool(row.get("fresh")),
        "estimated_edge": row.get("estimated_edge"),
        "blocking_gates": gates,
    }


def _candidate_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("ticker")): row
        for row in payload.get("candidates") or []
        if isinstance(row, dict) and row.get("ticker")
    }


def _quality_metric(
    name: str, numerator: int, denominator: int, threshold: float, source: str
) -> dict[str, Any]:
    ratio = numerator / denominator if denominator else 0.0
    return {
        "name": name,
        "numerator": numerator,
        "denominator": denominator,
        "ratio": round(ratio, 4),
        "threshold": threshold,
        "passed": denominator > 0 and ratio >= threshold,
        "source": source,
    }


def _next_action(blocker: str) -> str:
    normalized = blocker.lower()
    if "snapshot" in normalized:
        return "Verify the read-only WebSocket subscription and snapshot drain."
    if "liquidity" in normalized or "spread" in normalized:
        return "Continue observing executable books; do not weaken liquidity gates."
    if "risk" in normalized:
        return "Inspect Phase 3N evidence generation; do not bypass risk gates."
    if "edge" in normalized:
        return "Continue collecting forecasts; do not lower the edge threshold."
    return "Inspect the source artifact and preserve existing safety thresholds."


def _executive_report(
    current: dict[str, Any], changes: dict[str, Any], scorecard: dict[str, Any]
) -> str:
    soak = current.get("soak") or {}
    soak_label = (
        f"{soak.get('consecutive_healthy_cycles', 0)}/"
        f"{soak.get('required_healthy_cycles', 24)}"
    )
    added_removed = (
        f"{len(changes.get('candidates_added') or [])}/"
        f"{len(changes.get('candidates_removed') or [])}"
    )
    return "\n".join(
        [
            "# Refresh & Readiness Executive Summary",
            "",
            f"- Cycle: `{current.get('cycle_id')}`",
            f"- Status: `{current.get('status')}`",
            f"- Soak: `{soak_label}`",
            f"- Data quality gate: `{'PASS' if scorecard.get('passed') else 'BLOCKED'}`",
            f"- Candidates added/removed: `{added_removed}`",
            "- Paper-order creation and live execution remain disabled.",
            "",
        ]
    )


def _operator_report(
    current: dict[str, Any],
    changes: dict[str, Any],
    blockers: dict[str, Any],
    scorecard: dict[str, Any],
    alerts: dict[str, Any],
) -> str:
    lines = [_executive_report(current, changes, scorecard), "## Blockers", ""]
    for row in blockers.get("rows") or []:
        lines.append(
            f"- `{row['blocker']}`: {row['count']}/{row['denominator']} — {row['next_action']}"
        )
    lines.extend(["", "## Open incidents", ""])
    for row in alerts.get("incidents") or []:
        if row.get("state") == "OPEN":
            lines.append(f"- `{row.get('code')}` ({row.get('occurrences')} occurrences)")
    return "\n".join(lines) + "\n"


def _latest_history_path(history_dir: Path) -> Path | None:
    paths = sorted(history_dir.glob("*.json"), key=lambda path: path.stat().st_mtime)
    return paths[-1] if paths else None


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
