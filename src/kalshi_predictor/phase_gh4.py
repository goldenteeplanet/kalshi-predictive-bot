from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.utils.time import utc_now

GH4_VERSION = "GH-4.0"
GH4_APPROVAL_TOKEN = "I_APPROVE_GH4_PAPER_ORDER_CREATION"
DEFAULT_GH2_REPORT_PATH = Path("reports/phase_gh2/gh2_active_candidate_refresh.json")
DEFAULT_GH2_HISTORY_PATH = Path("reports/phase_gh2/gh2_paper_only_soak_history.jsonl")
DEFAULT_GH1_STATUS_PATH = Path("reports/phase_gh1/watch/status.json")
DEFAULT_GH2_SCHEDULER_STATUS_PATH = Path("reports/phase_gh2/gh2_scheduler_status.json")
PAPER_ONLY_SAFETY = "SIMULATED_PAPER_ORDERS_ONLY_EXCHANGE_EXECUTION_DISABLED"


@dataclass(frozen=True)
class GH4Artifacts:
    output_dir: Path
    json_path: Path
    markdown_path: Path


def build_source_reconnect_health(
    *,
    gh2_payload: dict[str, Any],
    gh1_payload: dict[str, Any],
    now: datetime | None = None,
    gh1_stale_minutes: int = 5,
    decision_stale_minutes: int = 35,
) -> dict[str, Any]:
    resolved_now = _aware(now or utc_now())
    gh1_age = _age_minutes(gh1_payload.get("generated_at"), resolved_now)
    decision_age = _age_minutes(gh2_payload.get("generated_at"), resolved_now)
    gh1_state = str(gh1_payload.get("state") or "UNKNOWN")
    gh1_healthy_states = {
        "CONNECTING",
        "DISCOVERING_QUOTED_BOOKS",
        "STREAMING",
        "STREAM_CYCLE_COMPLETE",
    }
    gh1_healthy = (
        gh1_age is not None
        and gh1_age <= gh1_stale_minutes
        and gh1_state in gh1_healthy_states
        and int(gh1_payload.get("snapshots_seen") or 0) > 0
        and int(gh1_payload.get("consecutive_failures") or 0) < 3
    )

    crypto_drain = gh2_payload.get("crypto_quote_drain") or {}
    crypto_errors = list(crypto_drain.get("errors") or [])
    coinbase_healthy = (
        decision_age is not None
        and decision_age <= decision_stale_minutes
        and str(crypto_drain.get("status") or "") == "COMPLETE"
        and int(crypto_drain.get("prices_inserted") or 0) > 0
        and not crypto_errors
    )

    decision = gh2_payload.get("decision_refresh") or {}
    weather_features = list(decision.get("weather_features") or [])
    weather_forecasts = decision.get("weather_forecasts") or {}
    weather_healthy = (
        decision_age is not None
        and decision_age <= decision_stale_minutes
        and sum(int(row.get("features_inserted") or 0) for row in weather_features) > 0
        and int(weather_forecasts.get("forecasts_inserted") or 0) > 0
    )

    sources = [
        {
            "source": "Kalshi WebSocket",
            "status": "HEALTHY" if gh1_healthy else "NEEDS_ATTENTION",
            "status_kind": "healthy" if gh1_healthy else "blocked",
            "age_minutes": gh1_age,
            "detail": (
                f"{gh1_state}; {int(gh1_payload.get('snapshots_seen') or 0)} snapshots; "
                f"{int(gh1_payload.get('reconnect_count') or 0)} reconnects"
            ),
            "recovery": (
                "Automatic bounded reconnect is active."
                if gh1_healthy
                else "Reconnect service must recover a fresh streaming snapshot."
            ),
        },
        {
            "source": "Coinbase",
            "status": "HEALTHY" if coinbase_healthy else "NEEDS_ATTENTION",
            "status_kind": "healthy" if coinbase_healthy else "blocked",
            "age_minutes": decision_age,
            "detail": (
                f"{int(crypto_drain.get('prices_inserted') or 0)} prices imported; "
                f"{len(crypto_errors)} errors"
            ),
            "recovery": (
                "The next bounded stage retries transient fetch failures."
                if coinbase_healthy
                else "Retry the filesystem-only quote stage before the next writer drain."
            ),
        },
        {
            "source": "NOAA weather",
            "status": "HEALTHY" if weather_healthy else "NEEDS_ATTENTION",
            "status_kind": "healthy" if weather_healthy else "blocked",
            "age_minutes": decision_age,
            "detail": (
                f"{sum(int(row.get('features_inserted') or 0) for row in weather_features)} "
                f"features; {int(weather_forecasts.get('forecasts_inserted') or 0)} forecasts"
            ),
            "recovery": (
                "Bounded weather refresh is producing current decisions."
                if weather_healthy
                else "Retry NOAA ingest and rebuild the current weather decision window."
            ),
        },
    ]
    return {
        "status": "HEALTHY" if all(row["status"] == "HEALTHY" for row in sources) else "DEGRADED",
        "sources": sources,
        "gh1_report_age_minutes": gh1_age,
        "decision_report_age_minutes": decision_age,
    }


