from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.refresh_control_plane import verify_authoritative_cloud_snapshot
from kalshi_predictor.roadmap.artifacts import verify_signed_artifact
from kalshi_predictor.roadmap.status import build_roadmap_status
from kalshi_predictor.utils.time import utc_now

DEFAULT_REFRESH_PATH = Path("reports/phase_gh2/gh2_active_candidate_refresh.json")
DEFAULT_HISTORY_PATH = Path("reports/phase_gh2/gh2_paper_only_soak_history.jsonl")
DEFAULT_MANIFEST_PATH = Path("reports/phase_gh1/watch/actionable_tickers.json")
STALE_AFTER_SECONDS = 30 * 60
DEFAULT_CONTROL_PLANE_ROOT = Path("reports/phase_gh2/control_plane")
DEFAULT_CLOUD_STATUS_PATH = Path("reports/phase_gh2/authoritative_cloud_status.json")
DEFAULT_CATEGORY_CENSUS_PATH = Path("reports/roadmap/category_ingestion_census.json")
DEFAULT_PAPER_THROUGHPUT_PATH = Path("reports/roadmap/paper_settlement_throughput.json")


def build_refresh_readiness_dashboard(
    *,
    refresh_path: Path = DEFAULT_REFRESH_PATH,
    history_path: Path = DEFAULT_HISTORY_PATH,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    control_plane_root: Path = DEFAULT_CONTROL_PLANE_ROOT,
    cloud_status_path: Path = DEFAULT_CLOUD_STATUS_PATH,
    category_census_path: Path = DEFAULT_CATEGORY_CENSUS_PATH,
    paper_throughput_path: Path = DEFAULT_PAPER_THROUGHPUT_PATH,
) -> dict[str, Any]:
    refresh = _read_json(refresh_path)
    history = _read_json_lines(history_path)[-24:]
    manifest = _read_json(manifest_path)
    generated_at = _parse_time(refresh.get("generated_at"))
    age_seconds = max(0, int((utc_now() - generated_at).total_seconds())) if generated_at else None
    source_state = _source_state(refresh, age_seconds)
    soak = refresh.get("soak") if isinstance(refresh.get("soak"), dict) else {}
    readiness = (
        refresh.get("paper_readiness") if isinstance(refresh.get("paper_readiness"), dict) else {}
    )
    candidates = manifest.get("candidates") if isinstance(manifest.get("candidates"), list) else []
    current_blockers = Counter(
        str(gate)
        for row in candidates
        if isinstance(row, dict)
        for gate in (row.get("blocking_gates") or [])
    )
    previous_blockers = _history_blockers(history[-2] if len(history) > 1 else {})
    blocker_rows = _blocker_rows(current_blockers, previous_blockers)
    changes = _read_json(control_plane_root / "cycle_changes.json")
    lifecycle = _read_json(control_plane_root / "candidate_lifecycle.json")
    blocker_intelligence = _read_json(control_plane_root / "blocker_intelligence.json")
    scorecard = _read_json(control_plane_root / "data_quality_scorecard.json")
    incidents = _read_json(control_plane_root / "incident_history.json")
    cloud = verify_authoritative_cloud_snapshot(cloud_status_path)
    category_census = verify_signed_artifact(category_census_path)
    paper_throughput = verify_signed_artifact(paper_throughput_path)
    return {
        "read_only": True,
        "source": {
            "state": source_state,
            "path": str(refresh_path),
            "generated_at": refresh.get("generated_at") or "unavailable",
            "age_label": _age_label(age_seconds),
            "meaning": _source_meaning(source_state),
        },
        "summary": {
            "status": str(refresh.get("status") or source_state),
            "healthy": bool(soak.get("healthy_cycle")),
            "soak_current": int(soak.get("consecutive_healthy_cycles") or 0),
            "soak_required": int(soak.get("required_healthy_cycles") or 24),
            "paper_ready": int(readiness.get("total_paper_ready_candidates") or 0),
            "positive_ev": _latest_int(history, "positive_ev_rows"),
            "fresh_candidates": int(
                (refresh.get("decision_refresh") or {}).get("fresh_ranked_candidates") or 0
            ),
            "next_refresh": "15-minute service cadence",
        },
        "safety": refresh.get("safety")
        or {
            "paper_order_creation_enabled": False,
            "live_execution_enabled": False,
            "autopilot_enabled": False,
        },
        "stages": _stage_rows(refresh),
        "blockers": blocker_rows,
        "history": _history_rows(history),
        "candidates": [_candidate_row(row) for row in candidates if isinstance(row, dict)],
        "empty_state": _empty_state(refresh, candidates, source_state),
        "cloud_authority": cloud,
        "changes": changes,
        "lifecycle": lifecycle,
        "blocker_intelligence": blocker_intelligence,
        "scorecard": scorecard,
        "incidents": incidents,
        "reports": (refresh.get("control_plane") or {}),
        "roadmap": build_roadmap_status(),
        "category_census": _category_census_view(category_census),
        "paper_throughput": _paper_throughput_view(paper_throughput),
    }


