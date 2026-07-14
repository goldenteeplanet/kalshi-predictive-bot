from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import (
    CryptoMarketLink,
    EconomicMarketLink,
    Forecast,
    ForecastSkipLog,
    LearningTradeTarget,
    MarketLeg,
    MarketOpportunity,
    MarketRanking,
    MarketSnapshot,
    NewsMarketLink,
    PaperOrder,
    SelfEvaluationFinding,
    SelfEvaluationJournal,
    SelfEvaluationRun,
    SportsFeature,
    SportsGame,
    SportsMarketLink,
    WeatherMarketLink,
)
from kalshi_predictor.feature_discovery.repository import feature_discovery_status
from kalshi_predictor.learning.diagnostics import build_learning_diagnostics
from kalshi_predictor.learning.safety import settled_paper_trade_count
from kalshi_predictor.market_legs import DISPLAY_CATEGORIES, LINKED_CATEGORIES
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3aa import build_settlement_eta_schedule
from kalshi_predictor.phase3ab import build_learning_governor
from kalshi_predictor.phase3ac import build_sports_provenance_snapshot
from kalshi_predictor.phase3z import build_market_coverage_doctor
from kalshi_predictor.reinforcement_learning.repository import rl_status
from kalshi_predictor.utils.time import utc_now

PHASE_3AD_VERSION = "phase3ad_v1"
PHASE3AH_SPORTS_REPORT_PATH = Path(
    "reports/phase3ah_sports/phase3ah_sports_evidence_backfill.json"
)
PHASE3AH_ROSTER_REPORT_PATH = Path(
    "reports/phase3ah_sports/phase3ah_roster_participant_verification.json"
)
PHASE3AH_PLACEHOLDER_REPORT_PATH = Path(
    "reports/phase3ah_sports/phase3ah_round_placeholder_resolution_report.json"
)
PHASE3AH_PLACEHOLDER_WATCH_PATH = Path(
    "reports/phase3ah_sports/phase3ah_sports_placeholder_watch.json"
)
PHASE3AA_R5_REPORT_PATH = Path(
    "reports/phase3aa_r5/phase3aa_r5_closed_market_outcome_capture.json"
)
PHASE3AZ_GAP_ANALYSIS_PATH = Path("reports/phase3az/phase3az_gap_analysis.json")
MARKET_COVERAGE_REPORT_PATH = Path("reports/market_coverage/market_coverage_doctor.json")

PHASE_MODULES = (
    ("3Y", "kalshi_predictor.phase3y"),
    ("3Z", "kalshi_predictor.phase3z"),
    ("3AA", "kalshi_predictor.phase3aa"),
    ("3AA-R2", "kalshi_predictor.phase3aa_r2"),
    ("3AA-R3", "kalshi_predictor.phase3aa_r3"),
    ("3AA-R5", "kalshi_predictor.phase3aa_r5"),
    ("3AB", "kalshi_predictor.phase3ab"),
    ("3AC", "kalshi_predictor.phase3ac"),
    ("3AD", "kalshi_predictor.phase3ad"),
    ("3AE", "kalshi_predictor.phase3ae"),
    ("3AF", "kalshi_predictor.phase3af"),
    ("3AG", "kalshi_predictor.phase3ag"),
    ("3AH", "kalshi_predictor.phase3ah"),
    ("3AH-WATCH", "kalshi_predictor.phase3ah_placeholder_watch"),
    ("3AI", "kalshi_predictor.phase3ai"),
    ("3AJ", "kalshi_predictor.phase3aj"),
    ("3AK", "kalshi_predictor.phase3ak"),
    ("3AL", "kalshi_predictor.phase3al"),
    ("3AM", "kalshi_predictor.phase3am"),
    ("3AN", "kalshi_predictor.phase3an"),
    ("3AO", "kalshi_predictor.phase3ao"),
    ("3AP", "kalshi_predictor.phase3ap"),
    ("3AQ", "kalshi_predictor.phase3aq"),
    ("3AR", "kalshi_predictor.phase3ar"),
    ("3AS", "kalshi_predictor.phase3as"),
    ("3AT", "kalshi_predictor.phase3at"),
    ("3AY", "kalshi_predictor.phase3ay"),
    ("3AZ", "kalshi_predictor.phase3az"),
    ("3BB", "kalshi_predictor.phase3bb"),
)

PHASE_COMMANDS = (
    "paper-settlement-doctor",
    "phase3aa-realize",
    "phase3aa-r2-exact-settlement-harvest",
    "phase3aa-r3-residual-settlement-audit",
    "phase3aa-r5-closed-market-outcome-capture",
    "phase3ab-learning-governor",
    "phase3ac-sports-provenance-repair",
    "phase3ae-verified-sports-connector",
    "phase3af-sports-schedule-bootstrap",
    "phase3ag-sports-ambiguity-coverage",
    "phase3ag-sports-link-repair-pass",
    "phase3ah-sports-evidence-backfill",
    "phase3ah-round-placeholder-resolution",
    "phase3ah-sports-placeholder-watch",
    "phase3ah-roster-participant-verification",
    "phase3ag-crypto-pipeline",
    "snapshot-coverage-repair",
    "phase3ai-link-reconciliation",
    "phase3aj-sports-alias-provenance",
    "phase3ak-multileg-provenance",
    "phase3al-learning-resume",
    "phase3am-sports-verified-upgrade",
    "phase3an-crypto-feature-completeness",
    "phase3ao-learning-reward-pipeline",
    "phase3ap-night-runner-v2",
    "phase3aq-self-improvement",
    "crypto-forecast-doctor",
    "phase3ar-crypto-forecast-coverage",
    "active-universe-doctor",
    "phase3as-active-universe",
    "crypto-history-warmup",
    "phase3at-active-router",
    "active-crypto-router",
    "phase3ay-health-refresh",
    "phase3ay-status",
    "phase3az-gap-analysis",
    "phase3bb-domain-readiness",
    "phase3bb-r2-general-candidate-routing",
    "phase3bb-r2-general-source-intake",
    "phase3bb-r2-general-source-evidence",
    "phase3bb-r2-general-source-availability",
    "phase3bb-r3-general-reclassification",
    "learning-diagnostics",
    "market-coverage-doctor",
    "self-evaluate",
    "feature-discovery-run",
    "rl-evaluate",
    "phase-orchestrator",
)


@dataclass(frozen=True)
class Phase3ADArtifactSet:
    output_path: Path
    json_path: Path
    next_prompt_path: Path