def build_gh3_soak_status(
    *,
    report_path: Path = DEFAULT_GH2_REPORT_PATH,
    history_path: Path = DEFAULT_GH2_HISTORY_PATH,
    gh1_status_path: Path = DEFAULT_GH1_STATUS_PATH,
    scheduler_status_path: Path = DEFAULT_GH2_SCHEDULER_STATUS_PATH,
    now: datetime | None = None,
    cadence_minutes: int = 15,
) -> dict[str, Any]:
    resolved_now = _aware(now or utc_now())
    payload = _read_json(report_path)
    history = _read_json_lines(history_path)
    gh1_payload = _read_json(gh1_status_path)
    scheduler = _read_json(scheduler_status_path)
    soak = payload.get("soak") or {}
    readiness = payload.get("paper_readiness") or {}
    completed = int(soak.get("consecutive_healthy_cycles") or 0)
    required = int(soak.get("required_healthy_cycles") or 24)
    remaining = max(0, required - completed)
    generated_at = _datetime(payload.get("generated_at"))
    scheduler_generated_at = _datetime(scheduler.get("generated_at"))
    next_run_base = scheduler_generated_at or generated_at
    next_run = (
        next_run_base + timedelta(minutes=cadence_minutes) if next_run_base else None
    )
    estimated_completion = resolved_now + timedelta(minutes=remaining * cadence_minutes)
    reconnect = build_source_reconnect_health(
        gh2_payload=payload,
        gh1_payload=gh1_payload,
        now=resolved_now,
    )
    report_age = _age_minutes(payload.get("generated_at"), resolved_now)
    report_fresh = report_age is not None and report_age <= max(35, cadence_minutes * 2 + 5)
    soak_complete = bool(soak.get("soak_complete"))
    current_paper_ready = int(readiness.get("total_paper_ready_candidates") or 0)
    weather_gate = payload.get("weather_gate") or {}
    weather_gate_summary = weather_gate.get("summary") or {}
    raw_soak_quality = payload.get("soak_quality") or {}
    soak_quality = {
        "passed": False,
        "checks": {},
        "failure_reasons": ["soak_quality_evidence_missing"],
        "observed": {},
        "required": {},
        **raw_soak_quality,
    }
    soak_quality["observed"] = dict(raw_soak_quality.get("observed") or {})
    soak_quality["required"] = dict(raw_soak_quality.get("required") or {})
    cycle_telemetry = payload.get("cycle_telemetry") or {}
    scheduler_state = str(scheduler.get("status") or "UNAVAILABLE")
    lock_wait_seconds = _nonnegative_float(
        scheduler.get("lock_wait_seconds") or cycle_telemetry.get("lock_wait_seconds")
    )
    writer_runtime_seconds = _nonnegative_float(
        scheduler.get("writer_runtime_seconds") or cycle_telemetry.get("runtime_seconds")
    )
    if scheduler_state == "RUNNING" and scheduler_generated_at is not None:
        writer_runtime_seconds = max(
            writer_runtime_seconds,
            (resolved_now - scheduler_generated_at).total_seconds(),
        )
    if not payload:
        status = "UNAVAILABLE"
        status_label = "Soak report unavailable"
        status_kind = "blocked"
    elif not report_fresh or not bool(soak.get("healthy_cycle")):
        status = "NEEDS_ATTENTION"
        status_label = "Soak needs attention"
        status_kind = "blocked"
    elif soak_complete:
        status = "COMPLETE"
        status_label = "Soak complete"
        status_kind = "healthy"
    else:
        status = "RUNNING"
        status_label = "Paper-only soak running"
        status_kind = "incomplete"
    latest_reset = next(
        (row for row in reversed(history) if not bool(row.get("healthy"))),
        None,
    )
    return {
        "status": status,
        "status_label": status_label,
        "status_kind": status_kind,
        "generated_at": payload.get("generated_at") or "n/a",
        "report_age_minutes": report_age,
        "completed_cycles": completed,
        "required_cycles": required,
        "remaining_cycles": remaining,
        "progress_percent": round((completed / max(required, 1)) * 100, 1),
        "eta_label": "complete"
        if remaining == 0
        else f"about {remaining * cadence_minutes / 60:.1f}h",
        "estimated_completion": _format_datetime(estimated_completion),
        "next_run": (
            "Running now"
            if scheduler_state in {"STAGING", "WAITING_FOR_WRITER", "RUNNING"}
            else _format_datetime(next_run)
        ),
        "scheduler_status": _enum_label(scheduler_state),
        "lock_wait_seconds": lock_wait_seconds,
        "writer_runtime_seconds": writer_runtime_seconds,
        "deferred_cycle_reason": _enum_label(
            scheduler.get("deferred_cycle_reason") or "NONE"
        ),
        "last_successful_completion": _format_datetime(
            _datetime(scheduler.get("last_successful_completion"))
            or (generated_at if bool(soak.get("healthy_cycle")) else None)
        ),
        "healthy_cycle": bool(soak.get("healthy_cycle")),
        "quality_gates_passed": bool(soak_quality.get("passed")),
        "soak_quality": soak_quality,
        "paper_ready_seen": bool(soak.get("paper_ready_seen_in_required_window")),
        "current_paper_ready_candidates": current_paper_ready,
        "positive_ev_rows": int(readiness.get("crypto_positive_ev_rows") or 0)
        + int(readiness.get("weather_positive_ev_rows") or 0),
        "fresh_ranked_candidates": int(
            (payload.get("decision_refresh") or {}).get("fresh_ranked_candidates") or 0
        ),
        "soak_complete": soak_complete,
        "latest_reset_reason": (
            str(latest_reset.get("reset_reason") or "Unhealthy cycle recorded.")
            if latest_reset
            else "No reset recorded in retained history."
        ),
        "history": list(reversed(history[-8:])),
        "reconnect": reconnect,
        "weather_gate": {
            "status": str(weather_gate.get("status") or "UNAVAILABLE"),
            "status_label": _enum_label(weather_gate.get("status") or "UNAVAILABLE"),
            "current_weather_links": int(weather_gate_summary.get("current_weather_links") or 0),
            "positive_raw_ev_rows": int(weather_gate_summary.get("positive_raw_ev_rows") or 0),
            "positive_executable_ev_rows": int(
                weather_gate_summary.get("positive_executable_ev_rows") or 0
            ),
            "paper_ready_rows": int(weather_gate_summary.get("paper_ready_rows") or 0),
            "first_hard_blocker": str(
                weather_gate_summary.get("first_hard_blocker") or "UNAVAILABLE"
            ),
            "candidate_rows": _weather_candidate_gate_rows(weather_gate),
        },
        "candidate_diagnostics": _candidate_diagnostic_rows(
            payload.get("candidate_diagnostics") or {}
        ),
        "paper_order_creation_enabled": False,
        "live_execution_enabled": False,
    }


