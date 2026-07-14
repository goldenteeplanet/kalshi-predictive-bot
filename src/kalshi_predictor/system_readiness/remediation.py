from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.advanced_risk.reports import (
    advanced_risk_card,
    generate_advanced_risk_report,
)
from kalshi_predictor.autopilot.guardrails import latest_snapshot_freshness
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import database_url_from_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.maintenance import (
    BLOCKED,
    READY,
    RECOVERY_INSTRUCTIONS,
    WARNING,
    database_health,
    database_status_card,
    generate_database_report,
)
from kalshi_predictor.data.repositories import get_recent_snapshots
from kalshi_predictor.data.schema import MarketSnapshot
from kalshi_predictor.forecasting.registry import run_forecast_models
from kalshi_predictor.forecasting.status import (
    generate_model_readiness_report,
    model_status_summary,
)
from kalshi_predictor.jobs.collect_once import collect_once
from kalshi_predictor.learning.diagnostics import generate_learning_diagnostics_report
from kalshi_predictor.learning.reports import generate_learning_report
from kalshi_predictor.live_readiness.reports import (
    generate_live_readiness_report,
    live_readiness_dashboard_card,
)
from kalshi_predictor.professional_ux.reports import generate_phase_3x_report
from kalshi_predictor.system_certification.reports import (
    generate_system_certification_report,
    system_certification_card,
)
from kalshi_predictor.utils.time import utc_now

DEFAULT_REPORT_PATH = Path("reports/system_readiness_remediation.md")


