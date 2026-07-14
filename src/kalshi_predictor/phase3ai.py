from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.market_legs import link_coverage_dashboard
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3ae import run_verified_sports_schedule_connector
from kalshi_predictor.utils.time import utc_now

PHASE_3AI_VERSION = "phase3ai_v1"


@dataclass(frozen=True)
class Phase3AIArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


def build_phase3ai_reconciliation(
    session: Session,
    *,
    settings: Settings | None = None,
    upgrade_sports: bool = True,
    limit: int | None = None,
    min_confidence: Decimal | None = None,
    build_features: bool = True,
    refresh_features: bool = False,
    max_schedule_delta_hours: int | None = 18,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    progress_every: int = 0,
) -> dict[str, Any]:
    """Reconcile link counts and optionally upgrade sports links with verified schedules.

    Phase 3AI only writes paper-only link/feature provenance rows through the existing
    Phase 3AE connector. It never submits, modifies, or cancels exchange orders.
    """
    resolved = settings or get_settings()
    session.flush()
    before_dashboard = link_coverage_dashboard(session)
    sports_upgrade = {"status": "SKIPPED", "reason": "upgrade_sports is false"}
    if upgrade_sports:
        sports_upgrade = run_verified_sports_schedule_connector(
            session,
            settings=resolved,
            limit=limit,
            min_confidence=min_confidence,
            build_features=build_features,
            refresh_features=refresh_features,
            max_schedule_delta_hours=max_schedule_delta_hours,
            progress_callback=progress_callback,
            progress_every=progress_every,
        )
        session.flush()
    after_dashboard = link_coverage_dashboard(session)
    before_sports = _sports_reconciliation(before_dashboard)
    after_sports = _sports_reconciliation(after_dashboard)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AI",
        "phase_version": PHASE_3AI_VERSION,
        "mode": "PAPER_ONLY_LINK_RECONCILIATION",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "upgrade_sports": upgrade_sports,
        "before": {
            "summary_cards": before_dashboard["summary_cards"],
            "category_rows": before_dashboard["category_rows"],
            "sports_reconciliation": before_sports,
        },
        "after": {
            "summary_cards": after_dashboard["summary_cards"],
            "category_rows": after_dashboard["category_rows"],
            "sports_reconciliation": after_sports,
        },
        "count_definitions": after_dashboard.get("count_definitions", []),
        "count_rows": _count_rows(after_sports),
        "consistency_checks": _consistency_checks(after_dashboard),
        "sports_upgrade": _sports_upgrade_summary(sports_upgrade),
        "remaining_partial_examples": after_dashboard.get("partial_examples", [])[:20],
        "recommended_next_action": _next_action(after_sports, sports_upgrade),
    }


def write_phase3ai_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ai"),
    settings: Settings | None = None,
    upgrade_sports: bool = True,
    limit: int | None = None,
    min_confidence: Decimal | None = None,
    build_features: bool = True,
    refresh_features: bool = False,
    max_schedule_delta_hours: int | None = 18,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    progress_every: int = 0,
) -> Phase3AIArtifactSet:
    payload = build_phase3ai_reconciliation(
        session,
        settings=settings,
        upgrade_sports=upgrade_sports,
        limit=limit,
        min_confidence=min_confidence,
        build_features=build_features,
        refresh_features=refresh_features,
        max_schedule_delta_hours=max_schedule_delta_hours,
        progress_callback=progress_callback,
        progress_every=progress_every,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3ai_link_reconciliation.json"
    markdown_path = output_dir / "phase3ai_link_reconciliation.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3AIArtifactSet(output_dir, json_path, markdown_path)


def _sports_reconciliation(dashboard: dict[str, Any]) -> dict[str, Any]:
    return dashboard.get("reconciliation", {}).get("sports", {})


def _count_rows(sports: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "metric": "partial_legs",
            "value": sports.get("unresolved_partial_legs", 0),
            "unit": "parsed legs",
            "definition": (
                "Parsed leg rows whose ticker still only has unresolved "
                "market-derived provenance."
            ),
        },
        {
            "metric": "partial_markets",
            "value": sports.get("unresolved_partial_markets", 0),
            "unit": "distinct tickers",
            "definition": (
                "Distinct sports markets with market-derived fallback rows and no "
                "derived/verified upgrade."
            ),
        },
        {
            "metric": "partial_link_rows",
            "value": sports.get("partial_link_rows", 0),
            "unit": "raw link rows",
            "definition": "Raw sports_market_links rows with market-derived fallback provenance.",
        },
        {
            "metric": "derived_but_usable_links",
            "value": sports.get("derived_usable_link_rows", 0),
            "unit": "raw link rows",
            "definition": (
                "Kalshi-event-derived sports rows that are usable for paper-only features "
                "but not externally verified."
            ),
        },
        {
            "metric": "verified_schedule_links",
            "value": sports.get("verified_schedule_link_rows", 0),
            "unit": "raw link rows",
            "definition": "Sports rows backed by ingested schedule/team/competition evidence.",
        },
    ]