def _weather_candidate_gate_rows(
    weather_gate: dict[str, Any],
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in weather_gate.get("weather_rows") or []:
        if not isinstance(raw, dict):
            continue
        ticker = str(raw.get("ticker") or "").strip()
        if not ticker:
            continue
        blocker = str(raw.get("first_blocker") or "UNKNOWN")
        rows.append(
            {
                "ticker": ticker,
                "detail_href": f"/opportunities/{ticker}",
                "raw_ev_label": _edge_cents_label(raw.get("raw_ev")),
                "executable_ev_label": _edge_cents_label(raw.get("executable_ev")),
                "liquidity_score": str(raw.get("liquidity_score") or "0"),
                "source_label": (
                    "Exact API source"
                    if bool(raw.get("source_identity_ready"))
                    else _enum_label(raw.get("kalshi_url_status") or "SOURCE_MISSING")
                ),
                "quote_age_label": (
                    f"{raw.get('snapshot_age_minutes')}m"
                    if raw.get("snapshot_age_minutes") is not None
                    else "n/a"
                ),
                "spread_label": str(raw.get("spread") or "n/a"),
                "ranking_label": (
                    "Current" if bool(raw.get("has_current_ranking")) else "Missing"
                ),
                "risk_label": (
                    "Ready"
                    if bool(raw.get("phase3s_proceed"))
                    and bool(raw.get("phase3m_nonzero_size"))
                    and bool(raw.get("phase3n_approved"))
                    else "Blocked"
                ),
                "book_label": (
                    "Executable"
                    if bool(raw.get("executable_book"))
                    else _enum_label(raw.get("no_book_reason") or "BOOK_MISSING")
                ),
                "failed_gate": blocker,
                "failed_gate_label": ", ".join(
                    _enum_label(item)
                    for item in (raw.get("failed_gates") or [blocker])
                ),
                "next_action": _weather_gate_next_action(blocker),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _candidate_diagnostic_rows(
    diagnostics: dict[str, Any],
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in diagnostics.get("rows") or []:
        if not isinstance(raw, dict) or not raw.get("ticker"):
            continue
        failed = [str(item) for item in raw.get("failed_gates") or []]
        rows.append(
            {
                **raw,
                "detail_href": f"/opportunities/{raw['ticker']}",
                "category_label": _enum_label(raw.get("category")),
                "source_label": "Ready" if raw.get("source_ready") else "Blocked",
                "book_label": "Ready" if raw.get("book_ready") else "Blocked",
                "quote_age_label": (
                    f"{raw.get('quote_age_minutes')}m"
                    if raw.get("quote_age_minutes") is not None
                    else "n/a"
                ),
                "raw_ev_label": _edge_cents_label(raw.get("raw_ev")),
                "executable_ev_label": _edge_cents_label(raw.get("executable_ev")),
                "spread_label": str(raw.get("spread") or "n/a"),
                "liquidity_label": str(raw.get("liquidity") or "n/a"),
                "ranking_label": "Ready" if raw.get("ranking_ready") else "Blocked",
                "risk_label": "Ready" if raw.get("risk_ready") else "Blocked",
                "failed_gate_label": ", ".join(_enum_label(item) for item in failed)
                or "Paper Ready",
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _edge_cents_label(value: Any) -> str:
    try:
        cents = Decimal(str(value)) * Decimal("100")
    except (InvalidOperation, TypeError, ValueError):
        return "n/a"
    rounded = cents.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{rounded}c"


def _enum_label(value: Any) -> str:
    return str(value or "UNKNOWN").replace("_", " ").title()


def _weather_gate_next_action(blocker: str) -> str:
    actions = {
        "MARKET_WINDOW_NOT_CURRENT": "Discover and link the next active market contract.",
        "MARKET_SOURCE_MISSING": "Refresh the exact Kalshi REST catalog source.",
        "MARKET_LINK_UNVERIFIED": "Verify the exact Kalshi operator URL mapping.",
        "SNAPSHOT_MISSING": "Keep the ticker subscribed until its first book arrives.",
        "SNAPSHOT_STALE": "Keep the ticker subscribed until a fresh Kalshi book arrives.",
        "WEATHER_SOURCE_MISSING": "Refresh the bounded NOAA source for this location.",
        "WEATHER_SOURCE_STALE": "Wait for a fresh bounded NOAA forecast.",
        "WEATHER_FEATURE_MISSING": "Build weather features for the active target window.",
        "WEATHER_FEATURE_STALE": "Rebuild stale weather features from fresh source data.",
        "FORECAST_MISSING": "Run the next bounded weather_v2 forecast refresh.",
        "RANKING_MISSING": "Run the next bounded weather_v2 ranking refresh.",
        "EV_NOT_POSITIVE": "Wait for model probability or market price to create positive raw EV.",
        "EXECUTABLE_EV_NOT_POSITIVE": (
            "Raw edge does not cover spread and configured execution costs."
        ),
        "LIQUIDITY_TOO_LOW": "Wait for sufficient visible depth at the configured limit.",
        "SPREAD_TOO_WIDE": "Wait for the book spread to enter the configured limit.",
        "BOOK_MISSING": "Keep the WebSocket subscription active until a usable book arrives.",
        "SETTLEMENT_TERMS_UNKNOWN": "Verify settlement terms before paper entry.",
        "RISK_NOT_ELIGIBLE": "Wait for the configured opportunity score and risk gates.",
        "PHASE_3M_ZERO_SIZE": "Produce a nonzero bounded paper position size.",
        "PHASE_3N_RISK_BLOCK": "Resolve the paper risk decision without weakening limits.",
        "PAPER_READY": "Eligible for the guarded GH-4 paper-only preflight.",
    }
    return actions.get(blocker, "Re-evaluate after the next guarded GH-2 refresh.")


def build_gh4_paper_activation_preflight(
    *,
    settings: Settings | None = None,
    gh2_report_path: Path = DEFAULT_GH2_REPORT_PATH,
    gh2_history_path: Path = DEFAULT_GH2_HISTORY_PATH,
    gh1_status_path: Path = DEFAULT_GH1_STATUS_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    soak_status = build_gh3_soak_status(
        report_path=gh2_report_path,
        history_path=gh2_history_path,
        gh1_status_path=gh1_status_path,
        now=now,
    )
    report = _read_json(gh2_report_path)
    safety = report.get("safety") or {}
    checks = [
        _check("gh3_soak_complete", soak_status["soak_complete"], "24 healthy cycles completed"),
        _check(
            "paper_ready_observed",
            soak_status["paper_ready_seen"],
            "At least one paper-ready candidate appeared in the required window",
        ),
        _check(
            "current_candidate_available",
            soak_status["current_paper_ready_candidates"] > 0,
            "A paper-ready candidate is available at activation time",
        ),
        _check(
            "latest_cycle_healthy", soak_status["healthy_cycle"], "Latest GH-2 cycle is healthy"
        ),
        _check(
            "soak_quality_gates",
            soak_status["quality_gates_passed"],
            "Current crypto/weather coverage and freshness minimums are satisfied",
        ),
        _check(
            "source_reconnect_health",
            soak_status["reconnect"]["status"] == "HEALTHY",
            "Kalshi, Coinbase, and weather source health is current",
        ),
        _check(
            "cycle_errors_clear", not list(report.get("errors") or []), "Latest cycle has no errors"
        ),
        _check(
            "soak_created_no_orders",
            int(safety.get("paper_orders_created") or 0) == 0,
            "The paper-only soak created zero orders",
        ),
        _check(
            "live_execution_disabled",
            not resolved_settings.execution_enabled,
            "Exchange execution remains disabled",
        ),
        _check(
            "autopilot_disabled",
            not resolved_settings.autopilot_enabled,
            "Autopilot remains disabled",
        ),
    ]
    ready = all(check["passed"] for check in checks)
    activation_requested = bool(resolved_settings.paper_order_creation_enabled)
    kill_switch = bool(resolved_settings.paper_order_kill_switch)
    if activation_requested and not ready:
        status = "UNSAFE_ACTIVATION_BLOCKED"
    elif activation_requested and not kill_switch:
        status = "OPERATOR_ACTIVATED_PAPER_ONLY"
    elif ready:
        status = "READY_FOR_OPERATOR_APPROVAL"
    else:
        status = "BLOCKED_GH3_OR_SAFETY_GATES"
    return {
        "phase": "GH-4",
        "phase_version": GH4_VERSION,
        "generated_at": (now or utc_now()).isoformat(),
        "status": status,
        "preflight_ready": ready,
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "checks": checks,
        "failed_checks": [check["id"] for check in checks if not check["passed"]],
        "soak": soak_status,
        "activation": {
            "paper_order_creation_enabled": activation_requested,
            "paper_order_kill_switch": kill_switch,
            "explicit_approval_token_required": True,
            "approval_token_value": GH4_APPROVAL_TOKEN,
        },
        "lifecycle_capabilities": [
            "paper decision generation",
            "simulated order creation",
            "immediate simulated fills",
            "position updates and limits",
            "realized and unrealized P&L",
            "settlement reconciliation",
            "paper-order kill switch",
        ],
        "safety": {
            "exchange_orders_enabled": False,
            "demo_orders_enabled": False,
            "autopilot_enabled": False,
            "preflight_writes_database": False,
            "preflight_creates_paper_orders": False,
        },
    }


def evaluate_paper_order_activation(
    *,
    settings: Settings,
    preflight: dict[str, Any],
    approval_token: str,
) -> dict[str, Any]:
    blockers: list[str] = []
    if not bool(preflight.get("preflight_ready")):
        blockers.append("GH4_PREFLIGHT_NOT_READY")
    if not settings.paper_order_creation_enabled:
        blockers.append("PAPER_ORDER_CREATION_DISABLED")
    if settings.paper_order_kill_switch:
        blockers.append("PAPER_ORDER_KILL_SWITCH_ACTIVE")
    if approval_token != GH4_APPROVAL_TOKEN:
        blockers.append("OPERATOR_APPROVAL_TOKEN_MISMATCH")
    if settings.execution_enabled:
        blockers.append("LIVE_EXECUTION_MUST_REMAIN_DISABLED")
    if settings.autopilot_enabled:
        blockers.append("AUTOPILOT_MUST_REMAIN_DISABLED")
    return {"allowed": not blockers, "blockers": blockers}


def write_gh4_paper_activation_preflight(
    *,
    output_dir: Path = Path("reports/phase_gh4"),
    settings: Settings | None = None,
    gh2_report_path: Path = DEFAULT_GH2_REPORT_PATH,
    gh2_history_path: Path = DEFAULT_GH2_HISTORY_PATH,
    gh1_status_path: Path = DEFAULT_GH1_STATUS_PATH,
) -> GH4Artifacts:
    payload = build_gh4_paper_activation_preflight(
        settings=settings,
        gh2_report_path=gh2_report_path,
        gh2_history_path=gh2_history_path,
        gh1_status_path=gh1_status_path,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "gh4_paper_activation_preflight.json"
    markdown_path = output_dir / "gh4_paper_activation_preflight.md"
    _write_json(json_path, payload)
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return GH4Artifacts(output_dir, json_path, markdown_path)


def _check(identifier: str, passed: bool, evidence: str) -> dict[str, Any]:
    return {"id": identifier, "passed": bool(passed), "evidence": evidence}


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# GH-4 Paper Order Activation Preflight",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- Generated: `{payload.get('generated_at')}`",
        f"- Preflight ready: `{payload.get('preflight_ready')}`",
        "- Exchange/live orders: `DISABLED`",
        "- Autopilot: `DISABLED`",
        "",
        "## Gates",
        "",
    ]
    for check in payload.get("checks") or []:
        mark = "PASS" if check.get("passed") else "BLOCKED"
        lines.append(f"- `{mark}` {check.get('id')}: {check.get('evidence')}")
    lines.extend(
        [
            "",
            "Paper-order creation requires the environment gate, released kill switch, "
            "and the exact operator approval token. This preflight performs no database writes.",
            "",
        ]
    )
    return "\n".join(lines)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
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
            payload = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _age_minutes(value: Any, now: datetime) -> float | None:
    parsed = _datetime(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() / 60)


def _nonnegative_float(value: Any) -> float:
    try:
        return max(float(value or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _format_datetime(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if value else "n/a"


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
