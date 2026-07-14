from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import database_url_from_settings
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.data.schema import Forecast, MarketLeg, MarketRanking
from kalshi_predictor.phase3bb_acceleration import (
    _metadata,
    _metadata_lines,
    _safety_flags,
    _write_manifest,
)
from kalshi_predictor.phase3bc_r6 import build_phase3bc_r5_status
from kalshi_predictor.utils.time import parse_datetime, utc_now

PHASE3BB_R10_VERSION = "phase3bb_r10_cloud_readiness_decision_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r10")
DEFAULT_REPORTS_DIR = Path("reports")

CLOUD_DECISION_STATUSES = {
    "NOT_NEEDED_YET",
    "USE_SMALL_VPS",
    "MIGRATE_TO_POSTGRES_FIRST",
    "NEED_ALWAYS_ON_SCHEDULER",
    "NEED_RATE_LIMIT_FIX_FIRST",
    "CLOUD_WOULD_NOT_HELP_CURRENT_BOTTLENECK",
}


@dataclass(frozen=True)
class Phase3BBR10CloudReadinessArtifacts:
    output_dir: Path
    executive_summary_path: Path
    decision_markdown_path: Path
    cost_plan_path: Path
    deployment_checklist_path: Path
    decision_json_path: Path
    manifest_path: Path