def system_remediation_card(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved = paper_only_settings(settings or get_settings())
    database = database_status_card(session, settings=resolved)
    phase_3w = system_certification_card(session, settings=resolved)
    phase_3v = live_readiness_dashboard_card(session, settings=resolved)
    latest_snapshot = session.scalar(
        select(MarketSnapshot).order_by(desc(MarketSnapshot.captured_at)).limit(1)
    )
    freshness = latest_snapshot_freshness(session)
    status = _card_status(database, phase_3w, freshness)
    report_path = DEFAULT_REPORT_PATH
    return {
        "status": status,
        "paper_only_confirmed": True,
        "database_status": database["status"],
        "phase_3w_status": phase_3w["overall_status"],
        "phase_3v_decision": phase_3v["decision"],
        "freshness_status": _freshness_label(freshness),
        "latest_snapshot_at": latest_snapshot.captured_at.isoformat()
        if latest_snapshot is not None
        else "none",
        "last_report": _last_generated(report_path),
        "report_href": "/reports/system_readiness_remediation.md",
        "next_commands": [
            "kalshi-bot db-health --output reports/database_report.md",
            "kalshi-bot system-remediate --refresh-data",
            "kalshi-bot system-remediation-report --output reports/system_readiness_remediation.md",
        ],
        "recommendations": _recommendations(
            database=database,
            phase_3w=phase_3w,
            phase_3v=phase_3v,
            freshness=freshness,
        ),
    }


def run_system_readiness_remediation(
    *,
    settings: Settings | None = None,
    output_path: Path = DEFAULT_REPORT_PATH,
    refresh_data: bool = False,
    collect_limit: int = 100,
    forecast_limit: int = 100,
    session_factory: Callable[[], Session] | None = None,
) -> dict[str, Any]:
    original_settings = settings or get_settings()
    safe_settings = paper_only_settings(original_settings)
    steps: list[dict[str, Any]] = []
    safety = _safety_summary(original_settings, safe_settings)
    db_health = database_health(settings=safe_settings)
    steps.append(
        _step(
            "database-health",
            db_health["status"],
            f"Database status is {db_health['status']}.",
            details=db_health["summary"],
        )
    )

    if _is_unsafe_original_environment(original_settings):
        steps.append(
            _step(
                "paper-only-safety",
                BLOCKED,
                "KALSHI_ENV is live/production; remediation will not run refresh jobs.",
            )
        )
        return _finish_result(
            steps=steps,
            safety=safety,
            db_health=db_health,
            output_path=output_path,
            summary={},
        )

    if db_health["status"] == BLOCKED:
        steps.append(
            _step(
                "database-recovery-required",
                BLOCKED,
                db_health.get("recovery") or RECOVERY_INSTRUCTIONS,
            )
        )
        return _finish_result(
            steps=steps,
            safety=safety,
            db_health=db_health,
            output_path=output_path,
            summary={},
        )

    engine = None
    if session_factory is None:
        engine = init_db(database_url_from_settings(safe_settings))
        session_factory = get_session_factory(engine)

    summary: dict[str, Any] = {}
    with session_factory() as session:
        try:
            if refresh_data:
                collection = collect_once(
                    status="open",
                    limit=collect_limit,
                    max_pages=1,
                    session=session,
                )
                steps.append(
                    _step(
                        "collect-once",
                        READY,
                        (
                            f"Captured {collection.snapshots_inserted} snapshots from "
                            f"{collection.markets_seen} markets."
                        ),
                    )
                )
            else:
                steps.append(
                    _step(
                        "collect-once",
                        WARNING,
                        "Skipped data refresh; rerun with --refresh-data to improve freshness.",
                    )
                )

            snapshots = get_recent_snapshots(session, limit=forecast_limit)
            if snapshots:
                forecast_summary = run_forecast_models(
                    session,
                    model_name="all",
                    snapshots=snapshots,
                )
                steps.append(
                    _step(
                        "forecast-all",
                        READY,
                        (
                            f"Scanned {forecast_summary.snapshots_scanned} snapshots, "
                            f"inserted {forecast_summary.forecasts_inserted} forecasts, "
                            f"skipped {forecast_summary.skipped}."
                        ),
                    )
                )
            else:
                steps.append(
                    _step(
                        "forecast-all",
                        WARNING,
                        "No snapshots were available for forecasting.",
                    )
                )

            _write_evidence_reports(session, safe_settings, steps)
            model_summary = model_status_summary(session)
            phase_3w = system_certification_card(session, settings=safe_settings)
            phase_3v = live_readiness_dashboard_card(session, settings=safe_settings)
            risk = advanced_risk_card(session, settings=safe_settings)
            freshness = latest_snapshot_freshness(session)
            database = database_status_card(session, settings=safe_settings)
            summary = {
                "freshness": freshness,
                "active_models": len(model_summary.active_models),
                "inactive_models": len(model_summary.inactive_models),
                "phase_3w": phase_3w,
                "phase_3v": phase_3v,
                "database": database,
                "advanced_risk": risk,
            }
            session.commit()
        except Exception as exc:
            session.rollback()
            steps.append(_step("remediation-run", BLOCKED, str(exc) or type(exc).__name__))

    if engine is not None:
        engine.dispose()
    return _finish_result(
        steps=steps,
        safety=safety,
        db_health=db_health,
        output_path=output_path,
        summary=summary,
    )


def paper_only_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "execution_enabled": False,
            "execution_dry_run": True,
            "execution_kill_switch": True,
            "autopilot_enabled": False,
            "autopilot_dry_run": True,
            "learning_block_demo_execution": True,
            "learning_block_live_execution": True,
            "ui_read_only": True,
            "phase_3w_system_certification_enabled": True,
            "phase_3w_mode": "LOCAL_INTEGRATION",
            "phase_3x_professional_ux_enabled": True,
            "phase_3x_mode": "preview",
        }
    )


