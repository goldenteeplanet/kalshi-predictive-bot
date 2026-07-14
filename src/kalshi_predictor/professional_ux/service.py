from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalshi_predictor.advanced_risk.reports import advanced_risk_card
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import (
    Forecast,
    MarketRanking,
    MarketSnapshot,
    PaperOrder,
)
from kalshi_predictor.institutional_dashboard.service import build_dashboard_snapshot
from kalshi_predictor.live_readiness.reports import live_readiness_dashboard_card
from kalshi_predictor.professional_ux.contracts import (
    BOUNDARY_ASSERTIONS,
    COMMAND_ITEMS,
    DECISION_INCOMPLETE,
    DEFAULT_PRESENTATION_PREFERENCES,
    NAV_ITEMS,
    PHASE_3X_VERSION,
    PROHIBITED_TERMS,
    ROUTE_INVENTORY,
    STATUS_GRAMMAR,
)
from kalshi_predictor.system_certification.reports import system_certification_card
from kalshi_predictor.utils.time import parse_datetime, utc_now
from kalshi_predictor.workspace_guard import build_workspace_consistency_guard

SHELL_STATUS_SNAPSHOT_SCHEMA_VERSION = "shell-status-snapshot-v1"
DEFAULT_SHELL_STATUS_SNAPSHOT_PATH = Path("reports/ui/shell_status_snapshot.json")
PHASE3AK_TOP_STRIP_STATUS_PATH = Path("reports/phase_3ak/top_strip_status.json")
SHELL_STATUS_STALE_AFTER_SECONDS = 30 * 60


def build_default_shell_context(settings: Settings | None = None) -> dict[str, Any]:
    resolved = settings or get_settings()
    return {
        "environment": resolved.kalshi_env.upper(),
        "execution_mode": _execution_mode(resolved),
        "account_scope": "local",
        "workspace_guard": build_workspace_consistency_guard(settings=resolved)["ui_badge"],
        "paper_runtime": _explicit_status(
            "UNINITIALIZED",
            "Status source has not been generated for this route yet.",
        ),
        "system_status": _explicit_status(
            "UNINITIALIZED",
            "Status source has not been generated for this route yet.",
        ),
        "market_freshness": _explicit_status(
            "UNINITIALIZED",
            "Status source has not been generated for this route yet.",
        ),
        "snapshot_as_of": "unknown",
        "snapshot_as_of_label": "unknown",
        "timezone": resolved.phase_3x_timezone,
        "phase_3w": {
            "status": "SYSTEM_INCOMPLETE",
            "label": "3W INCOMPLETE",
            "href": "/system-certification",
        },
        "phase_3v": {
            "status": "UNKNOWN",
            "label": "3V UNKNOWN",
            "href": "/live-readiness",
        },
        "phase_3x": {
            "decision": DECISION_INCOMPLETE,
            "mode": resolved.phase_3x_mode,
        },
        "nav_items": list(NAV_ITEMS),
        "command_items": list(COMMAND_ITEMS),
        "theme": resolved.phase_3x_theme,
        "density": resolved.phase_3x_density,
        "command_palette_enabled": resolved.phase_3x_command_palette_enabled,
    }