def build_phase_orchestrator(
    session: Session,
    *,
    settings: Settings | None = None,
    scan_limit: int = 500,
    refresh_market_coverage: bool = False,
    refresh_learning_diagnostics: bool = False,
    refresh_sports_provenance: bool = False,
) -> dict[str, Any]:
    """Build a paper-only roadmap from local evidence.

    This function intentionally does not execute generated prompts, enable live trading,
    or call demo/live execution paths.
    """
    resolved = settings or get_settings()
    session.flush()
    bounded_scan_limit = max(1, scan_limit)
    settlement_limit = max(200, bounded_scan_limit)
    settlement = build_settlement_eta_schedule(session, limit=settlement_limit)
    learning_governor = build_learning_governor(
        session,
        settings=resolved,
        limit=bounded_scan_limit,
    )
    sports = _sports_provenance_evidence(
        session,
        scan_limit=bounded_scan_limit,
        refresh=refresh_sports_provenance,
    )
    learning = _learning_diagnostics_evidence(
        session,
        settings=resolved,
        scan_limit=bounded_scan_limit,
        refresh=refresh_learning_diagnostics,
    )
    coverage = _market_coverage_evidence(
        session,
        settings=resolved,
        scan_limit=bounded_scan_limit,
        refresh=refresh_market_coverage,
    )
    evidence = {
        "settlement": settlement,
        "learning_governor": learning_governor,
        "sports_provenance": sports,
        "learning_diagnostics": learning,
        "market_coverage": coverage,
        "phase3ah_sports_evidence": _report_summary(PHASE3AH_SPORTS_REPORT_PATH),
        "phase3ah_roster_verification": _report_summary(PHASE3AH_ROSTER_REPORT_PATH),
        "phase3ah_round_placeholder_resolution": _report_summary(
            PHASE3AH_PLACEHOLDER_REPORT_PATH
        ),
        "phase3ah_placeholder_watch": _report_summary(PHASE3AH_PLACEHOLDER_WATCH_PATH),
        "phase3aa_r5_closed_market_capture": _report_summary(PHASE3AA_R5_REPORT_PATH),
        "phase3az_gap_analysis": _report_summary(PHASE3AZ_GAP_ANALYSIS_PATH),
        "self_improvement": _self_improvement_status(session),
        "phase_status": _phase_status(),
    }
    bottlenecks = _bottlenecks(evidence)
    improvement_candidates = _improvement_candidates(evidence, bottlenecks)
    next_phase = _choose_next_phase(evidence, bottlenecks)
    prompt = _implementation_prompt(next_phase, evidence, bottlenecks, improvement_candidates)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AD",
        "phase_version": PHASE_3AD_VERSION,
        "mode": "PAPER_ONLY_ROADMAP_ENGINE",
        "bounded_runtime": {
            "scan_limit": bounded_scan_limit,
            "settlement_limit": settlement_limit,
            "learning_rejection_limit": bounded_scan_limit,
            "market_coverage_source": coverage.get("source", "unknown"),
            "sports_provenance_source": sports.get("source", "unknown"),
            "learning_diagnostics_source": learning.get("source", "unknown"),
            "market_coverage_refresh_requested": refresh_market_coverage,
            "sports_provenance_refresh_requested": refresh_sports_provenance,
            "learning_diagnostics_refresh_requested": refresh_learning_diagnostics,
            "full_market_coverage_scan_default": False,
            "full_sports_provenance_scan_default": False,
            "full_learning_diagnostics_scan_default": False,
        },
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "automation_policy": {
            "executes_generated_code": False,
            "enables_live_trading": False,
            "enables_demo_execution": False,
            "requires_human_approval_for_implementation": True,
        },
        "expected_commands": list(PHASE_COMMANDS),
        "evidence": evidence,
        "bottlenecks": bottlenecks,
        "improvement_candidates": improvement_candidates,
        "next_phase": next_phase,
        "implementation_prompt": prompt,
        "recommended_loop": _recommended_loop(next_phase),
    }


def write_phase_orchestrator_report(
    session: Session,
    *,
    output_path: Path = Path("reports/phase_orchestrator.md"),
    json_path: Path | None = None,
    next_prompt_path: Path = Path("prompts/next_phase.md"),
    settings: Settings | None = None,
    scan_limit: int = 500,
    refresh_market_coverage: bool = False,
    refresh_learning_diagnostics: bool = False,
    refresh_sports_provenance: bool = False,
) -> Phase3ADArtifactSet:
    payload = build_phase_orchestrator(
        session,
        settings=settings,
        scan_limit=scan_limit,
        refresh_market_coverage=refresh_market_coverage,
        refresh_learning_diagnostics=refresh_learning_diagnostics,
        refresh_sports_provenance=refresh_sports_provenance,
    )
    resolved_json_path = json_path or output_path.with_suffix(".json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_json_path.parent.mkdir(parents=True, exist_ok=True)
    next_prompt_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_render_markdown(payload, next_prompt_path), encoding="utf-8")
    resolved_json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    next_prompt_path.write_text(payload["implementation_prompt"], encoding="utf-8")
    return Phase3ADArtifactSet(output_path, resolved_json_path, next_prompt_path)


def _coverage_summary(coverage: dict[str, Any], *, source: str) -> dict[str, Any]:
    return {
        "source": source,
        "parse_failures": coverage["stage_counts"].get("parse_failures"),
        "parsed_markets": coverage["stage_counts"].get("parsed_markets"),
        "parsed_legs": coverage["stage_counts"].get("parsed_legs"),
        "recommendations": coverage["recommendations"],
        "rows": [
            {
                "scope": row["scope_key"],
                "health": row["health"],
                "parsed_markets": row["parsed_markets"],
                "external_linked_markets": row["external_linked_markets"],
                "partial_markets": row["partial_markets"],
                "coverage": row["coverage"],
                "next_action": row["next_action"]["summary"],
            }
            for row in coverage["coverage_rows"]
        ],
    }


def _market_coverage_evidence(
    session: Session,
    *,
    settings: Settings,
    scan_limit: int,
    refresh: bool,
) -> dict[str, Any]:
    if not refresh:
        cached = _market_coverage_from_report(MARKET_COVERAGE_REPORT_PATH)
        if cached is not None:
            return cached
        return _bounded_market_coverage_summary(session, scan_limit=scan_limit)
    coverage = build_market_coverage_doctor(
        session,
        settings=settings,
        parse_first=False,
    )
    return _coverage_summary(coverage, source="fresh_market_coverage_doctor")


def _market_coverage_from_report(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if not isinstance(payload.get("stage_counts"), dict):
        return None
    if not isinstance(payload.get("coverage_rows"), list):
        return None
    return _coverage_summary(payload, source=f"cached_report:{path}")


def _bounded_market_coverage_summary(
    session: Session,
    *,
    scan_limit: int,
) -> dict[str, Any]:
    del scan_limit
    category_counts = _market_leg_category_counts(session)
    link_counts = _link_table_counts(session)
    rows = []
    for category in DISPLAY_CATEGORIES:
        counts = category_counts.get(category, {"parsed_legs": 0, "parsed_markets": 0})
        parsed_markets = int(counts["parsed_markets"])
        linked_markets = int(link_counts.get(category, 0)) if category in LINKED_CATEGORIES else 0
        health = _bounded_coverage_health(
            category=category,
            parsed_markets=parsed_markets,
            linked_markets=linked_markets,
        )
        rows.append(
            {
                "scope": category,
                "health": health,
                "parsed_markets": parsed_markets,
                "external_linked_markets": linked_markets,
                "partial_markets": 0,
                "coverage": _coverage_ratio(linked_markets, parsed_markets),
                "next_action": _bounded_coverage_next_action(category, health),
            }
        )
    parsed_markets_total = int(
        session.scalar(select(func.count(func.distinct(MarketLeg.ticker)))) or 0
    )
    parsed_legs_total = int(session.scalar(select(func.count()).select_from(MarketLeg)) or 0)
    return {
        "source": "bounded_sql_aggregate",
        "parse_failures": 0,
        "parsed_markets": parsed_markets_total,
        "parsed_legs": parsed_legs_total,
        "recommendations": [
            {
                "summary": (
                    "Market coverage was summarized from bounded SQL aggregates. "
                    "Run market-coverage-doctor separately for full row-level diagnostics."
                ),
                "command": "kalshi-bot market-coverage-doctor --output-dir reports/market_coverage",
            }
        ],
        "rows": rows,
    }


def _market_leg_category_counts(session: Session) -> dict[str, dict[str, int]]:
    rows = session.execute(
        select(
            MarketLeg.category,
            func.count().label("parsed_legs"),
            func.count(func.distinct(MarketLeg.ticker)).label("parsed_markets"),
        ).group_by(MarketLeg.category)
    )
    return {
        str(category or "unknown"): {
            "parsed_legs": int(parsed_legs or 0),
            "parsed_markets": int(parsed_markets or 0),
        }
        for category, parsed_legs, parsed_markets in rows
    }


def _link_table_counts(session: Session) -> dict[str, int]:
    return {
        "crypto": _table_count(session, CryptoMarketLink),
        "weather": _table_count(session, WeatherMarketLink),
        "economic": _table_count(session, EconomicMarketLink),
        "sports": _table_count(session, SportsMarketLink),
        "news": _table_count(session, NewsMarketLink),
    }


def _table_count(session: Session, table: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(table)) or 0)