def _category_census_view(verification: dict[str, Any]) -> dict[str, Any]:
    payload = verification.get("payload") if verification.get("verified") else {}
    categories = payload.get("categories") if isinstance(payload, dict) else []
    return {
        "state": "VERIFIED" if verification.get("verified") else "MISSING_OR_UNVERIFIED",
        "path": verification.get("path"),
        "generated_at": payload.get("generated_at") if isinstance(payload, dict) else None,
        "categories": categories if isinstance(categories, list) else [],
        "priority_blockers": (
            payload.get("priority_blockers", []) if isinstance(payload, dict) else []
        ),
    }


def _paper_throughput_view(verification: dict[str, Any]) -> dict[str, Any]:
    payload = verification.get("payload") if verification.get("verified") else {}
    return {
        "state": "VERIFIED" if verification.get("verified") else "MISSING_OR_UNVERIFIED",
        "path": verification.get("path"),
        "generated_at": payload.get("generated_at") if isinstance(payload, dict) else None,
        "summary": payload.get("summary", {}) if isinstance(payload, dict) else {},
        "categories": payload.get("categories", {}) if isinstance(payload, dict) else {},
        "live_category_progress": (
            payload.get("live_category_progress", {}) if isinstance(payload, dict) else {}
        ),
        "lineage_gaps": payload.get("lineage_gaps", []) if isinstance(payload, dict) else [],
        "pending_settlements": (
            payload.get("pending_settlements", []) if isinstance(payload, dict) else []
        ),
        "rejection_breakdown": (
            payload.get("rejection_breakdown", []) if isinstance(payload, dict) else []
        ),
        "next_actions": payload.get("next_actions", []) if isinstance(payload, dict) else [],
        "zero_trade_reasons": (
            payload.get("zero_trade_reasons", {}) if isinstance(payload, dict) else {}
        ),
    }


def _stage_rows(refresh: dict[str, Any]) -> list[dict[str, Any]]:
    telemetry = refresh.get("cycle_telemetry") or {}
    timings = telemetry.get("stages") if isinstance(telemetry, dict) else []
    timing_by_stage = {
        str(row.get("stage")): row for row in (timings or []) if isinstance(row, dict)
    }
    errors = [str(item) for item in refresh.get("errors") or []]
    definitions = (
        ("drain_websocket_stage", "Orderbook drain", refresh.get("websocket_drain")),
        ("drain_crypto_quotes", "Crypto quote drain", refresh.get("crypto_quote_drain")),
        ("parse_active_market_legs", "Market leg parsing", refresh.get("active_linking")),
        ("refresh_crypto_decisions", "Crypto decisions", refresh.get("decision_refresh")),
        ("refresh_weather_decisions", "Weather decisions", refresh.get("weather_gate")),
        ("write_candidate_manifest", "Candidate manifest", refresh.get("candidate_alignment")),
    )
    rows = []
    for key, label, evidence in definitions:
        timing = timing_by_stage.get(key, {})
        state = "COMPLETED" if evidence else "NO_SOURCE_DATA"
        if errors and key in {"drain_websocket_stage", "drain_crypto_quotes"}:
            state = "DEGRADED"
        rows.append(
            {
                "stage": key,
                "label": label,
                "state": state,
                "duration": (
                    f"{float(timing['duration_seconds']):.2f}s"
                    if timing.get("duration_seconds") is not None
                    else "not recorded"
                ),
            }
        )
    return rows