def _consistency_checks(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    sports = _sports_reconciliation(dashboard)
    sports_row = next(
        (
            row
            for row in dashboard.get("category_rows", [])
            if row.get("category") == "sports"
        ),
        {},
    )
    checks = [
        _check(
            "sports_category_partial_markets_match",
            sports_row.get("partial_markets", 0) == sports.get("unresolved_partial_markets", 0),
            (
                "Sports category Partial column equals unresolved partial markets, not raw "
                "link rows."
            ),
        ),
        _check(
            "partial_link_rows_not_less_than_markets",
            sports.get("partial_link_rows", 0) >= sports.get("partial_markets", 0),
            "Raw partial link rows should be at least distinct partial markets.",
        ),
        _check(
            "partial_legs_not_less_than_unresolved_markets",
            sports.get("unresolved_partial_legs", 0)
            >= sports.get("unresolved_partial_markets", 0),
            "A parsed unresolved partial market should have at least one parsed sports leg.",
        ),
        _check(
            "linked_sports_markets_cover_verified_and_derived",
            sports.get("sports_linked_markets", 0)
            >= max(
                sports.get("verified_schedule_markets", 0),
                sports.get("derived_usable_markets", 0),
            ),
            "Distinct sports linked markets should include verified and derived markets.",
        ),
    ]
    return checks


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "status": "PASS" if passed else "FAIL", "detail": detail}


def _sports_upgrade_summary(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("status") == "SKIPPED":
        return payload
    summary = payload.get("summary", {})
    return {
        "status": "RAN",
        "verified_links_created": summary.get("verified_links_created", 0),
        "verified_links_existing": summary.get("verified_links_existing", 0),
        "features_created": summary.get("features_created", 0),
        "unresolved": summary.get("unresolved", 0),
        "remaining_partial_without_upgrade": summary.get("remaining_partial_without_upgrade", 0),
        "recommended_next_action": payload.get("recommended_next_action"),
    }


def _next_action(sports: dict[str, Any], sports_upgrade: dict[str, Any]) -> str:
    unresolved = int(sports.get("unresolved_partial_markets") or 0)
    if sports_upgrade.get("status") == "SKIPPED":
        return (
            "Run Phase 3AI with --upgrade-sports to try verified schedule/team upgrades, "
            "then rerun market-coverage-doctor."
        )
    if unresolved:
        return (
            "Sports counts now reconcile. Remaining partial markets need more verified "
            "schedule/team/competition ingestion, especially soccer competitions."
        )
    return "Sports link counts reconcile and no unresolved partial sports markets remain."


def _render_markdown(payload: dict[str, Any]) -> str:
    after_sports = payload["after"]["sports_reconciliation"]
    lines = [
        "# Phase 3AI Link Coverage Count Reconciliation + Verified Sports Upgrade",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Upgrade sports: {payload['upgrade_sports']}",
        "",
        "## Count Reconciliation",
        "",
        "| Metric | Value | Unit | Definition |",
        "| --- | ---: | --- | --- |",
    ]
    for row in payload["count_rows"]:
        lines.append(
            f"| {row['metric']} | {row['value']} | {row['unit']} | {_md(row['definition'])} |"
        )
    lines.extend(
        [
            "",
            "## Sports Provenance",
            "",
            f"- Sports parsed markets: {after_sports.get('sports_parsed_markets', 0)}",
            f"- Sports linked markets: {after_sports.get('sports_linked_markets', 0)}",
            f"- Verified schedule markets: {after_sports.get('verified_schedule_markets', 0)}",
            f"- Derived-but-usable markets: {after_sports.get('derived_usable_markets', 0)}",
            f"- Unresolved partial markets: {after_sports.get('unresolved_partial_markets', 0)}",
            "",
            "## Consistency Checks",
            "",
            "| Check | Status | Detail |",
            "| --- | --- | --- |",
        ]
    )
    for check in payload["consistency_checks"]:
        lines.append(f"| {check['name']} | {check['status']} | {_md(check['detail'])} |")
    upgrade = payload["sports_upgrade"]
    lines.extend(["", "## Verified Sports Upgrade", ""])
    for key, value in upgrade.items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Remaining Partial Examples",
            "",
            "| Ticker | Category | Leg | Next action |",
            "| --- | --- | --- | --- |",
        ]
    )
    examples = payload["remaining_partial_examples"]
    if examples:
        for row in examples[:20]:
            lines.append(
                f"| `{row['ticker']}` | {row['category']} | {_md(row['raw_text'])} | "
                f"{_md(row['next_action'])} |"
            )
    else:
        lines.append("| none |  |  |  |")
    lines.extend(["", "## Recommended Next Action", "", payload["recommended_next_action"], ""])
    return "\n".join(lines)


def _md(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