def _distinct_market_leg_count(session: Session, category: str) -> int:
    return int(
        session.scalar(
            select(func.count(func.distinct(MarketLeg.ticker))).where(
                MarketLeg.category == category
            )
        )
        or 0
    )


def _bounded_coverage_health(
    *,
    category: str,
    parsed_markets: int,
    linked_markets: int,
) -> str:
    if parsed_markets <= 0:
        return "NO_COMPATIBLE_ACTIVE_MARKETS"
    if category not in LINKED_CATEGORIES:
        return "OBSERVED"
    if linked_markets <= 0:
        return "NEEDS_LINKS"
    if linked_markets < parsed_markets:
        return "PARTIAL"
    return "HEALTHY"


def _bounded_coverage_next_action(category: str, health: str) -> str:
    if health in {"HEALTHY", "NO_COMPATIBLE_ACTIVE_MARKETS"}:
        return "No bounded coverage action needed."
    if category == "sports":
        return (
            "Run Phase 3AH placeholder/roster watch and Phase 3Z-R2 provenance repair "
            "before Phase 3AE upgrades."
        )
    if category == "crypto":
        return "Run ingest-crypto, build-crypto-features, then link-crypto-markets."
    if category == "weather":
        return "Run weather ingestion/features, then link-weather-markets."
    if category == "economic":
        return "Load economic/calendar data, then link-economic-markets."
    if category == "news":
        return "Run news ingestion/features, then link-news-markets."
    return "Keep as observed context unless a specialized linker is added."


def _coverage_ratio(linked: int, parsed: int) -> str:
    if parsed <= 0:
        return "n/a"
    return f"{linked / parsed:.1%}"


def _phase_status() -> list[dict[str, Any]]:
    rows = []
    for phase, module_name in PHASE_MODULES:
        rows.append(
            {
                "phase": phase,
                "module": module_name,
                "installed": _module_available(module_name),
            }
        )
    return rows


def _module_available(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _sports_provenance_evidence(
    session: Session,
    *,
    scan_limit: int,
    refresh: bool,
) -> dict[str, Any]:
    del scan_limit
    if refresh:
        payload = build_sports_provenance_snapshot(session)
        payload["source"] = "fresh_sports_provenance_snapshot"
        return payload
    cached = _sports_provenance_from_market_coverage_report(MARKET_COVERAGE_REPORT_PATH)
    if cached is not None:
        return cached
    return {
        "source": "bounded_sql_aggregate",
        "parsed_sports_markets": _distinct_market_leg_count(session, "sports"),
        "sports_links": _table_count(session, SportsMarketLink),
        "sports_games": _table_count(session, SportsGame),
        "sports_features": _table_count(session, SportsFeature),
        "provenance_counts": {
            "verified_schedule": 0,
            "kalshi_event_derived": 0,
            "partial_market_derived": 0,
        },
        "partial_without_upgrade": 0,
        "partial_examples": [],
        "derived_examples": [],
        "bounded_note": (
            "Sports provenance used aggregate counts only. Run "
            "phase3ac-sports-provenance-repair for full row-level provenance."
        ),
    }


def _sports_provenance_from_market_coverage_report(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rows = payload.get("coverage_rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    sports_row = next(
        (
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("scope_key") or row.get("scope")) == "sports"
        ),
        None,
    )
    if not sports_row:
        return None
    derived = int(
        sports_row.get("derived_usable_markets")
        or sports_row.get("derived_markets")
        or 0
    )
    verified = int(sports_row.get("verified_schedule_markets") or 0)
    partial = int(sports_row.get("partial_markets") or 0)
    link_rows = int(
        sports_row.get("external_linked_markets")
        or sports_row.get("linked_markets")
        or 0
    )
    return {
        "source": f"cached_market_coverage_report:{path}",
        "parsed_sports_markets": int(sports_row.get("parsed_markets") or 0),
        "sports_links": link_rows,
        "sports_games": None,
        "sports_features": None,
        "provenance_counts": {
            "verified_schedule": verified,
            "kalshi_event_derived": derived,
            "partial_market_derived": partial,
        },
        "partial_without_upgrade": partial,
        "partial_examples": [],
        "derived_examples": [],
    }


def _learning_diagnostics_evidence(
    session: Session,
    *,
    settings: Settings,
    scan_limit: int,
    refresh: bool,
) -> dict[str, Any]:
    if refresh:
        payload = build_learning_diagnostics(
            session,
            settings=settings,
            rejection_limit=scan_limit,
            scan_limit=scan_limit,
            suggest_thresholds=True,
        )
        payload["source"] = "fresh_learning_diagnostics"
        return payload
    funnel = {
        "markets_scanned": _table_count(session, MarketLeg),
        "snapshots_available": _table_count(session, MarketSnapshot),
        "forecasts_generated": _table_count(session, Forecast),
        "forecasts_skipped": _table_count(session, ForecastSkipLog),
        "rankings_inserted": _table_count(session, MarketRanking),
        "opportunities_detected": _table_count(session, MarketOpportunity),
        "learning_candidates": _table_count(session, LearningTradeTarget),
        "paper_trades_created": _table_count(session, PaperOrder),
        "settled_paper_trades": settled_paper_trade_count(session),
    }
    return {
        "source": "bounded_sql_aggregate",
        "generated_at": utc_now().isoformat(),
        "funnel": funnel,
        "candidate_pool_size": 0,
        "top_bottleneck": {
            "reason": "BOUNDED_DIAGNOSTICS",
            "count": 0,
        },
        "bottleneck_banner": (
            "Phase 3AD used bounded aggregate learning diagnostics for fast post-refresh "
            "roadmap generation."
        ),
        "bottleneck_next_action": (
            "Run kalshi-bot learning-diagnostics separately when full rejection replay is "
            "needed."
        ),
        "rejection_breakdown": [],
        "threshold_advisor": {},
        "category_breakdown": [],
        "top_rejected_candidates": [],
        "top_usable_non_duplicate_candidates": [],
        "duplicate_cooldown": {
            "hours": settings.learning_duplicate_cooldown_hours,
            "status": "Not replayed in bounded Phase 3AD mode.",
        },
        "recommended_next_action": (
            "Use bounded Phase 3AD for roadmap freshness; run learning-diagnostics for "
            "full learning replay evidence."
        ),
        "current_thresholds": {
            "min_edge": str(settings.learning_min_edge),
            "min_score": str(settings.learning_min_opportunity_score),
        },
    }


def _self_improvement_status(session: Session) -> dict[str, Any]:
    rl = rl_status(session)
    feature = feature_discovery_status(session)
    self_eval = _self_evaluation_status(session)
    return {
        "policy": {
            "mode": "HUMAN_APPROVED_ROADMAP",
            "can_generate_prompts": True,
            "can_execute_generated_code": False,
            "can_change_trading_settings": False,
            "can_enable_live_trading": False,
            "can_enable_demo_execution": False,
        },
        "engines": {
            "reinforcement_learning": {
                "status": _engine_state(rl["run_count"], rl["latest_status"]),
                "learning_role": "offline/shadow policy evaluation from settled paper outcomes",
                "commands": ["kalshi-bot rl-evaluate --enable-research", "kalshi-bot rl-status"],
                **rl,
            },
            "feature_discovery": {
                "status": _engine_state(feature["run_count"], feature["latest_status"]),
                "learning_role": "candidate feature search from historical/paper evidence",
                "commands": [
                    "kalshi-bot feature-discovery-run --run-type INCREMENTAL",
                    "kalshi-bot feature-discovery-status",
                ],
                **feature,
            },
            "self_evaluation": {
                "status": _engine_state(self_eval["run_count"], self_eval["latest_status"]),
                "learning_role": "journal recurring failures and recommended repairs",
                "commands": [
                    "kalshi-bot self-evaluate --output reports/self_evaluation_journal.md",
                ],
                **self_eval,
            },
        },
    }


def _self_evaluation_status(session: Session) -> dict[str, Any]:
    latest = session.scalar(
        select(SelfEvaluationRun)
        .order_by(desc(SelfEvaluationRun.completed_at), desc(SelfEvaluationRun.generated_at))
        .limit(1)
    )
    return {
        "run_count": int(session.scalar(select(func.count()).select_from(SelfEvaluationRun)) or 0),
        "journal_count": int(
            session.scalar(select(func.count()).select_from(SelfEvaluationJournal)) or 0
        ),
        "finding_count": int(
            session.scalar(select(func.count()).select_from(SelfEvaluationFinding)) or 0
        ),
        "latest_run_id": latest.evaluation_run_id if latest else None,
        "latest_status": latest.status if latest else "NOT_RUN",
        "latest_completed_at": latest.completed_at.isoformat()
        if latest and latest.completed_at
        else None,
    }


def _engine_state(run_count: int, latest_status: str) -> str:
    if run_count <= 0:
        return "NEEDS_FIRST_RUN"
    if latest_status in {"COMPLETED", "READY"}:
        return "ACTIVE"
    return latest_status or "UNKNOWN"


def _report_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "path": str(path),
            "available": False,
            "summary": {},
            "recommended_next_action": "",
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        "path": str(path),
        "available": True,
        "summary": payload.get("summary", {}) if isinstance(payload, dict) else {},
        "implementation_queue": payload.get("implementation_queue", [])
        if isinstance(payload, dict)
        else [],
        "gaps": payload.get("gaps", []) if isinstance(payload, dict) else [],
        "recommended_next_action": (
            payload.get("recommended_next_action") or payload.get("next_action") or ""
        )
        if isinstance(payload, dict)
        else "",
    }