def build_shell_status_context(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Build a bounded shell context for the top status strip.

    This intentionally avoids the full Phase 3T dashboard snapshot. It is safe
    to run as a refresh job, while request handlers should load the written
    JSON snapshot instead of calling this directly.
    """

    resolved = settings or get_settings()
    context = build_default_shell_context(resolved)
    generated_at = utc_now()
    latest_market_snapshot_at = session.scalar(select(func.max(MarketSnapshot.captured_at)))
    paper_runtime = _paper_runtime_status({"status": "ok"}, resolved)
    context.update(
        {
            "paper_runtime": paper_runtime,
            "system_status": paper_runtime,
            "market_freshness": _market_freshness_from_timestamp(
                latest_market_snapshot_at,
                resolved,
            ),
            "snapshot_as_of": generated_at.isoformat(),
            "snapshot_as_of_label": _compact_timestamp_label(generated_at.isoformat()),
            "phase_3v": {
                "status": "NOT_READY",
                "label": "3V NOT READY",
                "href": "/live-readiness",
            },
            "shell_status_snapshot": {
                "schema_version": SHELL_STATUS_SNAPSHOT_SCHEMA_VERSION,
                "generated_at": generated_at.isoformat(),
                "refresh_mode": "BOUNDED_NON_BLOCKING_UI_SOURCE",
                "full_dashboard_snapshot_used": False,
            },
        }
    )
    _apply_phase3ak_top_strip_context(context)
    return context


def build_shell_status_snapshot(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    generated_at = utc_now().isoformat()
    context = build_shell_status_context(session, settings=settings)
    return {
        "schema_version": SHELL_STATUS_SNAPSHOT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "mode": "PAPER_ONLY_UI_STATUS_SNAPSHOT",
        "live_or_demo_execution": False,
        "full_dashboard_snapshot_used": False,
        "context": context,
    }


def write_shell_status_snapshot(
    session: Session,
    *,
    output_path: Path | str = DEFAULT_SHELL_STATUS_SNAPSHOT_PATH,
    settings: Settings | None = None,
) -> dict[str, Any]:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_shell_status_snapshot(session, settings=settings)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)
    return {"path": str(path), "payload": payload}


def load_shell_status_context(
    *,
    snapshot_path: Path | str = DEFAULT_SHELL_STATUS_SNAPSHOT_PATH,
    settings: Settings | None = None,
    stale_after_seconds: int = SHELL_STATUS_STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    path = Path(snapshot_path)
    if not path.exists():
        return _shell_status_snapshot_fallback(
            resolved,
            label="SNAPSHOT MISSING",
            description=(
                "No UI shell-status snapshot exists yet. Run "
                "`kalshi-bot ui-shell-status-refresh` to populate it."
            ),
            path=path,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _shell_status_snapshot_fallback(
            resolved,
            label="SNAPSHOT ERROR",
            description=f"Could not read UI shell-status snapshot: {exc}",
            path=path,
        )
    if payload.get("schema_version") != SHELL_STATUS_SNAPSHOT_SCHEMA_VERSION:
        return _shell_status_snapshot_fallback(
            resolved,
            label="SNAPSHOT INVALID",
            description="UI shell-status snapshot schema is not recognized.",
            path=path,
        )
    context = payload.get("context")
    if not isinstance(context, dict):
        return _shell_status_snapshot_fallback(
            resolved,
            label="SNAPSHOT INVALID",
            description="UI shell-status snapshot has no context object.",
            path=path,
        )
    loaded = dict(context)
    _refresh_loaded_shell_status_labels(
        loaded,
        payload=payload,
        snapshot_path=path,
        stale_after_seconds=stale_after_seconds,
        market_fresh_after_seconds=resolved.phase_3t_fresh_after_seconds,
    )
    _apply_phase3ak_top_strip_context(loaded)
    return loaded


def build_shell_context(
    session: Session,
    *,
    settings: Settings | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    phase_3t_snapshot = snapshot or build_dashboard_snapshot(session, settings=resolved)
    sources = {
        row["source_id"]: row for row in phase_3t_snapshot.get("source_statuses", [])
    }
    phase_3w = phase_3t_snapshot["panels"]["system_certification"]
    phase_3v = phase_3t_snapshot["panels"]["live_readiness"]
    database = phase_3t_snapshot["panels"]["system_health"]["database"]
    freshness = _source_status_badge(sources.get("market_state"))
    paper_runtime = _paper_runtime_status(database, resolved)
    phase_3v_state = _live_readiness_state(phase_3v)
    workspace_guard = build_workspace_consistency_guard(settings=resolved)
    generated_at = phase_3t_snapshot["generated_at"]
    return {
        "environment": phase_3t_snapshot["runtime_context"]["environment"],
        "execution_mode": _execution_mode(resolved),
        "account_scope": "local",
        "workspace_guard": workspace_guard["ui_badge"],
        "paper_runtime": paper_runtime,
        "system_status": paper_runtime,
        "market_freshness": freshness,
        "snapshot_as_of": generated_at,
        "snapshot_as_of_label": _compact_timestamp_label(generated_at),
        "timezone": phase_3t_snapshot["runtime_context"]["timezone"],
        "database_fingerprint": phase_3t_snapshot["runtime_context"]["database_fingerprint"],
        "git_commit": phase_3t_snapshot["runtime_context"]["git_commit"],
        "phase_3w": {
            "status": phase_3w["overall_status"],
            "label": f"3W {_short_certification_status(phase_3w['overall_status'])}",
            "href": "/system-certification",
        },
        "phase_3v": {
            "status": phase_3v_state,
            "label": f"3V {_short_certification_status(phase_3v_state)}",
            "href": "/live-readiness",
        },
        "phase_3x": phase_3x_status_card(session, settings=resolved),
        "nav_items": list(NAV_ITEMS),
        "command_items": list(COMMAND_ITEMS),
        "theme": resolved.phase_3x_theme,
        "density": resolved.phase_3x_density,
        "command_palette_enabled": resolved.phase_3x_command_palette_enabled,
    }


def phase_3x_status_card(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    phase_3w = system_certification_card(session, settings=resolved)
    phase_3v = live_readiness_dashboard_card(session, settings=resolved)
    blockers = []
    if phase_3w["overall_status"] != "SYSTEM_PASS":
        blockers.append("Phase 3W is not SYSTEM_PASS for production rollout.")
    if phase_3v["decision"] not in {"GO", "CONDITIONAL_GO"}:
        blockers.append("Phase 3V readiness is not live-capital approved.")
    blockers.extend(
        [
            "Accessibility manual evidence is not complete.",
            "Visual regression baselines are not complete.",
            "Performance budget evidence is not complete.",
            "Rollback rehearsal evidence is not complete.",
        ]
    )
    return {
        "phase": "3X",
        "version": PHASE_3X_VERSION,
        "enabled": resolved.phase_3x_professional_ux_enabled,
        "mode": resolved.phase_3x_mode,
        "decision": DECISION_INCOMPLETE,
        "release_stage": "audit_preview",
        "route_count": len(ROUTE_INVENTORY),
        "component_count": len(component_catalog()),
        "phase_3w_status": phase_3w["overall_status"],
        "phase_3v_decision": phase_3v["decision"],
        "boundary_passed": all(item["passed"] for item in BOUNDARY_ASSERTIONS),
        "live_trading_authorized": False,
        "blockers": blockers,
        "next_action": "Use Phase 3X as audit/preview until Phase 3W is SYSTEM_PASS.",
    }


def build_today_workspace(
    session: Session,
    *,
    settings: Settings | None = None,
    dashboard_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    context = dashboard_context or {}
    opportunities = context.get("opportunities") or []
    advanced_risk = context.get("advanced_risk_status") or advanced_risk_card(
        session,
        settings=resolved,
    )
    phase_3w = context.get("system_certification_status") or system_certification_card(
        session,
        settings=resolved,
    )
    phase_3v = context.get("live_readiness_status") or live_readiness_dashboard_card(
        session,
        settings=resolved,
    )
    candidate_count = session.execute(select(func.count(MarketRanking.id))).scalar_one()
    forecast_count = session.execute(select(func.count(Forecast.id))).scalar_one()
    paper_trade_count = session.execute(select(func.count(PaperOrder.id))).scalar_one()
    warnings = _today_warnings(phase_3w, phase_3v, advanced_risk)
    return {
        "headline": _today_headline(opportunities),
        "decision_state": "ranked" if opportunities else "no_trade",
        "decision_label": "Ranked opportunities" if opportunities else "No trade",
        "candidate_count": candidate_count,
        "forecast_count": forecast_count,
        "paper_trade_count": paper_trade_count,
        "blocked_count": int(advanced_risk.get("block_count", 0) or 0),
        "reduced_count": int(advanced_risk.get("reduce_count", 0) or 0),
        "warnings": warnings,
        "phase_3w_status": phase_3w["overall_status"],
        "phase_3v_decision": phase_3v["decision"],
        "no_trade_message": (
            "No opportunity currently clears the available ranking, liquidity, "
            "sizing, and risk evidence. This is a valid state, not an error."
        ),
    }


def build_ux_audit(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    status = phase_3x_status_card(session, settings=resolved)
    shell = build_shell_context(session, settings=resolved)
    return {
        "schema_version": PHASE_3X_VERSION,
        "generated_at": utc_now().isoformat(),
        "release_decision": status["decision"],
        "phase_3w_prerequisite": {
            "status": status["phase_3w_status"],
            "production_rollout_allowed": False,
            "note": "SYSTEM_PASS is required before production rollout claims.",
        },
        "shell_context": shell,
        "routes": list(ROUTE_INVENTORY),
        "components": component_catalog(),
        "status_grammar": STATUS_GRAMMAR,
        "boundary_assertions": list(BOUNDARY_ASSERTIONS),
        "content_policy": {
            "prohibited_terms": list(PROHIBITED_TERMS),
            "ai_copy_authority": "structured evidence remains authoritative",
        },
        "preferences": DEFAULT_PRESENTATION_PREFERENCES,
        "open_findings": status["blockers"],
    }


def write_phase_3x_artifacts(
    session: Session,
    *,
    output_dir: Path,
    settings: Settings | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    audit = build_ux_audit(session, settings=settings)
    artifacts = {
        "UI_UX_AUDIT.md": _render_audit_markdown(audit),
        "COMPONENT_CATALOG.md": _render_component_catalog(audit),
        "PHASE_3X_REPORT.md": _render_report_markdown(audit),
        "phase_3x_audit.json": json.dumps(audit, indent=2, sort_keys=True),
    }
    written = []
    for name, content in artifacts.items():
        path = output_dir / name
        path.write_text(content + ("\n" if not content.endswith("\n") else ""), encoding="utf-8")
        written.append(str(path))
    return {
        "decision": audit["release_decision"],
        "output_dir": str(output_dir),
        "artifacts": written,
        "audit": audit,
    }


def component_catalog() -> list[dict[str, str]]:
    return [
        {
            "name": "Professional shell",
            "purpose": "Persistent environment, mode, freshness, 3W, and 3V context.",
            "states": "normal, degraded, incomplete, mobile",
            "authority": "Presentation only",
        },
        {
            "name": "Command palette",
            "purpose": "Keyboard route navigation without domain mutations.",
            "states": "open, filtered, empty",
            "authority": "Navigation only",
        },
        {
            "name": "Status pill",
            "purpose": "Icon plus text status, never color-only.",
            "states": ", ".join(sorted(STATUS_GRAMMAR)),
            "authority": "Presentation mapping",
        },
        {
            "name": "Probability pair",
            "purpose": "Keep market probability and model probability visually distinct.",
            "states": "available, unknown, stale",
            "authority": "Existing ranking/forecast values",
        },
        {
            "name": "Decision waterfall",
            "purpose": "Show Phase 3S to 3M to 3N gate outcomes in order.",
            "states": "proceed, skip, allow, reduce, block, unknown",
            "authority": "Existing phase outputs only",
        },
        {
            "name": "State panel",
            "purpose": "Truthful loading, no-trade, stale, partial, blocked, and error states.",
            "states": "empty-valid, no-trade, unavailable, blocked, expired, error",
            "authority": "Presentation only",
        },
    ]


def _render_audit_markdown(audit: dict[str, Any]) -> str:
    lines = [
        "# Phase 3X UI/UX Audit",
        "",
        f"- Generated at: `{audit['generated_at']}`",
        f"- Release decision: `{audit['release_decision']}`",
        f"- Phase 3W prerequisite: `{audit['phase_3w_prerequisite']['status']}`",
        "- Live trading authorized: `False`",
        "",
        "## Route Inventory",
        "",
        (
            "| Route | Primary user job | Source authority | Action authority | "
            "Disposition | Risk |"
        ),
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in audit["routes"]:
        lines.append(
            "| {route} | {primary_job} | {source_authority} | {action_authority} | "
            "{disposition} | {risk} |".format(**row)
        )
    lines.extend(["", "## Open Findings", ""])
    lines.extend(f"- {item}" for item in audit["open_findings"])
    return "\n".join(lines)


def _render_component_catalog(audit: dict[str, Any]) -> str:
    lines = [
        "# Phase 3X Component Catalog",
        "",
        "| Component | Purpose | States | Authority |",
        "| --- | --- | --- | --- |",
    ]
    for row in audit["components"]:
        lines.append(
            f"| {row['name']} | {row['purpose']} | {row['states']} | {row['authority']} |"
        )
    return "\n".join(lines)


def _render_report_markdown(audit: dict[str, Any]) -> str:
    lines = [
        "# Phase 3X Professional UX/UI Report",
        "",
        "THIS REPORT DOES NOT AUTHORIZE LIVE TRADING.",
        "",
        f"- Decision: `{audit['release_decision']}`",
        f"- Phase 3W status: `{audit['phase_3w_prerequisite']['status']}`",
        f"- Routes inventoried: `{len(audit['routes'])}`",
        f"- Components cataloged: `{len(audit['components'])}`",
        "- Boundary result: presentation-only audit/preview",
        "",
        "## Boundary Assertions",
        "",
    ]
    for row in audit["boundary_assertions"]:
        lines.append(f"- `{row['name']}`: {row['detail']}")
    lines.extend(["", "## Final Decision", "", "`INCOMPLETE`"])
    return "\n".join(lines)


def _execution_mode(settings: Settings) -> str:
    if settings.execution_enabled and not settings.execution_dry_run:
        return "LIVE-CAPABLE BLOCKED BY READINESS"
    if settings.execution_enabled and settings.execution_dry_run:
        return "DEMO DRY RUN"
    return "PAPER / READ-ONLY"


def _freshness_status(
    latest_snapshot: MarketSnapshot | None,
    settings: Settings,
) -> dict[str, Any]:
    if latest_snapshot is None:
        status = _status("unknown")
        status["age_label"] = "no snapshots"
        status["as_of"] = "unknown"
        return status
    captured_at = _aware(latest_snapshot.captured_at)
    age_seconds = max(0, int((utc_now() - captured_at).total_seconds()))
    kind = "fresh" if age_seconds <= settings.phase_3t_fresh_after_seconds else "stale"
    status = _status(kind)
    status["age_seconds"] = age_seconds
    status["age_label"] = _age_label(age_seconds)
    status["as_of"] = captured_at.isoformat()
    return status


def _market_freshness_from_timestamp(value: Any, settings: Settings) -> dict[str, Any]:
    captured_at = parse_datetime(value)
    if captured_at is None:
        status = _status("unknown")
        status["age_label"] = "no snapshots"
        status["as_of"] = "unknown"
        return status
    age_seconds = max(0, int((utc_now() - _aware(captured_at)).total_seconds()))
    kind = "fresh" if age_seconds <= settings.phase_3t_fresh_after_seconds else "stale"
    status = _status(kind)
    status["age_seconds"] = age_seconds
    status["age_label"] = _age_label(age_seconds)
    status["as_of"] = _aware(captured_at).isoformat()
    return status


def _snapshot_as_of(latest_snapshot: MarketSnapshot | None) -> str:
    if latest_snapshot is None:
        return "unknown"
    return _aware(latest_snapshot.captured_at).isoformat()


def _compact_timestamp_label(value: Any) -> str:
    parsed = parse_datetime(value)
    if parsed is None:
        return str(value or "unknown")
    return _aware(parsed).strftime("%b %d %H:%M")


def _shell_status_snapshot_fallback(
    settings: Settings,
    *,
    label: str,
    description: str,
    path: Path,
) -> dict[str, Any]:
    context = build_default_shell_context(settings)
    status = _explicit_status(label, description)
    context["paper_runtime"] = status
    context["system_status"] = status
    context["market_freshness"] = status
    context["snapshot_as_of"] = "unknown"
    context["snapshot_as_of_label"] = "snapshot missing"
    context["phase_3v"] = {
        "status": "NOT_READY",
        "label": "3V NOT READY",
        "href": "/live-readiness",
    }
    context["shell_status_snapshot"] = {
        "schema_version": SHELL_STATUS_SNAPSHOT_SCHEMA_VERSION,
        "path": str(path),
        "freshness_status": label,
        "description": description,
    }
    _apply_phase3ak_top_strip_context(context)
    return context


def _refresh_loaded_shell_status_labels(
    context: dict[str, Any],
    *,
    payload: dict[str, Any],
    snapshot_path: Path,
    stale_after_seconds: int,
    market_fresh_after_seconds: int,
) -> None:
    generated_at = parse_datetime(payload.get("generated_at"))
    age_seconds = None
    freshness_status = "UNKNOWN"
    if generated_at is not None:
        age_seconds = max(0, int((utc_now() - _aware(generated_at)).total_seconds()))
        freshness_status = "STALE" if age_seconds > stale_after_seconds else "FRESH"
        context["snapshot_as_of"] = _aware(generated_at).isoformat()
        context["snapshot_as_of_label"] = _age_label(age_seconds)
    else:
        context["snapshot_as_of"] = str(payload.get("generated_at") or "unknown")
        context["snapshot_as_of_label"] = "unknown"
    market_freshness = context.get("market_freshness")
    if isinstance(market_freshness, dict):
        market_as_of = parse_datetime(market_freshness.get("as_of"))
        if market_as_of is not None:
            market_age_seconds = max(0, int((utc_now() - _aware(market_as_of)).total_seconds()))
            kind = "fresh" if market_age_seconds <= market_fresh_after_seconds else "stale"
            status = _status(kind)
            status.update(
                {
                    "age_seconds": market_age_seconds,
                    "age_label": _age_label(market_age_seconds),
                    "as_of": _aware(market_as_of).isoformat(),
                }
            )
            context["market_freshness"] = status
    context["shell_status_snapshot"] = {
        "schema_version": payload.get("schema_version"),
        "path": str(snapshot_path),
        "generated_at": payload.get("generated_at"),
        "age_seconds": age_seconds,
        "age_label": _age_label(age_seconds) if age_seconds is not None else "unknown",
        "freshness_status": freshness_status,
        "full_dashboard_snapshot_used": bool(payload.get("full_dashboard_snapshot_used")),
    }


def _apply_phase3ak_top_strip_context(
    context: dict[str, Any],
    *,
    path: Path = PHASE3AK_TOP_STRIP_STATUS_PATH,
) -> None:
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    market_state = str(payload.get("market_data_state") or payload.get("state") or "").upper()
    if market_state == "FRESH":
        status = _status("fresh")
    elif market_state in {"STALE", "BLOCKED_BY_ACTIVE_WRITER"}:
        status = _status("stale")
    elif market_state == "MISSING":
        status = _status("incomplete")
    else:
        status = _status("unknown")
    watermark = parse_datetime(payload.get("data_watermark"))
    age_seconds = None
    if watermark is not None:
        age_seconds = max(0, int((utc_now() - _aware(watermark)).total_seconds()))
    elif payload.get("staleness_age_minutes") is not None:
        try:
            age_seconds = int(float(payload["staleness_age_minutes"]) * 60)
        except (TypeError, ValueError):
            age_seconds = None
    if age_seconds is not None:
        status["age_seconds"] = age_seconds
        status["age_label"] = _age_label(age_seconds)
    else:
        status["age_label"] = "unknown"
    status["as_of"] = _aware(watermark).isoformat() if watermark is not None else "unknown"
    if payload.get("blocked_reason"):
        status["description"] = (
            f"Market data is {market_state or 'UNKNOWN'}; "
            f"blocked by {payload.get('active_writer_name') or 'active writer'} "
            f"pid {payload.get('active_writer_pid') or 'unknown'}."
        )
    else:
        status["description"] = f"Market data state from Phase 3AK: {market_state or 'UNKNOWN'}."
    context["market_freshness"] = status
    context["phase3ak_top_strip_status"] = payload


def _source_status_badge(source: dict[str, Any] | None) -> dict[str, Any]:
    if not source:
        status = _explicit_status("UNINITIALIZED", "No source-status record is available.")
        status["age_label"] = "no source"
        status["as_of"] = "unknown"
        return status
    freshness = str(source.get("freshness_status") or "UNKNOWN").upper()
    kind = {
        "FRESH": "fresh",
        "AGING": "degraded",
        "STALE": "stale",
        "UNKNOWN": "unknown",
        "NOT_APPLICABLE": "incomplete",
    }.get(freshness, "unknown")
    status = _status(kind)
    watermark = parse_datetime(source.get("data_watermark") or source.get("latest_at"))
    if watermark is None:
        lifecycle = str(source.get("lifecycle_state") or "NO_WATERMARK").upper()
        status["label"] = lifecycle
        status["icon"] = "SRC"
        status["description"] = (
            f"{source.get('source_id') or 'source'} has no data watermark. "
            f"Lifecycle state: {lifecycle}."
        )
        status["age_label"] = lifecycle.lower()
        status["as_of"] = "unknown"
        return status
    age_seconds = max(0, int((utc_now() - _aware(watermark)).total_seconds()))
    status["age_seconds"] = age_seconds
    status["age_label"] = _age_label(age_seconds)
    status["as_of"] = _aware(watermark).isoformat()
    return status


def _live_readiness_state(phase_3v: dict[str, Any]) -> str:
    decision = str(phase_3v.get("decision") or "").upper()
    certificate = str(phase_3v.get("certificate") or "none").lower()
    if decision == "GO" and certificate not in {"", "none", "n/a"}:
        return "READY"
    if decision in {"GO", "CONDITIONAL_GO"}:
        return "PENDING_APPROVAL"
    return "NOT_READY"


def _paper_runtime_status(database: dict[str, Any], settings: Settings) -> dict[str, str]:
    kind = _paper_runtime_status_kind(database, settings)
    status = _status(kind)
    status["description"] = _paper_runtime_description(kind, database, settings)
    return status


def _paper_runtime_status_kind(database: dict[str, Any], settings: Settings) -> str:
    db_status = str(database.get("status", "")).lower()
    if "blocked" in db_status or "malformed" in db_status or "error" in db_status:
        return "failed"
    if settings.kalshi_env.lower() in {"live", "prod", "production"}:
        return "blocked"
    if settings.execution_enabled and not settings.execution_dry_run:
        return "blocked"
    if (
        settings.execution_enabled
        or not settings.learning_block_demo_execution
        or not settings.learning_block_live_execution
    ):
        return "degraded"
    if "warning" in db_status or "degraded" in db_status or database.get("warning"):
        return "degraded"
    return "healthy"


def _paper_runtime_description(
    kind: str,
    database: dict[str, Any],
    settings: Settings,
) -> str:
    if kind == "healthy":
        return "Paper-only runtime is reachable and execution guards are blocking demo/live orders."
    if kind == "blocked":
        return (
            "Paper runtime is blocked because live-capable environment or execution settings "
            "are enabled."
        )
    if kind == "failed":
        return str(database.get("migration_message") or "Database health check failed.")
    if settings.execution_enabled:
        return "Demo execution is enabled; paper learning remains safe but not fully read-only."
    if not settings.learning_block_demo_execution or not settings.learning_block_live_execution:
        return "Learning execution block settings are not fully enabled."
    return str(database.get("warning") or "Paper runtime has warnings.")


def _status(kind: str) -> dict[str, str]:
    return {"code": kind, **STATUS_GRAMMAR.get(kind, STATUS_GRAMMAR["unknown"])}


def _explicit_status(label: str, description: str) -> dict[str, str]:
    status = _status("incomplete")
    status.update({"label": label, "icon": "SRC", "description": description})
    return status


def _today_warnings(
    phase_3w: dict[str, Any],
    phase_3v: dict[str, Any],
    advanced_risk: dict[str, Any],
) -> list[dict[str, str]]:
    warnings = []
    if phase_3w.get("overall_status") != "SYSTEM_PASS":
        warnings.append(
            {
                "severity": "incomplete",
                "title": "Production rollout blocked",
                "detail": "Phase 3W is not SYSTEM_PASS. Treat this as audit/preview.",
            }
        )
    if phase_3v.get("decision") not in {"GO", "CONDITIONAL_GO"}:
        warnings.append(
            {
                "severity": "blocked",
                "title": "Live-capital actions unavailable",
                "detail": "Phase 3V readiness is not approved for live-capital use.",
            }
        )
    block_count = int(advanced_risk.get("block_count", 0) or 0)
    if block_count:
        warnings.append(
            {
                "severity": "blocked",
                "title": "Risk blocks present",
                "detail": f"Phase 3N has blocked {block_count} candidate(s).",
            }
        )
    return warnings


def _today_headline(opportunities: list[Any]) -> str:
    if opportunities:
        return "Review ranked opportunities with full gate context before any action."
    return "No trade is currently the truthful recommendation."


def _short_certification_status(value: str) -> str:
    return value.removeprefix("SYSTEM_").replace("_", " ")


def _age_label(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    return f"{hours}h"


def _aware(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=utc_now().tzinfo)
    return value