def _candidate_row(row: dict[str, Any]) -> dict[str, Any]:
    gates = [str(item) for item in row.get("blocking_gates") or []]
    if row.get("selection_tier") == "MISSING_SNAPSHOT_RECOVERY":
        lifecycle = "SNAPSHOT_NEEDED"
    elif not row.get("fresh"):
        lifecycle = "WARMING_OR_STALE"
    elif not row.get("positive_edge"):
        lifecycle = "RANKED_NO_POSITIVE_EDGE"
    elif gates:
        lifecycle = "BLOCKED_BY_GATES"
    elif row.get("executable"):
        lifecycle = "PAPER_READY_REVIEW"
    else:
        lifecycle = "RANKED"
    return {
        "ticker": row.get("ticker") or "unknown",
        "tier": row.get("selection_tier") or "unknown",
        "lifecycle": lifecycle,
        "fresh": bool(row.get("fresh")),
        "edge": row.get("estimated_edge"),
        "blockers": gates,
    }


def _history_rows(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "generated_at": row.get("generated_at") or "unknown",
            "healthy": bool(row.get("healthy")),
            "paper_ready": int(row.get("paper_ready_candidates") or 0),
            "positive_ev": int(row.get("positive_ev_rows") or 0),
            "fresh_candidates": int(row.get("fresh_ranked_candidates") or 0),
            "reset_reason": row.get("reset_reason") or "none",
        }
        for row in reversed(history[-12:])
    ]


def _blocker_rows(current: Counter[str], previous: Counter[str] | None) -> list[dict[str, Any]]:
    rows = []
    prior = previous or Counter()
    for blocker in sorted(set(current) | set(prior)):
        now, before = current[blocker], prior[blocker]
        rows.append(
            {
                "blocker": blocker,
                "current": now,
                "previous": before if previous is not None else "not recorded",
                "trend": (
                    "NOT_RECORDED"
                    if previous is None
                    else "IMPROVING"
                    if now < before
                    else "WORSE"
                    if now > before
                    else "UNCHANGED"
                ),
            }
        )
    return rows


def _history_blockers(row: dict[str, Any]) -> Counter[str] | None:
    if "blocker_counts" not in row:
        return None
    raw = row.get("blocker_counts") or {}
    if not isinstance(raw, dict):
        return None
    return Counter({str(key): int(value or 0) for key, value in raw.items()})


def _empty_state(
    refresh: dict[str, Any], candidates: list[Any], source_state: str
) -> dict[str, str] | None:
    if source_state in {"NO_SOURCE_DATA", "INVALID_SOURCE"}:
        return {"code": source_state, "message": "No valid GH-2 cycle artifact is available."}
    if not candidates:
        linking = refresh.get("active_linking") or {}
        active = int(linking.get("crypto_candidates") or 0) + int(
            linking.get("weather_candidates") or 0
        )
        code = "NO_ELIGIBLE_ROWS" if active else "VALID_ZERO_ACTIVE_MARKETS"
        return {
            "code": code,
            "message": "The cycle ran, but no candidate rows reached the watch manifest.",
        }
    return None


def _source_state(refresh: dict[str, Any], age_seconds: int | None) -> str:
    if not refresh:
        return "NO_SOURCE_DATA"
    if not refresh.get("generated_at"):
        return "INVALID_SOURCE"
    if age_seconds is not None and age_seconds > STALE_AFTER_SECONDS:
        return "STALE_INPUT"
    return "CURRENT"


def _source_meaning(state: str) -> str:
    return {
        "CURRENT": "A valid, current cycle artifact was loaded.",
        "STALE_INPUT": "A valid artifact was loaded, but it is older than the 30-minute threshold.",
        "NO_SOURCE_DATA": "The GH-2 cycle artifact does not exist in this workspace.",
        "INVALID_SOURCE": "The artifact exists but lacks a valid generated timestamp.",
    }[state]


def _latest_int(history: list[dict[str, Any]], key: str) -> int:
    return int(history[-1].get(key) or 0) if history else 0


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _age_label(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows
