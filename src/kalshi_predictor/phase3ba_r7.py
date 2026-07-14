from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import (
    database_url_from_settings,
    redact_database_url,
    sqlite_path_from_url,
)
from kalshi_predictor.data.db import describe_db_location
from kalshi_predictor.data.schema import (
    CryptoMarketLink,
    EconomicMarketLink,
    Market,
    MarketLeg,
    NewsMarketLink,
    SportsMarketLink,
    WeatherMarketLink,
)
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.time import utc_now

PHASE3BA_R7_VERSION = "phase3ba_r7_composite_market_parking_plan_v1"
DISPLAY_CATEGORIES = (
    "crypto",
    "weather",
    "economic",
    "sports",
    "news",
    "cross_category",
    "general",
    "unknown",
)
LINK_TABLE_BY_CATEGORY: dict[str, Any] = {
    "crypto": CryptoMarketLink,
    "weather": WeatherMarketLink,
    "economic": EconomicMarketLink,
    "sports": SportsMarketLink,
    "news": NewsMarketLink,
}
SPORTS_COMPOSITE_PREFIX = "KXMVESPORTSMULTIGAME"
CROSS_CATEGORY_COMPOSITE_PREFIX = "KXMVECROSSCATEGORY"


@dataclass(frozen=True)
class Phase3BAR7ArtifactSet:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    rows_csv_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3ba_r7_composite_market_plan_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ba_r7"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> Phase3BAR7ArtifactSet:
    payload = build_phase3ba_r7_composite_market_plan(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "composite_market_plan.md"
    rows_csv_path = output_dir / "composite_market_rows.csv"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_plan_markdown(payload), encoding="utf-8")
    _write_rows_csv(rows_csv_path, payload["composite_rows"])
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [executive_summary_path, markdown_path, rows_csv_path, next_actions_path],
    )
    return Phase3BAR7ArtifactSet(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        rows_csv_path=rows_csv_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3ba_r7_composite_market_plan(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ba_r7"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> dict[str, Any]:
    del reports_dir
    generated_at = utc_now()
    resolved = settings or get_settings()
    rows = _composite_rows(session)
    summary = _summary(rows)
    payload = {
        **_metadata(
            session,
            settings=resolved,
            generated_at=generated_at.isoformat(),
            command_args=command_args or [],
        ),
        "phase": "3BA-R7",
        "phase_version": PHASE3BA_R7_VERSION,
        "mode": "PAPER_READ_ONLY_COMPOSITE_MARKET_PARKING_PLAN",
        "output_dir": str(output_dir),
        "summary": summary,
        "composite_rows": rows,
        "future_support_plan": _future_support_plan(summary),
        "acceptance": _acceptance(summary),
        "operator_guardrails": _operator_guardrails(),
    }
    return payload


def _composite_rows(session: Session) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for category in DISPLAY_CATEGORIES:
        table = LINK_TABLE_BY_CATEGORY.get(category)
        filters: list[Any] = [
            MarketLeg.category == category,
            _unsupported_composite_predicate(),
        ]
        if table is not None:
            filters.append(~MarketLeg.ticker.in_(select(table.ticker).distinct()))
        statement = (
            select(
                MarketLeg.ticker,
                MarketLeg.category,
                Market.title,
                Market.event_ticker,
                Market.series_ticker,
                Market.status,
                Market.close_time,
                func.count(MarketLeg.id).label("parsed_legs"),
            )
            .join(Market, Market.ticker == MarketLeg.ticker)
            .where(*filters)
            .group_by(
                MarketLeg.ticker,
                MarketLeg.category,
                Market.title,
                Market.event_ticker,
                Market.series_ticker,
                Market.status,
                Market.close_time,
            )
            .order_by(MarketLeg.category, MarketLeg.ticker)
        )
        for row in session.execute(statement):
            (
                ticker,
                category_value,
                title,
                event_ticker,
                series_ticker,
                status,
                close_time,
                parsed_legs,
            ) = row
            composite_type = _classify_composite_type(ticker, event_ticker, series_ticker)
            rows.append(
                {
                    "ticker": ticker,
                    "category": category_value,
                    "composite_type": composite_type,
                    "market_title": title,
                    "event_ticker": event_ticker,
                    "series_ticker": series_ticker,
                    "market_status": status,
                    "close_time": (
                        close_time.isoformat() if hasattr(close_time, "isoformat") else close_time
                    ),
                    "parsed_legs": int(parsed_legs or 0),
                    "parking_status": _parking_status(composite_type),
                    "excluded_from_single_market_remediation": True,
                    "normal_link_remediation_allowed": False,
                    "exact_component_evidence_exists": False,
                    "component_evidence_status": _component_evidence_status(category_value),
                    "future_support_required": _future_support_required(composite_type),
                }
            )
    return rows


def _unsupported_composite_predicate() -> Any:
    sports_family = f"{SPORTS_COMPOSITE_PREFIX}%"
    cross_category_family = f"{CROSS_CATEGORY_COMPOSITE_PREFIX}%"
    return (
        func.upper(func.coalesce(Market.ticker, "")).like(sports_family)
        | func.upper(func.coalesce(Market.event_ticker, "")).like(sports_family)
        | func.upper(func.coalesce(Market.series_ticker, "")).like(sports_family)
        | func.upper(func.coalesce(Market.ticker, "")).like(cross_category_family)
        | func.upper(func.coalesce(Market.event_ticker, "")).like(cross_category_family)
        | func.upper(func.coalesce(Market.series_ticker, "")).like(cross_category_family)
    )


def _classify_composite_type(
    ticker: str | None,
    event_ticker: str | None,
    series_ticker: str | None,
) -> str:
    values = [str(value or "").upper() for value in (ticker, event_ticker, series_ticker)]
    if any(value.startswith(SPORTS_COMPOSITE_PREFIX) for value in values):
        return "sports_multigame"
    if any(value.startswith(CROSS_CATEGORY_COMPOSITE_PREFIX) for value in values):
        return "cross_category"
    if any(value.startswith("KXMVE") for value in values):
        return "other_kxmve_composite"
    return "unknown_composite"


def _parking_status(composite_type: str) -> str:
    if composite_type == "sports_multigame":
        return "PARKED_NEEDS_SPORTS_COMPOSITE_SUPPORT"
    if composite_type == "cross_category":
        return "PARKED_NEEDS_COMPONENT_EVIDENCE_SUPPORT"
    return "PARKED_UNSUPPORTED_COMPOSITE"


def _component_evidence_status(category: str) -> str:
    if category == "sports":
        return "NO_EXACT_VERIFIED_COMPONENT_EVIDENCE_ON_COMPOSITE_TICKER"
    if category == "cross_category":
        return "NO_COMPONENT_SCHEMA_OR_VERIFIED_COMPONENT_ROWS"
    return "NO_EXACT_COMPONENT_EVIDENCE_FOUND"


def _future_support_required(composite_type: str) -> str:
    if composite_type == "sports_multigame":
        return "sports component schedule/roster model plus composite settlement logic"
    if composite_type == "cross_category":
        return "component registry with exact ticker/source/settlement evidence"
    return "composite component parser and paper-only preview gate"


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_type = Counter(row["composite_type"] for row in rows)
    by_category = Counter(row["category"] for row in rows)
    legs_by_category: dict[str, int] = {}
    for row in rows:
        category = str(row["category"])
        legs_by_category[category] = legs_by_category.get(category, 0) + int(
            row["parsed_legs"] or 0
        )
    return {
        "unsupported_composite_rows": len(rows),
        "all_rows_parked": all(row["excluded_from_single_market_remediation"] for row in rows),
        "normal_single_market_remediation_allowed_rows": sum(
            1 for row in rows if row["normal_link_remediation_allowed"]
        ),
        "exact_component_evidence_rows": sum(
            1 for row in rows if row["exact_component_evidence_exists"]
        ),
        "composite_type_counts": dict(by_type),
        "category_counts": dict(by_category),
        "parsed_leg_counts_by_category": legs_by_category,
        "coverage_pollution_status": "PARKED_OUTSIDE_SINGLE_MARKET_LINK_COVERAGE",
    }


def _future_support_plan(summary: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "stage": "R7A_COMPONENT_SCHEMA",
            "purpose": "Add explicit composite component rows keyed by composite ticker.",
            "gate": "Only exact Kalshi component tickers or source URLs are allowed.",
        },
        {
            "stage": "R7B_PAPER_ONLY_PREVIEW",
            "purpose": "Preview composite decomposition without DB writes or paper trades.",
            "gate": "Emit rows_safe_for_component_link only with exact component evidence.",
        },
        {
            "stage": "R7C_COMPONENT_FORECAST_AGGREGATION",
            "purpose": "Combine verified component forecasts into composite probabilities.",
            "gate": "No fuzzy matching, no fabricated legs, no single-market remediations.",
        },
        {
            "stage": "R7D_COMPOSITE_PAPER_GATE",
            "purpose": "Integrate composite rows into a separate paper-ready gate.",
            "gate": (
                f"Keep {summary['unsupported_composite_rows']} current rows parked until "
                "component evidence is complete."
            ),
        },
    ]


def _acceptance(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "composite_markets_remain_parked": summary["all_rows_parked"],
        "do_not_pollute_single_market_link_coverage": (
            summary["normal_single_market_remediation_allowed_rows"] == 0
        ),
        "future_support_plan_exists": True,
        "no_paper_trades_created": True,
        "no_live_or_demo_orders": True,
    }


def _operator_guardrails() -> list[str]:
    return [
        "Keep PAPER / READ-ONLY.",
        "Do not create paper trades.",
        "Do not submit, cancel, replace, or amend live/demo exchange orders.",
        "Do not decompose composite markets with fuzzy component matching.",
        "Do not fabricate component evidence.",
        "Do not run normal single-market link remediation against parked composites.",
    ]


def _metadata(
    session: Session,
    *,
    settings: Settings,
    generated_at: str,
    command_args: list[str],
) -> dict[str, Any]:
    db_url = database_url_from_settings(settings)
    return {
        "generated_at": generated_at,
        "repository_root": str(Path.cwd().resolve()),
        "git_branch": _git_value("rev-parse", "--abbrev-ref", "HEAD"),
        "git_commit": _git_value("rev-parse", "HEAD"),
        "git_dirty": _git_dirty_status(),
        "python_executable": str(Path(sys.executable).resolve()),
        "installed_package_path": str(Path(__file__).resolve()),
        "resolved_database_url": redact_database_url(db_url),
        "database_fingerprint": _database_fingerprint(db_url),
        "database_location": describe_db_location(db_url),
        "migration_revision": _migration_revision(session),
        "timezone": getattr(settings, "timezone", None) or "UTC",
        "command_arguments": {
            "command": "kalshi-bot phase3ba-r7-composite-market-plan",
            "argv": command_args,
        },
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
        "safety_flags": {
            "paper_only": True,
            "diagnostic_only": True,
            "creates_paper_trades": False,
            "places_exchange_orders": False,
            "submits_cancels_replaces_orders": False,
            "fabricates_component_evidence": False,
            "uses_fuzzy_component_matching": False,
            "normal_single_market_remediation": False,
        },
    }


def _database_fingerprint(db_url: str) -> dict[str, Any]:
    redacted = redact_database_url(db_url)
    sqlite_path = sqlite_path_from_url(db_url)
    if sqlite_path is None:
        return {
            "kind": "non_sqlite",
            "database_url_hash": hashlib.sha256(redacted.encode("utf-8")).hexdigest(),
        }
    if str(sqlite_path) == ":memory:":
        return {"kind": "sqlite_memory", "path": ":memory:"}
    path = sqlite_path.expanduser().resolve()
    if not path.exists():
        return {"kind": "missing_sqlite_file", "path": str(path)}
    stat = path.stat()
    payload = {
        "path": str(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    return {
        "kind": "sqlite_file_stat",
        **payload,
        "fingerprint": hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }


def _migration_revision(session: Session) -> str | None:
    try:
        return session.execute(text("select version_num from alembic_version limit 1")).scalar()
    except Exception:
        return None


def _git_value(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "UNKNOWN"


def _git_dirty_status() -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"
    if result.returncode != 0:
        return "UNKNOWN"
    return "dirty" if result.stdout.strip() else "clean"


def _render_executive_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = _metadata_lines(payload, title="# Phase 3BA-R7 Composite Market Parking Plan")
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Unsupported composite rows: `{summary['unsupported_composite_rows']}`",
            f"- All rows parked: `{summary['all_rows_parked']}`",
            "- Normal single-market remediation allowed rows: "
            f"`{summary['normal_single_market_remediation_allowed_rows']}`",
            "- Exact component evidence rows: "
            f"`{summary['exact_component_evidence_rows']}`",
            f"- Coverage status: `{summary['coverage_pollution_status']}`",
            "",
            "## Type Counts",
            "",
        ]
    )
    for key, value in summary["composite_type_counts"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Acceptance", ""])
    for key, value in payload["acceptance"].items():
        lines.append(f"- {key}: `{value}`")
    return "\n".join(lines) + "\n"


def _render_plan_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = _metadata_lines(payload, title="# Composite Market Plan")
    lines.extend(
        [
            "",
            "## Parking Summary",
            "",
            f"- Unsupported composite rows: `{summary['unsupported_composite_rows']}`",
            "- Composite type counts: "
            f"`{json.dumps(summary['composite_type_counts'], sort_keys=True)}`",
            f"- Category counts: `{json.dumps(summary['category_counts'], sort_keys=True)}`",
            "- Status: parked outside normal single-market link remediation.",
            "",
            "## Future Support Plan",
            "",
        ]
    )
    for step in payload["future_support_plan"]:
        lines.extend(
            [
                f"### {step['stage']}",
                f"- Purpose: {step['purpose']}",
                f"- Gate: {step['gate']}",
                "",
            ]
        )
    lines.extend(
        [
            "## Component Evidence",
            "",
            "- Exact component evidence rows found: "
            f"`{summary['exact_component_evidence_rows']}`",
            "- Current decision: no composite decomposition is allowed until explicit component "
            "evidence exists.",
            "",
            "## Guardrails",
            "",
        ]
    )
    for guardrail in payload["operator_guardrails"]:
        lines.append(f"- {guardrail}")
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = _metadata_lines(payload, title="# Phase 3BA-R7 Next Actions")
    lines.extend(
        [
            "",
            "## Exact Next Operator Command",
            "",
            "```bash",
            "kalshi-bot phase3ba-r6-noncrypto-engine-backlog "
            "--output-dir reports/phase3ba_r6 --reports-dir reports",
            "```",
            "",
            "## Composite Rule",
            "",
            f"- Keep `{summary['unsupported_composite_rows']}` composite rows parked.",
            "- Do not run normal `link-remediate` against KXMVE composites.",
            "- Build composite component support only through a paper-only preview gate.",
            "",
            "## Future Build",
            "",
        ]
    )
    for step in payload["future_support_plan"]:
        lines.append(f"- {step['stage']}: {step['purpose']} Gate: {step['gate']}")
    return "\n".join(lines) + "\n"


def _metadata_lines(payload: dict[str, Any], *, title: str) -> list[str]:
    return [
        title,
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Git commit: `{payload['git_commit']}`",
        f"- DB fingerprint: `{json.dumps(payload['database_fingerprint'], sort_keys=True)}`",
        f"- Command args: `{json.dumps(payload['command_arguments'], sort_keys=True)}`",
        f"- Safety flags: `{json.dumps(payload['safety_flags'], sort_keys=True)}`",
        f"- Live/demo execution: `{payload['live_or_demo_execution']}`",
        "- Order submission/cancel/replace: "
        f"`{payload['order_submission'] or payload['order_cancel_replace']}`",
        f"- Paper trade creation: `{payload['paper_trade_creation']}`",
        f"- Thresholds lowered: `{payload['thresholds_lowered']}`",
    ]


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "ticker",
        "category",
        "composite_type",
        "market_title",
        "event_ticker",
        "series_ticker",
        "market_status",
        "close_time",
        "parsed_legs",
        "parking_status",
        "excluded_from_single_market_remediation",
        "normal_link_remediation_allowed",
        "exact_component_evidence_exists",
        "component_evidence_status",
        "future_support_required",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines = []
    for file_path in files:
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {file_path.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