def render_system_remediation_report(result: dict[str, Any]) -> str:
    lines = [
        "# System Readiness Remediation Report",
        "",
        f"- Generated at: `{result['generated_at']}`",
        f"- Status: `{result['status']}`",
        f"- Paper-only confirmed: `{result['safety']['paper_only_confirmed']}`",
        f"- Demo execution attempted: `{result['safety']['demo_execution_attempted']}`",
        f"- Live execution attempted: `{result['safety']['live_execution_attempted']}`",
        f"- Order write attempted: `{result['safety']['order_write_attempted']}`",
        "",
        "## Safety Boundary",
        "",
        "- This remediation does not submit live orders.",
        "- This remediation does not submit demo orders.",
        "- This remediation does not authorize production rollout.",
        "- Phase 3V and Phase 3W gates remain authoritative.",
        "",
        "## Steps",
        "",
        "| Step | Status | Message |",
        "| --- | --- | --- |",
    ]
    for step in result["steps"]:
        lines.append(f"| {step['name']} | {step['status']} | {_md(step['message'])} |")

    summary = result.get("summary") or {}
    lines.extend(["", "## Current State", ""])
    if summary:
        lines.extend(
            [
                f"- Database: `{summary['database']['status']}`",
                f"- Freshness: `{_freshness_label(summary['freshness'])}`",
                f"- Phase 3W: `{summary['phase_3w']['overall_status']}`",
                f"- Phase 3V: `{summary['phase_3v']['decision']}`",
                f"- Active models: `{summary['active_models']}`",
                f"- Inactive models: `{summary['inactive_models']}`",
                f"- Risk blocks: `{summary['advanced_risk'].get('block_count', 0)}`",
            ]
        )
    else:
        lines.append(
            "- Current state unavailable because remediation stopped before DB session use."
        )

    lines.extend(["", "## Recommended Next Actions", ""])
    lines.extend(f"- {item}" for item in result["recommended_next_actions"])
    lines.extend(
        [
            "",
            "## Next Local Commands",
            "",
            "```bash",
            "source .venv/bin/activate",
            "kalshi-bot db-health --output reports/database_report.md",
            "kalshi-bot system-remediate --refresh-data",
            "kalshi-bot system-remediation-report --output reports/system_readiness_remediation.md",
            "kalshi-bot ui",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _write_evidence_reports(
    session: Session,
    settings: Settings,
    steps: list[dict[str, Any]],
) -> None:
    report_steps = (
        (
            "database-report",
            lambda: generate_database_report(
                output_path=Path("reports/database_report.md"),
                settings=settings,
            ),
        ),
        (
            "model-readiness-report",
            lambda: generate_model_readiness_report(
                session,
                output_path=Path("reports/model_readiness.md"),
            ),
        ),
        (
            "live-readiness-report",
            lambda: generate_live_readiness_report(
                session,
                output_path=Path("reports/live_readiness_report.md"),
                json_output_path=Path("reports/live_readiness_decision.json"),
                settings=settings,
                persist=True,
            ),
        ),
        (
            "system-certification-report",
            lambda: generate_system_certification_report(
                session,
                output_dir=Path("reports/system_certification"),
                settings=settings,
                mode="LOCAL_INTEGRATION",
                run_contract_tests=True,
                run_golden_trace=True,
                persist=True,
            ),
        ),
        (
            "phase3x-report",
            lambda: generate_phase_3x_report(
                session,
                output_dir=Path("docs/phase_3x"),
                settings=settings,
            ),
        ),
        (
            "advanced-risk-report",
            lambda: generate_advanced_risk_report(session, settings=settings),
        ),
        (
            "learning-report",
            lambda: generate_learning_report(session, settings=settings),
        ),
        (
            "learning-diagnostics-report",
            lambda: generate_learning_diagnostics_report(session, settings=settings),
        ),
    )
    for name, writer in report_steps:
        try:
            path = writer()
            steps.append(_step(name, READY, f"Wrote {path}."))
        except Exception as exc:  # noqa: BLE001 - remediation should continue after report failures.
            steps.append(_step(name, WARNING, str(exc) or type(exc).__name__))


def _finish_result(
    *,
    steps: list[dict[str, Any]],
    safety: dict[str, Any],
    db_health: dict[str, Any],
    output_path: Path,
    summary: dict[str, Any],
) -> dict[str, Any]:
    result = {
        "generated_at": utc_now().isoformat(),
        "status": _overall_status(steps),
        "steps": steps,
        "safety": safety,
        "database_health": db_health,
        "summary": summary,
        "recommended_next_actions": _recommended_next_actions(
            steps=steps,
            db_health=db_health,
            summary=summary,
        ),
        "report_path": str(output_path),
        "live_trading_authorized": False,
        "demo_trading_authorized": False,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_system_remediation_report(result), encoding="utf-8")
    return result


def _safety_summary(original: Settings, safe: Settings) -> dict[str, Any]:
    return {
        "original_kalshi_env": original.kalshi_env,
        "paper_only_confirmed": (
            not safe.execution_enabled
            and safe.execution_dry_run
            and safe.execution_kill_switch
            and safe.autopilot_dry_run
            and safe.learning_block_demo_execution
            and safe.learning_block_live_execution
        ),
        "demo_execution_attempted": False,
        "live_execution_attempted": False,
        "order_write_attempted": False,
        "execution_enabled": safe.execution_enabled,
        "execution_dry_run": safe.execution_dry_run,
        "execution_kill_switch": safe.execution_kill_switch,
        "autopilot_enabled": safe.autopilot_enabled,
        "autopilot_dry_run": safe.autopilot_dry_run,
    }


def _is_unsafe_original_environment(settings: Settings) -> bool:
    return settings.kalshi_env.strip().lower() in {"live", "production", "prod"}


def _card_status(
    database: dict[str, Any],
    phase_3w: dict[str, Any],
    freshness: dict[str, Any],
) -> str:
    if database["status"] == BLOCKED:
        return BLOCKED
    if phase_3w["overall_status"] != "SYSTEM_PASS":
        return WARNING
    if _freshness_label(freshness) != "FRESH":
        return WARNING
    return READY


def _overall_status(steps: list[dict[str, Any]]) -> str:
    if any(step["status"] == BLOCKED for step in steps):
        return BLOCKED
    if any(step["status"] == WARNING for step in steps):
        return WARNING
    return READY


def _recommended_next_actions(
    *,
    steps: list[dict[str, Any]],
    db_health: dict[str, Any],
    summary: dict[str, Any],
) -> list[str]:
    if db_health["status"] == BLOCKED:
        return [
            "Stop the UI and any overnight/autopilot processes.",
            "Move SQLite out of OneDrive or migrate to PostgreSQL.",
            "Restore a known-good backup before rerunning remediation.",
            "Do not run live or demo execution.",
        ]
    actions = []
    if any(step["name"] == "collect-once" and step["status"] == WARNING for step in steps):
        actions.append("Rerun with --refresh-data to improve market freshness.")
    if summary:
        if _freshness_label(summary["freshness"]) != "FRESH":
            actions.append("Collect fresh market snapshots before trusting dashboard freshness.")
        if summary["phase_3w"]["overall_status"] != "SYSTEM_PASS":
            actions.append("Review Phase 3W evidence gaps; keep production rollout blocked.")
        if summary["phase_3v"]["decision"] not in {"GO", "CONDITIONAL_GO"}:
            actions.append("Keep live-capital actions unavailable until Phase 3V is approved.")
        if int(summary["advanced_risk"].get("block_count", 0) or 0):
            actions.append(
                "Review Phase 3N risk block reasons before tuning paper-trade thresholds."
            )
    actions.append("Continue paper-only learning and settlement syncing.")
    return actions


def _recommendations(
    *,
    database: dict[str, Any],
    phase_3w: dict[str, Any],
    phase_3v: dict[str, Any],
    freshness: dict[str, Any],
) -> list[str]:
    summary = {
        "database": database,
        "phase_3w": phase_3w,
        "phase_3v": phase_3v,
        "freshness": freshness,
        "advanced_risk": {"block_count": 0},
    }
    return _recommended_next_actions(
        steps=[],
        db_health={"status": database["status"]},
        summary=summary,
    )


def _freshness_label(freshness: dict[str, Any]) -> str:
    if freshness.get("latest_captured_at") is None:
        return "UNKNOWN"
    status = str(freshness.get("status") or freshness.get("freshness_status") or "").upper()
    if status in {"FRESH", "STALE", "UNKNOWN"}:
        return status
    age = freshness.get("age_minutes")
    if age is None:
        return "UNKNOWN"
    try:
        return "FRESH" if float(age) <= 30 else "STALE"
    except (TypeError, ValueError):
        return "UNKNOWN"


def _last_generated(path: Path) -> str:
    if not path.exists():
        return "not generated"
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()


def _step(
    name: str,
    status: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "message": message,
        "details": details or {},
    }


def _md(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")