def _bottlenecks(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    settlement = evidence["settlement"]["summary"]
    learning = evidence["learning_governor"]["summary"]
    sports = evidence["sports_provenance"]
    diagnostics = evidence["learning_diagnostics"]
    coverage_rows = evidence["market_coverage"]["rows"]
    phase3az_next = _phase3az_next_implementation(evidence)
    r5_summary = evidence.get("phase3aa_r5_closed_market_capture", {}).get("summary", {})
    r5_closed_without_outcome = int(r5_summary.get("closed_without_outcome_rows") or 0)
    r5_usable_candidates = int(r5_summary.get("usable_outcome_candidate_rows") or 0)
    bottlenecks: list[dict[str, Any]] = []
    if phase3az_next:
        bottlenecks.append(
            _bottleneck(
                f"PHASE3AZ_{str(phase3az_next.get('gap_id', 'NEXT_GAP')).upper()}",
                str(phase3az_next.get("priority") or "MEDIUM"),
                str(phase3az_next.get("objective") or "Phase 3AZ found an implementation gap."),
                (
                    f"Implement {phase3az_next.get('phase')}: "
                    f"{phase3az_next.get('starter_command') or 'see Phase 3AZ report'}"
                ),
            )
        )
    if settlement["eligible_exact_settlements"] > 0:
        bottlenecks.append(
            _bottleneck(
                "EXACT_SETTLEMENTS_READY",
                "HIGH",
                (
                    f"{settlement['eligible_exact_settlements']} exact ticker "
                    "settlement(s) can realize."
                ),
                "Run Phase 3AA without dry-run, then refresh confidence and learning metrics.",
            )
        )
    if settlement["due_or_overdue"] > 0:
        bottlenecks.append(
            _bottleneck(
                "DUE_OR_OVERDUE_SETTLEMENTS",
                "MEDIUM",
                f"{settlement['due_or_overdue']} paper trade(s) are due or overdue.",
                _due_settlement_next_action(
                    closed_without_outcome=r5_closed_without_outcome,
                    usable_candidates=r5_usable_candidates,
                ),
            )
        )
    if learning["fast_settlement_candidates"] == 0:
        bottlenecks.append(
            _bottleneck(
                "NO_FAST_LEARNING_CANDIDATES",
                "MEDIUM",
                "Learning governor found no 0-24h candidates.",
                "Collect and rank more short-dated markets before starting new learning cycles.",
            )
        )
    improvement = evidence["self_improvement"]["engines"]
    if improvement["reinforcement_learning"]["run_count"] == 0:
        bottlenecks.append(
            _bottleneck(
                "RL_POLICY_NOT_EVALUATED",
                "LOW",
                "Phase 3S has not produced an offline/shadow policy evaluation yet.",
                "Run rl-evaluate after enough paper outcomes settle.",
            )
        )
    if improvement["feature_discovery"]["run_count"] == 0:
        bottlenecks.append(
            _bottleneck(
                "FEATURE_DISCOVERY_NOT_RUN",
                "LOW",
                "Phase 3Q has not searched for new feature candidates yet.",
                "Run feature-discovery-run once paper/forecast history is current.",
            )
        )
    if improvement["self_evaluation"]["run_count"] == 0:
        bottlenecks.append(
            _bottleneck(
                "SELF_EVALUATION_NOT_RUN",
                "LOW",
                "Phase 3P has not written a self-evaluation journal yet.",
                "Run self-evaluate to turn diagnostics into recurring lessons.",
            )
        )
    if (
        settlement["eligible_exact_settlements"] > 0
        and improvement["reinforcement_learning"]["run_count"] == 0
    ):
        bottlenecks.append(
            _bottleneck(
                "SETTLED_OUTCOMES_NEED_RL_REPLAY",
                "MEDIUM",
                "Settled outcomes are available before any offline RL replay.",
                "Realize paper outcomes, then run rl-evaluate in research mode.",
            )
        )
    if diagnostics["funnel"]["settled_paper_trades"] == 0:
        bottlenecks.append(
            _bottleneck(
                "NO_SETTLED_LEARNING_REWARDS",
                "MEDIUM",
                "Learning Mode has no settled paper trades, so RL rewards are not mature yet.",
                (
                    "Prioritize 0-24h markets and rerun settlement watcher before "
                    "training policy gates."
                ),
            )
        )
    if sports["partial_without_upgrade"] > 0:
        bottlenecks.append(
            _bottleneck(
                "PARTIAL_SPORTS_PROVENANCE",
                "MEDIUM",
                f"{sports['partial_without_upgrade']} sports link(s) remain partial.",
                "Add verified sports schedule/team ingestion to upgrade provenance.",
            )
        )
    diag_bottleneck = diagnostics.get("top_bottleneck", {})
    if diag_bottleneck and diag_bottleneck.get("reason") not in {None, "none", "NO_REJECTIONS"}:
        bottlenecks.append(
            _bottleneck(
                f"LEARNING_{diag_bottleneck.get('reason', 'BOTTLENECK')}",
                "LOW",
                diagnostics.get("bottleneck_banner", "Learning diagnostics found a bottleneck."),
                diagnostics.get("bottleneck_next_action", diagnostics["recommended_next_action"]),
            )
        )
    for row in coverage_rows:
        if row["health"] not in {"HEALTHY", "NO_COMPATIBLE_ACTIVE_MARKETS"}:
            bottlenecks.append(
                _bottleneck(
                    f"COVERAGE_{row['scope'].upper()}_{row['health']}",
                    "LOW",
                    f"{row['scope']} coverage health is {row['health']}.",
                    row["next_action"],
                )
            )
    return bottlenecks or [
        _bottleneck(
            "NO_BLOCKING_BOTTLENECK",
            "INFO",
            "No blocking bottleneck was detected from current local reports.",
            "Continue paper-only learning and settlement realization cadence.",
        )
    ]


def _bottleneck(code: str, severity: str, evidence: str, next_action: str) -> dict[str, str]:
    return {
        "code": code,
        "severity": severity,
        "evidence": evidence,
        "next_action": next_action,
    }


def _phase3az_next_implementation(evidence: dict[str, Any]) -> dict[str, Any] | None:
    report = evidence.get("phase3az_gap_analysis") or {}
    queue = report.get("implementation_queue") or []
    if not isinstance(queue, list):
        return None
    for row in queue:
        if isinstance(row, dict):
            return row
    return None


def _due_settlement_next_action(
    *,
    closed_without_outcome: int,
    usable_candidates: int,
) -> str:
    if closed_without_outcome and usable_candidates == 0:
        return (
            "R5 found closed exact-market payloads with no supported outcome fields; "
            "keep exact-ticker watch active and do not realize from siblings."
        )
    if usable_candidates:
        return "Rerun Phase 3AA-R2, then dry-run Phase 3AA realization."
    return "Run the Phase 3AA-R2 exact-ticker settlement harvest before realizing paper P&L."


def _settlement_harvest_priority(
    *,
    due: int,
    eligible: int,
    closed_without_outcome: int,
    usable_candidates: int,
) -> int:
    if eligible:
        return 40
    if due <= 0:
        return 40
    if closed_without_outcome and usable_candidates == 0:
        return 62
    return 88


def _settlement_harvest_next_command(
    *,
    closed_without_outcome: int,
    usable_candidates: int,
) -> str:
    if closed_without_outcome and usable_candidates == 0:
        return "kalshi-bot phase3ay-health-refresh --cycles 1 --interval-seconds 0"
    if usable_candidates:
        return "kalshi-bot phase3aa-r2-exact-settlement-harvest --output-dir reports/phase3aa_r2"
    return "kalshi-bot phase3aa-r2-exact-settlement-harvest --output-dir reports/phase3aa_r2"


def _settlement_harvest_blocked_by(
    *,
    eligible: int,
    closed_without_outcome: int,
    usable_candidates: int,
) -> str:
    if eligible:
        return "none"
    if closed_without_outcome and usable_candidates == 0:
        return "R5 found closed exact-market payloads with no supported outcome field"
    return "needs exact ticker source settlement evidence"


def _improvement_candidates(
    evidence: dict[str, Any],
    bottlenecks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    settlement = evidence["settlement"]["summary"]
    diagnostics = evidence["learning_diagnostics"]
    learning = evidence["learning_governor"]["summary"]
    improvement = evidence["self_improvement"]["engines"]
    phase3ah_sports = evidence.get("phase3ah_sports_evidence", {}).get("summary", {})
    phase3ah_roster = evidence.get("phase3ah_roster_verification", {}).get("summary", {})
    phase3aa_r5 = evidence.get("phase3aa_r5_closed_market_capture", {}).get("summary", {})
    placeholder_resolution = evidence.get(
        "phase3ah_round_placeholder_resolution",
        {},
    ).get("summary", {})
    placeholder_rows = int(phase3ah_sports.get("round_placeholder_resolution_rows") or 0)
    roster_rework_rows = int(phase3ah_roster.get("rework_rows") or 0)
    still_placeholder_rows = int(placeholder_resolution.get("still_placeholder_rows") or 0)
    r5_closed_without_outcome = int(phase3aa_r5.get("closed_without_outcome_rows") or 0)
    r5_usable_candidates = int(phase3aa_r5.get("usable_outcome_candidate_rows") or 0)
    settlement_harvest_blocked_by = _settlement_harvest_blocked_by(
        eligible=settlement["eligible_exact_settlements"],
        closed_without_outcome=r5_closed_without_outcome,
        usable_candidates=r5_usable_candidates,
    )
    settlement_harvest_next_command = _settlement_harvest_next_command(
        closed_without_outcome=r5_closed_without_outcome,
        usable_candidates=r5_usable_candidates,
    )
    phase3az_next = _phase3az_next_implementation(evidence)
    candidates = []
    if phase3az_next:
        priority = 94 if phase3az_next.get("priority") == "HIGH" else 86
        candidates.append(
            {
                "id": "phase3az_next_gap",
                "title": str(phase3az_next.get("objective") or "Implement Phase 3AZ next gap"),
                "model_family": "post_refresh_gap_analysis",
                "priority": priority,
                "why": "Phase 3AZ is the post-refresh gap arbiter and found actionable code work.",
                "next_command": str(
                    phase3az_next.get("starter_command")
                    or "kalshi-bot phase3az-gap-analysis --output-dir reports/phase3az"
                ),
                "blocked_by": "none",
            }
        )
    candidates.extend(
        [
            {
                "id": "paper_market_health_refresh",
            "title": "Keep paper and market health fresh automatically",
            "model_family": "health_refresh",
            "priority": 92 if settlement["due_or_overdue"] else 72,
            "why": (
                "A bounded refresh loop can keep exact-ticker settlement harvests, "
                "paper P&L realization, market collection, market coverage, placeholder "
                "watch, and the roadmap current without manual reruns."
            ),
            "next_command": (
                "kalshi-bot phase3ay-health-refresh --cycles 999 "
                "--interval-seconds 300 --all-markets"
            ),
            "blocked_by": "none",
        },
        {
            "id": "settlement_outcome_feedback",
            "title": "Realize exact paper outcomes into confidence and RL rewards",
            "model_family": "reinforcement_learning + model_confidence",
            "priority": 90 if settlement["eligible_exact_settlements"] else 55,
            "why": (
                "Settled paper outcomes are the reward signal. Without them, the bot can only "
                "guess which policies are improving."
            ),
            "next_command": "kalshi-bot phase3aa-realize --dry-run --no-sync-settlements",
            "blocked_by": "no exact settled paper trades"
            if settlement["eligible_exact_settlements"] == 0
            else "human approval to realize exact outcomes",
        },
        {
            "id": "exact_settlement_harvest",
            "title": "Harvest exact ticker settlement evidence for due paper orders",
            "model_family": "settlement_reconciliation",
            "priority": _settlement_harvest_priority(
                due=settlement["due_or_overdue"],
                eligible=settlement["eligible_exact_settlements"],
                closed_without_outcome=r5_closed_without_outcome,
                usable_candidates=r5_usable_candidates,
            ),
            "why": (
                "Due paper trades cannot become reward signals until the local settlement "
                "table has exact ticker outcomes."
            ),
            "next_command": settlement_harvest_next_command,
            "blocked_by": settlement_harvest_blocked_by,
        },
        {
            "id": "fast_settlement_router",
            "title": "Route learning toward markets that settle soonest",
            "model_family": "learning_governor",
            "priority": 85 if learning["fast_settlement_candidates"] == 0 else 65,
            "why": "Faster settlements create faster reward feedback for model improvement.",
            "next_command": "kalshi-bot phase3ab-learning-governor",
            "blocked_by": "needs more fresh short-dated candidates"
            if learning["fast_settlement_candidates"] == 0
            else "none",
        },
        {
            "id": "feature_discovery",
            "title": "Search for new predictive features from paper evidence",
            "model_family": "feature_discovery",
            "priority": 80 if improvement["feature_discovery"]["run_count"] == 0 else 45,
            "why": "Feature discovery can propose data/features that reduce recurring bottlenecks.",
            "next_command": "kalshi-bot feature-discovery-run --run-type INCREMENTAL",
            "blocked_by": "needs enough historical rows"
            if diagnostics["funnel"]["settled_paper_trades"] == 0
            else "none",
        },
        {
            "id": "rl_policy_replay",
            "title": "Evaluate policy actions with offline/shadow reinforcement learning",
            "model_family": "reinforcement_learning",
            "priority": 80
            if diagnostics["funnel"]["settled_paper_trades"] > 0
            and improvement["reinforcement_learning"]["run_count"] == 0
            else 50,
            "why": "RL should learn from finalized paper rewards before it advises policy gates.",
            "next_command": "kalshi-bot rl-evaluate --enable-research",
            "blocked_by": "needs settled paper rewards"
            if diagnostics["funnel"]["settled_paper_trades"] == 0
            else "none",
        },
        {
            "id": "sports_round_placeholder_resolution",
            "title": "Watch sports bracket placeholders until source schedules name teams",
            "model_family": "market_linking",
            "priority": 78 if placeholder_rows else 35,
            "why": (
                "Bracket placeholder teams must become real teams before Phase 3AE can "
                "safely apply the clean team + time + market-type gate."
            ),
            "next_command": (
                "kalshi-bot phase3ah-sports-placeholder-watch "
                "--output-dir reports/phase3ah_sports"
            ),
            "blocked_by": "source still has bracket placeholders"
            if still_placeholder_rows
            else "needs source resolution run"
            if placeholder_rows
            else "none",
        },
        {
            "id": "sports_provenance_repair",
            "title": "Verify sports player/participant roster evidence",
            "model_family": "market_linking",
            "priority": 75 if roster_rework_rows else 35,
            "why": (
                "Player props require roster/team evidence before Phase 3AE can safely "
                "upgrade verified links."
            ),
            "next_command": (
                "kalshi-bot phase3ah-roster-participant-verification "
                "--output-dir reports/phase3ah_sports"
            ),
            "blocked_by": "needs verified roster/team evidence"
            if roster_rework_rows
            else "none",
        },
            {
                "id": "self_evaluation_journal",
            "title": "Write recurring failure journal and next-build rationale",
            "model_family": "self_evaluation",
            "priority": 70 if improvement["self_evaluation"]["run_count"] == 0 else 40,
            "why": "Self-evaluation turns repeated symptoms into stable engineering lessons.",
            "next_command": "kalshi-bot self-evaluate --output reports/self_evaluation_journal.md",
                "blocked_by": "none",
            },
        ]
    )
    bottleneck_codes = {row["code"] for row in bottlenecks}
    for candidate in candidates:
        candidate["related_bottlenecks"] = sorted(
            code
            for code in bottleneck_codes
            if _candidate_matches_bottleneck(candidate["id"], code)
        )
    return sorted(candidates, key=lambda row: (-int(row["priority"]), row["id"]))


def _candidate_matches_bottleneck(candidate_id: str, code: str) -> bool:
    mapping = {
        "settlement_outcome_feedback": ("SETTLEMENT", "OUTCOME"),
        "exact_settlement_harvest": ("SETTLEMENT",),
        "fast_settlement_router": ("FAST", "SLOW", "LEARNING"),
        "feature_discovery": ("FEATURE", "COVERAGE"),
        "rl_policy_replay": ("RL", "REWARD"),
        "sports_provenance_repair": ("SPORTS",),
        "sports_round_placeholder_resolution": ("SPORTS",),
        "phase3az_next_gap": ("PHASE3AZ",),
        "self_evaluation_journal": ("SELF_EVALUATION",),
    }
    return any(fragment in code for fragment in mapping.get(candidate_id, ()))


def _choose_next_phase(
    evidence: dict[str, Any],
    bottlenecks: list[dict[str, Any]],
) -> dict[str, Any]:
    codes = {row["code"] for row in bottlenecks}
    if "EXACT_SETTLEMENTS_READY" in codes:
        return {
            "phase": "3AE",
            "title": "Outcome Feedback Integrator",
            "objective": (
                "Turn exact settled paper outcomes into model confidence, learning metrics, "
                "and morning lessons."
            ),
            "primary_reason": "Exact settlements are ready to realize.",
        }
    phase3az_next = _phase3az_next_implementation(evidence)
    if phase3az_next:
        return {
            "phase": str(phase3az_next.get("phase") or "NEXT"),
            "title": str(phase3az_next.get("objective") or "Post-Refresh Gap Repair"),
            "objective": (
                str(phase3az_next.get("objective") or "Implement the next Phase 3AZ gap.")
                + " Keep the work paper-only and do not bypass domain-specific safety gates."
            ),
            "primary_reason": (
                f"Phase 3AZ recommends {phase3az_next.get('gap_id')} from the refreshed "
                "gap-analysis implementation queue."
            ),
        }
    if "PARTIAL_SPORTS_PROVENANCE" in codes:
        verified_count = int(
            evidence["sports_provenance"].get("provenance_counts", {}).get(
                "verified_schedule",
                0,
            )
            or 0
        )
        if verified_count <= 0:
            return {
                "phase": "3AF",
                "title": "Verified Sports Schedule Ingestion Bootstrap",
                "objective": (
                    "Fetch and ingest verified sports schedule/team rows so Phase 3AE can "
                    "upgrade partial sports links safely."
                ),
                "primary_reason": (
                    "Sports partial links remain, but no verified sports schedule provenance "
                    "exists yet."
                ),
            }
        phase3ah_summary = evidence.get("phase3ah_sports_evidence", {}).get("summary", {})
        placeholder_rows = int(phase3ah_summary.get("round_placeholder_resolution_rows") or 0)
        if placeholder_rows > 0:
            roster_summary = evidence.get("phase3ah_roster_verification", {}).get(
                "summary",
                {},
            )
            roster_rework_rows = int(roster_summary.get("rework_rows") or 0)
            placeholder_summary = evidence.get(
                "phase3ah_round_placeholder_resolution",
                {},
            ).get("summary", {})
            still_placeholders = int(placeholder_summary.get("still_placeholder_rows") or 0)
            if still_placeholders:
                reason = (
                    f"{still_placeholders} source row(s) still list bracket placeholders; "
                    "rerun the resolver after bracket advancement."
                )
            else:
                reason = (
                    f"{placeholder_rows} round-placeholder schedule row(s) still block the "
                    "clean team + time + market-type gate."
                )
            return {
                "phase": "3AH",
                "title": "Sports Placeholder Watch"
                if roster_rework_rows <= 0
                else "Sports Placeholder Resolution + Roster Verification",
                "objective": (
                    "Keep round-placeholder schedule rows blocked until source schedules "
                    "name real teams, then rerun Phase 3AE after the clean team + time + "
                    "market-type gate can evaluate them."
                )
                if roster_rework_rows <= 0
                else (
                    "Resolve round-placeholder schedule rows and verify the remaining "
                    "player/participant roster evidence before Phase 3AE link upgrades."
                ),
                "primary_reason": reason,
            }
        return {
            "phase": "3AH",
            "title": "Sports Roster / Participant Verification",
            "objective": (
                "Validate Phase 3AH roster template rows into verified player/participant "
                "evidence before Phase 3AE upgrades player props."
            ),
            "primary_reason": (
                "Verified schedule windows exist, but player-prop rows remain blocked by "
                "missing roster/participant mappings."
            ),
        }
    if "NO_FAST_LEARNING_CANDIDATES" in codes:
        return {
            "phase": "3AE",
            "title": "Fast Market Harvester",
            "objective": (
                "Collect, rank, and route more 0-24h markets before creating new paper trades."
            ),
            "primary_reason": "Learning needs more fast-settlement candidates.",
        }
    if "DUE_OR_OVERDUE_SETTLEMENTS" in codes:
        r5_summary = evidence.get("phase3aa_r5_closed_market_capture", {}).get("summary", {})
        r5_closed_without_outcome = int(r5_summary.get("closed_without_outcome_rows") or 0)
        r5_usable_candidates = int(r5_summary.get("usable_outcome_candidate_rows") or 0)
        if r5_closed_without_outcome and r5_usable_candidates == 0:
            return {
                "phase": "3AY",
                "title": "Exact Settlement Watch Refresh",
                "objective": (
                    "Keep exact-ticker settlement harvesting fresh while closed source "
                    "payloads continue to expose no supported outcome fields."
                ),
                "primary_reason": (
                    "Phase 3AA-R5 found closed exact-market payloads without usable "
                    "settlement outcome fields."
                ),
            }
        return {
            "phase": "3AA-R2",
            "title": "Exact Settlement Harvest + Paper P&L Realization",
            "objective": (
                "Fetch due paper order markets by exact ticker, write only exact settlement "
                "evidence, then realize paper P&L only if the doctor finds eligible outcomes."
            ),
            "primary_reason": "There are due or overdue paper trades to monitor.",
        }
    if evidence["learning_governor"]["summary"]["fast_settlement_candidates"] > 0:
        return {
            "phase": "3AE",
            "title": "Paper Learning Cadence Runner",
            "objective": (
                "Run safe paper-only learning cycles from fast-settlement candidates and "
                "summarize outcomes."
            ),
            "primary_reason": "Fast-settlement candidates are available.",
        }
    return {
        "phase": "3AE",
        "title": "System Evidence Consolidator",
        "objective": "Consolidate reports and keep the roadmap loop moving safely.",
        "primary_reason": "No dominant bottleneck was detected.",
    }


def _implementation_prompt(
    next_phase: dict[str, Any],
    evidence: dict[str, Any],
    bottlenecks: list[dict[str, Any]],
    improvement_candidates: list[dict[str, Any]],
) -> str:
    settlement = evidence["settlement"]["summary"]
    learning = evidence["learning_governor"]["summary"]
    sports = evidence["sports_provenance"]
    coverage = evidence["market_coverage"]
    phase3ah_sports = evidence.get("phase3ah_sports_evidence", {}).get("summary", {})
    phase3ah_roster = evidence.get("phase3ah_roster_verification", {}).get("summary", {})
    phase3aa_r5 = evidence.get("phase3aa_r5_closed_market_capture", {}).get("summary", {})
    phase3az = evidence.get("phase3az_gap_analysis", {})
    phase3az_queue = phase3az.get("implementation_queue") or []
    placeholder_resolution = evidence.get(
        "phase3ah_round_placeholder_resolution",
        {},
    ).get("summary", {})
    placeholder_watch = evidence.get("phase3ah_placeholder_watch", {}).get("summary", {})
    placeholder_safe_rows = placeholder_resolution.get("safe_to_apply_rows", 0)
    placeholder_still_rows = placeholder_resolution.get("still_placeholder_rows", 0)
    placeholder_watch_gate = (
        evidence.get("phase3ah_placeholder_watch", {})
        .get("summary", {})
        .get("phase3ae_gate_status", "")
    )
    bottleneck_lines = "\n".join(
        f"- {row['code']} ({row['severity']}): {row['evidence']} Next: {row['next_action']}"
        for row in bottlenecks
    )
    improvement_lines = "\n".join(
        (
            f"- {row['title']} | model: {row['model_family']} | priority: {row['priority']} | "
            f"blocked_by: {row['blocked_by']} | next: {row['next_command']}"
        )
        for row in improvement_candidates[:6]
    )
    title = str(next_phase["title"]).rstrip(".")
    return f"""Build: Phase {next_phase['phase']}: {title}.

Objective:
{next_phase['objective']}

Safety:
- Do NOT add live trading.
- Do NOT enable demo execution.
- Keep Learning Mode PAPER ONLY.
- Do not submit orders to any exchange.
- Generated code must require explicit human approval for any future execution expansion.
- Reinforcement learning must remain offline/shadow until explicitly approved.
- The bot may generate reports/prompts, but must not auto-edit or auto-deploy code.

Current evidence:
- Exact settlement eligible trades: {settlement['eligible_exact_settlements']}
- Active unsettled trades: {settlement['active_unsettled']}
- Due or overdue trades: {settlement['due_or_overdue']}
- ETA buckets: {settlement['eta_buckets']}
- Phase 3AA-R5 closed/no-outcome rows: {phase3aa_r5.get('closed_without_outcome_rows', 0)}
- Phase 3AA-R5 usable outcome candidates: {phase3aa_r5.get('usable_outcome_candidate_rows', 0)}
- Fast settlement candidates: {learning['fast_settlement_candidates']}
- Slow settlement avoids: {learning['slow_settlement_avoids']}
- Sports partial links without upgrade: {sports['partial_without_upgrade']}
- Sports provenance counts: {sports['provenance_counts']}
- Phase 3AH round placeholder rows: {phase3ah_sports.get('round_placeholder_resolution_rows', 0)}
- Phase 3AH placeholder resolver safe rows: {placeholder_safe_rows}
- Phase 3AH placeholder resolver still placeholders: {placeholder_still_rows}
- Phase 3AH placeholder watch rows: {placeholder_watch.get('placeholder_rows_reviewed', 0)}
- Phase 3AH placeholder watch gate: {placeholder_watch_gate or 'unknown'}
- Phase 3AH roster rework rows: {phase3ah_roster.get('rework_rows', 0)}
- Phase 3AZ implementation queue: {phase3az_queue}
- Phase 3AZ recommended next action: {phase3az.get('recommended_next_action', '')}
- Market coverage recommendations: {coverage['recommendations']}

Detected bottlenecks:
{bottleneck_lines}

Self-improvement candidates:
{improvement_lines}

Tasks:
1. Inspect the current Phase 3AA, 3AB, 3AC, and 3AD reports.
2. Use paper outcomes, feature discovery, self-evaluation, and offline/shadow RL as evidence.
3. Implement only the smallest safe layer needed for the objective above.
4. Preserve exact-ticker-only settlement realization.
5. Preserve paper-only Learning Mode and execution blocks.
6. Prefer `phase3ay-health-refresh` when the task is freshness/health automation.
7. Add or update CLI command(s) and Markdown/JSON reports.
8. Add focused tests for the new behavior and safety guarantees.
9. Run targeted pytest and `ruff check .`.

Acceptance commands:
```bash
source .venv/bin/activate
kalshi-bot phase3aa-realize --dry-run --no-sync-settlements
kalshi-bot phase3ay-health-refresh --cycles 1 --interval-seconds 0
kalshi-bot phase3ay-status
kalshi-bot phase3bb-domain-readiness --output-dir reports/phase3bb
kalshi-bot phase3bb-r2-general-candidate-routing --output-dir reports/phase3bb_r2
kalshi-bot phase3bb-r2-general-source-intake --output-dir reports/phase3bb_r2_sources
kalshi-bot phase3bb-r2-general-source-evidence --output-dir reports/phase3bb_r2_sources
kalshi-bot phase3bb-r2-general-source-availability --output-dir reports/phase3bb_r2_sources
kalshi-bot phase3bb-r3-general-reclassification --output-dir reports/phase3bb_r3
kalshi-bot phase3az-gap-analysis --output-dir reports/phase3az
kalshi-bot phase3aa-r2-exact-settlement-harvest --output-dir reports/phase3aa_r2
kalshi-bot phase3ab-learning-governor
kalshi-bot phase3ac-sports-provenance-repair
kalshi-bot phase3af-sports-schedule-bootstrap --leagues MLB,WNBA,SOCCER --days-ahead 14 --ingest
kalshi-bot phase3ag-sports-ambiguity-coverage --output-dir reports/phase3ag
kalshi-bot phase3ag-sports-link-repair-pass --output-dir reports/phase3ag
kalshi-bot phase3ah-sports-evidence-backfill --output-dir reports/phase3ah_sports \
  --fetch-schedules --ingest-schedules
kalshi-bot phase3ah-round-placeholder-resolution --output-dir reports/phase3ah_sports
kalshi-bot phase3ah-sports-placeholder-watch --output-dir reports/phase3ah_sports
kalshi-bot phase3ah-roster-participant-verification --output-dir reports/phase3ah_sports
kalshi-bot phase3ae-verified-sports-connector
kalshi-bot feature-discovery-status
kalshi-bot rl-status
kalshi-bot phase-orchestrator --analyze \
  --output reports/phase_orchestrator.md \
  --next-prompt prompts/next_phase.md \
  --scan-limit 100
ruff check .
```

Final response should summarize:
- files changed
- commands added
- tests run
- latest bottleneck
- next recommended command
"""


def _recommended_loop(next_phase: dict[str, Any]) -> list[str]:
    return [
        "kalshi-bot phase3aa-realize --dry-run --no-sync-settlements",
        "kalshi-bot phase3ay-health-refresh --cycles 1 --interval-seconds 0",
        "kalshi-bot phase3ay-status",
        "kalshi-bot phase3bb-domain-readiness --output-dir reports/phase3bb",
        "kalshi-bot phase3bb-r2-general-candidate-routing --output-dir reports/phase3bb_r2",
        (
            "kalshi-bot phase3bb-r2-general-source-intake "
            "--output-dir reports/phase3bb_r2_sources"
        ),
        (
            "kalshi-bot phase3bb-r2-general-source-evidence "
            "--output-dir reports/phase3bb_r2_sources"
        ),
        (
            "kalshi-bot phase3bb-r2-general-source-availability "
            "--output-dir reports/phase3bb_r2_sources"
        ),
        "kalshi-bot phase3bb-r3-general-reclassification --output-dir reports/phase3bb_r3",
        "kalshi-bot phase3az-gap-analysis --output-dir reports/phase3az",
        "kalshi-bot phase3aa-r2-exact-settlement-harvest --output-dir reports/phase3aa_r2",
        "kalshi-bot phase3ab-learning-governor",
        "kalshi-bot phase3ac-sports-provenance-repair",
        (
            "kalshi-bot phase3af-sports-schedule-bootstrap "
            "--leagues MLB,WNBA,SOCCER --days-ahead 14 --ingest"
        ),
        "kalshi-bot phase3ag-sports-ambiguity-coverage --output-dir reports/phase3ag",
        "kalshi-bot phase3ag-sports-link-repair-pass --output-dir reports/phase3ag",
        (
            "kalshi-bot phase3ah-sports-evidence-backfill "
            "--output-dir reports/phase3ah_sports --fetch-schedules --ingest-schedules"
        ),
        (
            "kalshi-bot phase3ah-round-placeholder-resolution "
            "--output-dir reports/phase3ah_sports"
        ),
        (
            "kalshi-bot phase3ah-sports-placeholder-watch "
            "--output-dir reports/phase3ah_sports"
        ),
        (
            "kalshi-bot phase3ah-roster-participant-verification "
            "--output-dir reports/phase3ah_sports"
        ),
        "kalshi-bot phase3ae-verified-sports-connector",
        "kalshi-bot feature-discovery-status",
        "kalshi-bot rl-status",
        (
            "kalshi-bot phase-orchestrator --analyze "
            "--output reports/phase_orchestrator.md --next-prompt prompts/next_phase.md "
            "--scan-limit 100"
        ),
        f"Review prompts/next_phase.md before implementing Phase {next_phase['phase']}.",
    ]


def _render_markdown(payload: dict[str, Any], next_prompt_path: Path) -> str:
    next_phase = payload["next_phase"]
    evidence = payload["evidence"]
    lines = [
        "# Phase 3AD Phase Orchestrator + Auto Roadmap Engine",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Scan limit: {payload['bounded_runtime']['scan_limit']}",
        (
            "- Market coverage source: "
            f"{payload['bounded_runtime']['market_coverage_source']}"
        ),
        (
            "- Sports provenance source: "
            f"{payload['bounded_runtime']['sports_provenance_source']}"
        ),
        (
            "- Learning diagnostics source: "
            f"{payload['bounded_runtime']['learning_diagnostics_source']}"
        ),
        "- This command does not execute generated implementation prompts.",
        "- Live trading: blocked",
        "- Demo execution: blocked",
        "",
        "## Current State",
        "",
        (
            "- Exact settlements eligible: "
            f"{evidence['settlement']['summary']['eligible_exact_settlements']}"
        ),
        f"- Active unsettled trades: {evidence['settlement']['summary']['active_unsettled']}",
        f"- Due or overdue trades: {evidence['settlement']['summary']['due_or_overdue']}",
        (
            "- Fast settlement candidates: "
            f"{evidence['learning_governor']['summary']['fast_settlement_candidates']}"
        ),
        (
            "- Sports partial links without upgrade: "
            f"{evidence['sports_provenance']['partial_without_upgrade']}"
        ),
        "",
        "## Bottlenecks",
        "",
        "| Code | Severity | Evidence | Next action |",
        "| --- | --- | --- | --- |",
    ]
    for row in payload["bottlenecks"]:
        lines.append(
            f"| {row['code']} | {row['severity']} | {_md(row['evidence'])} | "
            f"{_md(row['next_action'])} |"
        )
    lines.extend(
        [
            "",
            "## Self-Improvement Engines",
            "",
            "| Engine | Status | Role | Latest |",
            "| --- | --- | --- | --- |",
        ]
    )
    for name, row in evidence["self_improvement"]["engines"].items():
        lines.append(
            f"| {name} | {row['status']} | {_md(row['learning_role'])} | "
            f"{row.get('latest_status', 'n/a')} |"
        )
    lines.extend(
        [
            "",
            "## AI Build Candidates",
            "",
            "| Priority | Candidate | Model family | Blocked by | Next command |",
            "| ---: | --- | --- | --- | --- |",
        ]
    )
    for row in payload["improvement_candidates"]:
        lines.append(
            f"| {row['priority']} | {_md(row['title'])} | {row['model_family']} | "
            f"{_md(row['blocked_by'])} | `{row['next_command']}` |"
        )
    lines.extend(
        [
            "",
            "## Automation Guardrails",
            "",
            "- Generates reports and next-phase prompts only.",
            "- Does not execute generated code.",
            "- Does not change live/demo execution settings.",
            "- Reinforcement learning remains offline/shadow unless explicitly approved.",
        ]
    )
    lines.extend(
        [
            "",
            "## Next Recommended Phase",
            "",
            f"- Phase: {next_phase['phase']}",
            f"- Title: {next_phase['title']}",
            f"- Objective: {next_phase['objective']}",
            f"- Reason: {next_phase['primary_reason']}",
            f"- Prompt written to: `{next_prompt_path}`",
            "",
            "## Loop Commands",
            "",
            "```bash",
        ]
    )
    lines.extend(payload["recommended_loop"])
    lines.extend(
        [
            "```",
            "",
            "## Installed Phase Modules",
            "",
            "| Phase | Module | Installed |",
            "| --- | --- | --- |",
        ]
    )
    for row in evidence["phase_status"]:
        lines.append(f"| {row['phase']} | `{row['module']}` | {row['installed']} |")
    lines.extend(["", "## Generated Prompt Preview", "", "```text"])
    lines.append(payload["implementation_prompt"].strip())
    lines.extend(["```", ""])
    return "\n".join(lines)


def _md(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