def write_phase3bb_r10_cloud_readiness_decision_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> Phase3BBR10CloudReadinessArtifacts:
    payload = build_phase3bb_r10_cloud_readiness_decision(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    decision_markdown_path = output_dir / "cloud_readiness_decision.md"
    cost_plan_path = output_dir / "cloud_cost_plan.md"
    deployment_checklist_path = output_dir / "deployment_checklist.md"
    decision_json_path = output_dir / "cloud_readiness_decision.json"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    decision_markdown_path.write_text(_render_decision(payload), encoding="utf-8")
    cost_plan_path.write_text(_render_cost_plan(payload), encoding="utf-8")
    deployment_checklist_path.write_text(_render_deployment_checklist(payload), encoding="utf-8")
    decision_json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            decision_markdown_path,
            cost_plan_path,
            deployment_checklist_path,
            decision_json_path,
        ],
    )
    return Phase3BBR10CloudReadinessArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        decision_markdown_path=decision_markdown_path,
        cost_plan_path=cost_plan_path,
        deployment_checklist_path=deployment_checklist_path,
        decision_json_path=decision_json_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r10_cloud_readiness_decision(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    now = utc_now()
    metadata = _metadata(
        session,
        settings=resolved,
        generated_at=now.isoformat(),
        command_args=command_args or [],
        output_dir=output_dir,
    )
    metadata["command_arguments"] = {
        "command": "kalshi-bot phase3bb-r10-cloud-readiness-decision",
        "argv": command_args or [],
    }
    writer = db_writer_monitor(settings=resolved)
    r5_status = build_phase3bc_r5_status(output_dir=reports_dir / "phase3bc_r5")
    machine = _machine_profile(metadata)
    category = _category_activity(session)
    artifacts = _artifact_freshness(reports_dir, now=now)
    evidence = _decision_evidence(
        metadata=metadata,
        writer=writer,
        r5_status=r5_status,
        machine=machine,
        category=category,
        artifacts=artifacts,
        settings=resolved,
    )
    decision = decide_cloud_readiness(evidence)
    cost_plan = cloud_cost_plan(decision["status"])
    deployment = deployment_plan(decision["status"], evidence)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "do_not_deploy": True,
        "production_settings_changed": False,
        "creates_paper_trades": False,
        "creates_paper_orders": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "db_writes_performed": 0,
    }
    return {
        **metadata,
        "phase": "3BB-R10-CLOUD-READINESS-DECISION",
        "phase_version": PHASE3BB_R10_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_DECISION_GATE",
        "reports_dir": str(reports_dir),
        "writer": writer,
        "r5_status": r5_status,
        "machine_profile": machine,
        "category_activity": category,
        "artifact_freshness": artifacts,
        "decision_evidence": evidence,
        "decision": decision,
        "cost_plan": cost_plan,
        "deployment_plan": deployment,
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def decide_cloud_readiness(evidence: dict[str, Any]) -> dict[str, Any]:
    writer_active = bool(evidence.get("writer_active"))
    sqlite_backend = bool(evidence.get("sqlite_backend"))
    r5_overrunning = bool(evidence.get("r5_overrunning"))
    r5_running = bool(evidence.get("r5_running"))
    cpu_bottleneck = bool(evidence.get("cpu_bottleneck"))
    ram_bottleneck = bool(evidence.get("ram_bottleneck"))
    rate_limit_risk = bool(evidence.get("api_rate_limit_risk"))
    scheduler_needed = bool(evidence.get("scheduler_needed"))
    categories_running = int(evidence.get("categories_running") or 0)
    current_bot_blocker = str(evidence.get("current_bot_blocker") or "UNKNOWN")

    reasons: list[str] = []
    if rate_limit_risk:
        status = "NEED_RATE_LIMIT_FIX_FIRST"
        buy_compute = False
        recommendation = "local only"
        reasons.append("API/rate-limit evidence should be fixed before buying compute.")
    elif writer_active and sqlite_backend and not r5_overrunning:
        status = "MIGRATE_TO_POSTGRES_FIRST"
        buy_compute = False
        recommendation = "local Postgres or VPS + Postgres after migration plan"
        reasons.append("SQLite writer contention is visible; another host alone will not fix it.")
    elif r5_overrunning or (scheduler_needed and not r5_running):
        status = "NEED_ALWAYS_ON_SCHEDULER"
        buy_compute = True
        recommendation = "small VPS + systemd"
        reasons.append("The main value of cloud is an always-on guarded scheduler.")
    elif cpu_bottleneck or ram_bottleneck:
        status = "USE_SMALL_VPS"
        buy_compute = True
        recommendation = "small VPS"
        reasons.append("Local CPU/RAM pressure is high enough that a small VPS is justified.")
    elif categories_running >= 3 and scheduler_needed:
        status = "USE_SMALL_VPS"
        buy_compute = True
        recommendation = "small VPS + systemd"
        reasons.append("Multiple categories need bounded scheduling without manual babysitting.")
    elif current_bot_blocker in {
        "EV_NOT_POSITIVE",
        "WATCH_NO_POSITIVE_EXPECTED_VALUE",
        "WAITING_FOR_EXECUTABLE_BOOK",
        "NO_CURRENT_POSITIVE_EV",
        "PAPER_READY_CLOSED",
    }:
        status = "CLOUD_WOULD_NOT_HELP_CURRENT_BOTTLENECK"
        buy_compute = False
        recommendation = "local only"
        reasons.append("The current blocker is market/opportunity quality, not compute.")
    else:
        status = "NOT_NEEDED_YET"
        buy_compute = False
        recommendation = "local only"
        reasons.append("No decisive compute, memory, writer, or scheduler bottleneck is visible.")

    if status not in CLOUD_DECISION_STATUSES:
        status = "NOT_NEEDED_YET"
    return {
        "status": status,
        "buy_compute_now": buy_compute,
        "recommendation": recommendation,
        "recommended_architecture": _architecture_for_status(status),
        "primary_reason": reasons[0],
        "reasons": reasons,
        "cloud_will_help": _cloud_help_for_status(status),
        "cloud_will_not_help": [
            "Finding positive EV",
            "Creating executable orderbooks",
            "Lowering spread/liquidity/risk gates",
            "Replacing exact source evidence",
        ],
        "next_operator_command": _next_operator_command(status),
    }


def cloud_cost_plan(status: str) -> dict[str, Any]:
    if status == "USE_SMALL_VPS":
        return {
            "buy": True,
            "monthly_budget_usd": 24,
            "budget_ceiling_usd": 35,
            "spec": "2 vCPU, 4 GB RAM, 80 GB NVMe SSD, Ubuntu 24.04 LTS",
            "storage": "80 GB minimum; keep DB backups off-host.",
            "postgres": "optional later",
            "upgrade_when": "CPU load stays above 0.85 or RAM free stays below 15%.",
        }
    if status == "NEED_ALWAYS_ON_SCHEDULER":
        return {
            "buy": True,
            "monthly_budget_usd": 24,
            "budget_ceiling_usd": 35,
            "spec": "2 vCPU, 4 GB RAM, 80 GB NVMe SSD, Ubuntu 24.04 LTS",
            "storage": "80 GB minimum for DB, reports, logs, and backups.",
            "postgres": "not required before first scheduler move",
            "upgrade_when": "Add 3+ writer-heavy categories or dashboard users.",
        }
    if status == "MIGRATE_TO_POSTGRES_FIRST":
        return {
            "buy": False,
            "monthly_budget_usd": 0,
            "budget_ceiling_usd": 0,
            "spec": "No VPS purchase until the DB migration plan is tested.",
            "storage": "Keep local DB backups before migration.",
            "postgres": "required first; expected future VPS+Postgres budget is 35 USD/mo.",
            "upgrade_when": "After local Postgres migration and writer-gate validation pass.",
        }
    return {
        "buy": False,
        "monthly_budget_usd": 0,
        "budget_ceiling_usd": 0,
        "spec": "Do not buy compute for this phase.",
        "storage": "Continue local backups of DB and reports.",
        "postgres": "deferred",
        "upgrade_when": (
            "Buy only after scheduler, CPU/RAM, or multi-category writer load is decisive."
        ),
    }


def deployment_plan(status: str, evidence: dict[str, Any]) -> dict[str, Any]:
    should_prepare = status in {"USE_SMALL_VPS", "NEED_ALWAYS_ON_SCHEDULER"}
    return {
        "deploy_now": False,
        "prepare_deployment": should_prepare,
        "recommended_runtime": "systemd services on Ubuntu" if should_prepare else "local only",
        "docker_compose": "optional after Postgres migration",
        "postgres_readiness": _postgres_readiness(status, evidence),
        "backup_plan": [
            "Take a DB backup before every migration or writer-capable maintenance job.",
            "Keep reports/ and logs/ on persistent disk.",
            "Keep secrets out of report archives.",
            "Copy nightly DB backups off-host before always-on operation.",
        ],
        "checklist": _deployment_checklist(status, evidence),
    }


def _decision_evidence(
    *,
    metadata: dict[str, Any],
    writer: dict[str, Any],
    r5_status: dict[str, Any],
    machine: dict[str, Any],
    category: dict[str, Any],
    artifacts: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    db_url = database_url_from_settings(settings)
    sqlite_backend = db_url.startswith("sqlite")
    guard = r5_status.get("guard") or {}
    latest_summary = r5_status.get("latest_summary") or {}
    current_bot_blocker = (
        latest_summary.get("phase3bc_main_blocker")
        or latest_summary.get("primary_gap_after_refresh")
        or artifacts.get("phase3bb_r8_blocker")
        or "UNKNOWN"
    )
    writer_active = bool(writer.get("current_writer_pid"))
    r5_overrunning = str(guard.get("status") or "").upper() == "OVERRUNNING"
    one_drive_risk = "onedrive" in str(metadata.get("repository_root") or "").lower()
    categories_running = int(category.get("categories_with_forecasts_or_rankings") or 0)
    api_rate_limit_risk = _artifact_text_has(
        reports_dir=Path(metadata["output_dir"]).parent,
        needle="rate limit",
    )
    scheduler_needed = r5_overrunning or one_drive_risk or categories_running >= 2
    return {
        "cpu_bottleneck": bool(machine.get("cpu_bottleneck")),
        "ram_bottleneck": bool(machine.get("ram_bottleneck")),
        "sqlite_writer_contention": writer_active and sqlite_backend,
        "writer_active": writer_active,
        "writer_status": writer.get("status"),
        "writer_pid": writer.get("current_writer_pid"),
        "writer_command": writer.get("current_writer_command"),
        "sqlite_backend": sqlite_backend,
        "database_path": (metadata.get("database_fingerprint") or {}).get("path"),
        "onedrive_path_risk": one_drive_risk,
        "r5_running": bool(guard.get("running")),
        "r5_overrunning": r5_overrunning,
        "r5_should_stop": bool(guard.get("should_stop")),
        "r5_elapsed_seconds": guard.get("elapsed_seconds"),
        "r5_latest_watch_state": r5_status.get("latest_watch_state"),
        "api_rate_limit_risk": api_rate_limit_risk,
        "categories_running": categories_running,
        "scheduler_needed": scheduler_needed,
        "postgres_ready": not sqlite_backend,
        "backup_needed": True,
        "current_bot_blocker": current_bot_blocker,
    }


def _machine_profile(metadata: dict[str, Any]) -> dict[str, Any]:
    cpu_count = os.cpu_count() or 0
    load_avg = _load_average()
    load_ratio = (
        round(float(load_avg[0]) / float(cpu_count), 3)
        if load_avg and cpu_count > 0
        else None
    )
    memory = _memory_profile()
    disk = _disk_profile(Path.cwd())
    db_path = (metadata.get("database_fingerprint") or {}).get("path")
    db_disk = _disk_profile(Path(db_path).parent) if db_path else {}
    return {
        "cpu_count": cpu_count,
        "load_average_1m_5m_15m": load_avg,
        "load_to_cpu_ratio_1m": load_ratio,
        "cpu_bottleneck": load_ratio is not None and load_ratio >= 0.85,
        "memory": memory,
        "ram_bottleneck": bool(memory.get("available_ratio", 1.0) < 0.15),
        "workspace_disk": disk,
        "database_disk": db_disk,
    }


def _memory_profile() -> dict[str, Any]:
    path = Path("/proc/meminfo")
    if not path.exists():
        return {"available": False}
    values: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].rstrip(":") in {"MemTotal", "MemAvailable"}:
            values[parts[0].rstrip(":")] = int(parts[1]) * 1024
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    ratio = round(available / total, 3) if total and available is not None else None
    return {
        "available": True,
        "total_bytes": total,
        "available_bytes": available,
        "available_ratio": ratio,
    }


def _disk_profile(path: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"available": False, "path": str(path)}
    free_ratio = round(usage.free / usage.total, 3) if usage.total else None
    return {
        "available": True,
        "path": str(path),
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "free_ratio": free_ratio,
    }


def _load_average() -> list[float] | None:
    try:
        return [round(float(value), 3) for value in os.getloadavg()]
    except (AttributeError, OSError):
        return None


def _category_activity(session: Session) -> dict[str, Any]:
    parsed_by_category = {
        str(category): int(count or 0)
        for category, count in session.execute(
            select(MarketLeg.category, func.count(func.distinct(MarketLeg.ticker))).group_by(
                MarketLeg.category
            )
        ).all()
        if category
    }
    forecast_models = {
        str(model): int(count or 0)
        for model, count in session.execute(
            select(Forecast.model_name, func.count()).group_by(Forecast.model_name)
        ).all()
        if model
    }
    ranking_models = {
        str(model): int(count or 0)
        for model, count in session.execute(
            select(MarketRanking.forecast_model, func.count()).group_by(
                MarketRanking.forecast_model
            )
        ).all()
        if model
    }
    category_names = {
        _category_for_model(model)
        for model in set(forecast_models) | set(ranking_models)
        if forecast_models.get(model, 0) > 0 or ranking_models.get(model, 0) > 0
    }
    return {
        "parsed_by_category": parsed_by_category,
        "forecast_models": forecast_models,
        "ranking_models": ranking_models,
        "categories_with_forecasts_or_rankings": len(category_names),
        "active_engine_categories": sorted(category_names),
    }


def _artifact_freshness(reports_dir: Path, *, now: datetime) -> dict[str, Any]:
    paths = {
        "phase3ba_status": reports_dir / "phase3ba_status" / "status.json",
        "phase3bb_r8": reports_dir / "phase3bb_r8" / "unified_paper_gate.md",
        "phase3bb_r9": reports_dir / "phase3bb_r9" / "learning_acceleration.md",
        "phase3bc_r5_status": reports_dir / "phase3bc_r5" / "phase3bc_r5_status.json",
    }
    result: dict[str, Any] = {}
    for key, path in paths.items():
        payload = _read_json(path) if path.suffix == ".json" else {}
        text = _read_text(path) if path.suffix != ".json" else ""
        generated = payload.get("generated_at") or _generated_from_markdown(text)
        parsed = parse_datetime(generated)
        age_seconds = (
            int(max(0, (now - parsed).total_seconds())) if parsed is not None else None
        )
        result[key] = {
            "path": str(path),
            "exists": path.exists(),
            "generated_at": generated,
            "age_seconds": age_seconds,
            "freshness": "CURRENT" if path.exists() and age_seconds is not None else "UNKNOWN",
        }
    result["phase3bb_r8_blocker"] = _read_phase3bb_r8_blocker(paths["phase3bb_r8"])
    return result


def _read_phase3bb_r8_blocker(path: Path) -> str | None:
    text = _read_text(path)
    for line in text.splitlines():
        if "Primary blocker" in line or "True first blocker" in line:
            return line.split("`")[1] if "`" in line else line.strip()
    return None


def _artifact_text_has(*, reports_dir: Path, needle: str) -> bool:
    lowered = needle.lower()
    candidates = [
        reports_dir / "phase3bb_r1" / "operator_scheduler.json",
        reports_dir / "phase3bb_r8" / "unified_paper_gate.md",
        reports_dir / "phase3bc_r5" / "phase3bc_r5_status.md",
    ]
    for path in candidates:
        text = _read_text(path).lower()
        if lowered in text or "429" in text:
            return True
    return False


def _architecture_for_status(status: str) -> str:
    if status == "NEED_ALWAYS_ON_SCHEDULER":
        return "small VPS with systemd guarded scheduler; keep PAPER/READ-ONLY"
    if status == "USE_SMALL_VPS":
        return "small VPS for always-on bounded jobs; Postgres optional later"
    if status == "MIGRATE_TO_POSTGRES_FIRST":
        return "test Postgres migration before buying or scaling compute"
    return "local-only until the current non-compute blocker changes"


def _cloud_help_for_status(status: str) -> list[str]:
    if status in {"USE_SMALL_VPS", "NEED_ALWAYS_ON_SCHEDULER"}:
        return [
            "Always-on scheduling",
            "Cleaner Linux filesystem outside OneDrive",
            "More predictable DB backups and logs",
            "Fewer manual command chains",
        ]
    if status == "MIGRATE_TO_POSTGRES_FIRST":
        return ["Cloud can help after the database writer bottleneck is redesigned."]
    return ["No immediate cloud benefit for the current trading blocker."]


def _next_operator_command(status: str) -> str:
    if status == "NEED_ALWAYS_ON_SCHEDULER":
        return (
            "kalshi-bot phase3bb-r1-operator-scheduler --output-dir "
            "reports/phase3bb_r1 --reports-dir reports"
        )
    if status == "MIGRATE_TO_POSTGRES_FIRST":
        return "kalshi-bot db-writer-monitor --json"
    if status == "USE_SMALL_VPS":
        return (
            "kalshi-bot phase3bb-r10-cloud-readiness-decision --output-dir "
            "reports/phase3bb_r10 --reports-dir reports"
        )
    return (
        "kalshi-bot phase3bb-r8-unified-paper-gate --output-dir "
        "reports/phase3bb_r8 --reports-dir reports"
    )


def _postgres_readiness(status: str, evidence: dict[str, Any]) -> dict[str, Any]:
    sqlite_backend = bool(evidence.get("sqlite_backend"))
    return {
        "current_backend": "sqlite" if sqlite_backend else "postgres_or_other",
        "required_before_purchase": status == "MIGRATE_TO_POSTGRES_FIRST",
        "required_before_multi_category_scale": sqlite_backend,
        "reason": (
            "SQLite is okay for one writer, but Postgres is safer before multiple "
            "writer-heavy categories run unattended."
        ),
    }


def _deployment_checklist(status: str, evidence: dict[str, Any]) -> list[str]:
    items = [
        "Confirm PAPER/READ-ONLY env flags before copying credentials.",
        "Backup the SQLite DB and reports directory.",
        "Run phase3ba-status and db-writer-monitor locally before migration.",
        "Create a non-OneDrive Linux working directory on the target host.",
        "Install Python, build tools, and the repo virtual environment.",
        "Copy only required read-only Kalshi credentials and source keys.",
        "Run R10 again on the target host before starting watchers.",
        "Start exactly one guarded R5 watcher.",
        "Configure nightly DB/report backups.",
    ]
    if status == "MIGRATE_TO_POSTGRES_FIRST":
        items.insert(3, "Create and test the Postgres migration before buying VPS capacity.")
    if evidence.get("r5_overrunning"):
        items.insert(0, "Clear or restart the overrun guarded R5 watcher before migration work.")
    return items


def _category_for_model(model_name: str) -> str:
    lowered = model_name.lower()
    if "weather" in lowered:
        return "weather"
    if "crypto" in lowered:
        return "crypto"
    if any(token in lowered for token in ("economic", "cpi", "fed", "gdp", "jobs")):
        return "economic"
    if any(token in lowered for token in ("sports", "mlb", "nba", "nfl", "nhl")):
        return "sports"
    if any(token in lowered for token in ("usda", "agri", "commodity")):
        return "agriculture_general"
    if "news" in lowered:
        return "news"
    return "general"


def _generated_from_markdown(text: str) -> str | None:
    for line in text.splitlines()[:20]:
        if line.startswith("- Generated at:"):
            parts = line.split("`")
            if len(parts) >= 2:
                return parts[1]
    return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R10 Cloud Readiness Decision")
    decision = payload["decision"]
    cost = payload["cost_plan"]
    evidence = payload["decision_evidence"]
    lines.extend(
        [
            "",
            "## Decision",
            "",
            f"- Status: `{decision['status']}`",
            f"- Buy compute now: `{decision['buy_compute_now']}`",
            f"- Recommendation: `{decision['recommendation']}`",
            f"- Monthly budget: `${cost['monthly_budget_usd']}`",
            f"- Budget ceiling: `${cost['budget_ceiling_usd']}`",
            f"- Primary reason: {decision['primary_reason']}",
            "",
            "## Current Evidence",
            "",
            f"- Active writer: `{evidence['writer_active']}`",
            f"- Writer PID: `{evidence['writer_pid']}`",
            f"- SQLite backend: `{evidence['sqlite_backend']}`",
            f"- OneDrive path risk: `{evidence['onedrive_path_risk']}`",
            f"- R5 running: `{evidence['r5_running']}`",
            f"- R5 overrunning: `{evidence['r5_overrunning']}`",
            f"- Current bot blocker: `{evidence['current_bot_blocker']}`",
            "",
            "## Safety",
            "",
            "- No deployment was performed.",
            "- No production settings were changed.",
            "- No paper/live/demo trades were created.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_decision(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R10 Cloud Readiness Decision Detail")
    decision = payload["decision"]
    evidence = payload["decision_evidence"]
    machine = payload["machine_profile"]
    category = payload["category_activity"]
    lines.extend(
        [
            "",
            "## Buy Or Wait",
            "",
            f"- Decision status: `{decision['status']}`",
            f"- Buy compute now: `{decision['buy_compute_now']}`",
            f"- Architecture: `{decision['recommended_architecture']}`",
            "",
            "## Why",
            "",
        ]
    )
    for reason in decision["reasons"]:
        lines.append(f"- {reason}")
    lines.extend(["", "## Bottleneck Checks", ""])
    checks = [
        ("CPU bottleneck", evidence["cpu_bottleneck"]),
        ("RAM bottleneck", evidence["ram_bottleneck"]),
        ("SQLite writer contention", evidence["sqlite_writer_contention"]),
        ("OneDrive path risk", evidence["onedrive_path_risk"]),
        ("R5 overrunning", evidence["r5_overrunning"]),
        ("API rate-limit risk", evidence["api_rate_limit_risk"]),
        ("Scheduler needed", evidence["scheduler_needed"]),
        ("Postgres ready", evidence["postgres_ready"]),
    ]
    for label, value in checks:
        lines.append(f"- {label}: `{value}`")
    lines.extend(
        [
            "",
            "## Machine",
            "",
            f"- CPU count: `{machine['cpu_count']}`",
            f"- Load average: `{machine['load_average_1m_5m_15m']}`",
            f"- Load/CPU ratio: `{machine['load_to_cpu_ratio_1m']}`",
            f"- RAM available ratio: `{machine.get('memory', {}).get('available_ratio')}`",
            "",
            "## Category Load",
            "",
            f"- Active engine categories: `{category['active_engine_categories']}`",
            "- Categories with forecasts/rankings: "
            f"`{category['categories_with_forecasts_or_rankings']}`",
            "",
            "## Cloud Helps",
            "",
        ]
    )
    for item in decision["cloud_will_help"]:
        lines.append(f"- {item}")
    lines.extend(["", "## Cloud Does Not Help", ""])
    for item in decision["cloud_will_not_help"]:
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def _render_cost_plan(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R10 Cloud Cost Plan")
    cost = payload["cost_plan"]
    decision = payload["decision"]
    lines.extend(
        [
            "",
            "## Budget",
            "",
            f"- Buy: `{cost['buy']}`",
            f"- Monthly budget: `${cost['monthly_budget_usd']}`",
            f"- Budget ceiling: `${cost['budget_ceiling_usd']}`",
            f"- Specs: `{cost['spec']}`",
            f"- Storage: {cost['storage']}",
            f"- Postgres: {cost['postgres']}",
            f"- Upgrade when: {cost['upgrade_when']}",
            "",
            "## Exact Purchase Rule",
            "",
            (
                "Buy only if the decision status is `USE_SMALL_VPS` or "
                "`NEED_ALWAYS_ON_SCHEDULER`; otherwise spend `$0` on compute now."
            ),
            "",
            f"- Current decision: `{decision['status']}`",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_deployment_checklist(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R10 Deployment Checklist")
    plan = payload["deployment_plan"]
    lines.extend(
        [
            "",
            "## Deployment Status",
            "",
            f"- Deploy now: `{plan['deploy_now']}`",
            f"- Prepare deployment: `{plan['prepare_deployment']}`",
            f"- Runtime: `{plan['recommended_runtime']}`",
            f"- Docker Compose: `{plan['docker_compose']}`",
            "",
            "## Checklist",
            "",
        ]
    )
    for item in plan["checklist"]:
        lines.append(f"- [ ] {item}")
    lines.extend(["", "## Backups", ""])
    for item in plan["backup_plan"]:
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"
