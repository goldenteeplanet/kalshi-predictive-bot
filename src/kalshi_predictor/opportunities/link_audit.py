from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import Market, MarketRanking
from kalshi_predictor.opportunities.market_identity import (
    BUILT_FROM_EXACT_CATALOG,
    COMPOSITE_LOCAL_ONLY,
    GENERAL_SOURCE_NOT_SAFE,
    MALFORMED_URL,
    MARKET_NOT_IN_CATALOG,
    MISSING_MARKET_TICKER,
    PARTIAL_PROVENANCE_BLOCKED,
    PLACEHOLDER_BLOCKED,
    STALE_CATALOG,
    SYNTHETIC_ONLY,
    UNVERIFIED,
    VERIFIED,
    VERIFIED_BUT_CLOSED,
    VERIFIED_BUT_PAUSED,
    VERIFIED_BUT_SETTLED,
    market_identity_fields,
    verify_market_identity,
)
from kalshi_predictor.utils.time import utc_now

PHASE_3AO_LINK_AUDIT_VERSION = "phase3ao_opportunity_links_v1"

CLICKABLE_STATUSES = {
    VERIFIED,
    VERIFIED_BUT_CLOSED,
    VERIFIED_BUT_PAUSED,
    VERIFIED_BUT_SETTLED,
}


@dataclass(frozen=True)
class OpportunityLinkAuditArtifacts:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    broken_links_csv_path: Path
    manifest_path: Path


def build_opportunity_link_audit(
    session: Session,
    *,
    limit: int = 2000,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Build a read-only audit of opportunity rows and exact Kalshi links."""
    resolved_settings = settings or get_settings()
    generated_at = utc_now().isoformat()
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    seen: set[str] = set()
    statement = (
        select(MarketRanking)
        .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
        .limit(max(1, limit * 3))
    )
    for ranking in session.scalars(statement):
        ticker = str(ranking.ticker or "").strip()
        dedupe_key = ticker or f"ranking:{ranking.id}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        market = session.get(Market, ticker) if ticker else None
        identity = verify_market_identity(
            session,
            ticker=ticker,
            ranking=ranking,
            market=market,
            settings=resolved_settings,
        )
        identity_fields = market_identity_fields(identity)
        malformed_url = _has_malformed_or_mismatched_url(market=market, identity_status=identity.url_verification_status)
        ui_contract = _ui_contract(identity.tradeable, identity.url_verified)
        row = {
            "ranking_id": ranking.id,
            "ticker": ticker,
            "title": identity.market_title or ranking.title or ticker,
            "model": ranking.forecast_model,
            "ranked_at": ranking.ranked_at.isoformat() if ranking.ranked_at else None,
            "opportunity_score": str(ranking.opportunity_score or ""),
            "ui_contract": ui_contract,
            "malformed_or_mismatched_url": malformed_url,
            **identity_fields,
            "market_identity": identity.as_dict(),
        }
        rows.append(row)
        counts[identity.url_verification_status] += 1
        if malformed_url:
            counts["MALFORMED_OR_MISMATCHED_URL"] += 1
        if len(rows) >= limit:
            break

    verified = sum(1 for row in rows if row["kalshi_url_verified"] and row["kalshi_url_status"] in CLICKABLE_STATUSES)
    blocked_rows = [row for row in rows if row["diagnostic_only"]]
    ui_visible_missing = [
        row
        for row in rows
        if row["ui_contract"] == "DIRECT_REVIEW" and not row["kalshi_url_verified"]
    ]
    status_counts = dict(sorted(counts.items()))
    summary = {
        "total_opportunities_scanned": len(rows),
        "verified_urls": verified,
        "blocked_unverified_opportunities": len(blocked_rows),
        "ui_visible_opportunities_without_clickable_verified_url": len(ui_visible_missing),
        "missing_market_ticker": counts[MISSING_MARKET_TICKER],
        "not_found_in_catalog": counts[MARKET_NOT_IN_CATALOG],
        "synthetic_internal": counts[SYNTHETIC_ONLY],
        "composite_local": counts[COMPOSITE_LOCAL_ONLY],
        "placeholder_blocked": counts[PLACEHOLDER_BLOCKED],
        "partial_provenance": counts[PARTIAL_PROVENANCE_BLOCKED],
        "general_source_not_safe": counts[GENERAL_SOURCE_NOT_SAFE],
        "stale_catalog": counts[STALE_CATALOG],
        "deterministic_catalog_url_proposals": counts[BUILT_FROM_EXACT_CATALOG],
        "unverified_missing_web_url_or_slug": counts[UNVERIFIED] + counts[MALFORMED_URL],
        "malformed_or_mismatched_urls": counts[MALFORMED_URL]
        + counts["MALFORMED_OR_MISMATCHED_URL"],
        "passes_contract": len(ui_visible_missing) == 0,
    }
    return {
        "generated_at": generated_at,
        "phase": "3AO",
        "phase_version": PHASE_3AO_LINK_AUDIT_VERSION,
        "mode": "PAPER_ONLY_READ_ONLY_LINK_AUDIT",
        "safety": {
            "live_execution_enabled": False,
            "demo_execution_enabled": False,
            "order_submit_cancel_replace_enabled": False,
            "paper_trade_creation_enabled": False,
            "database_writes": False,
        },
        "contract": {
            "direct_review_requires_exact_market_ticker": True,
            "direct_review_requires_verified_kalshi_market_url": True,
            "synthetic_composite_placeholder_and_partial_rows_are_diagnostic_only": True,
            "sibling_or_related_ticker_substitution_allowed": False,
            "fabricated_title_slug_urls_allowed": False,
        },
        "summary": summary,
        "status_counts": status_counts,
        "top_blocker_reasons": _top_blockers(blocked_rows),
        "next_actions": _next_actions(summary, status_counts),
        "rows": rows,
        "broken_or_blocked_rows": blocked_rows,
    }


def write_opportunity_link_audit(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ao"),
    limit: int = 2000,
    settings: Settings | None = None,
) -> OpportunityLinkAuditArtifacts:
    payload = build_opportunity_link_audit(session, limit=limit, settings=settings)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "opportunity_link_audit.json"
    markdown_path = output_dir / "opportunity_link_audit.md"
    csv_path = output_dir / "broken_opportunity_links.csv"
    manifest_path = output_dir / "MANIFEST.sha256"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    _write_broken_csv(csv_path, payload["broken_or_blocked_rows"])
    _write_manifest(manifest_path, [json_path, markdown_path, csv_path])
    return OpportunityLinkAuditArtifacts(
        output_dir=output_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        broken_links_csv_path=csv_path,
        manifest_path=manifest_path,
    )


def _ui_contract(tradeable: bool, url_verified: bool) -> str:
    if tradeable and url_verified:
        return "DIRECT_REVIEW"
    return "DIAGNOSTIC_ONLY"


def _has_malformed_or_mismatched_url(*, market: Market | None, identity_status: str) -> bool:
    if market is None:
        return False
    raw = decode_json(market.raw_json)
    raw_url = next(
        (
            str(raw.get(key) or "").strip()
            for key in ("kalshi_url", "market_url", "trade_url", "web_url", "event_url", "url")
            if str(raw.get(key) or "").strip()
        ),
        "",
    )
    if not raw_url:
        return False
    return identity_status in {UNVERIFIED, MALFORMED_URL, "AMBIGUOUS_MARKET_IDENTITY"}


def _top_blockers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[tuple[str, str]] = Counter()
    for row in rows:
        counter[(str(row.get("kalshi_url_status") or ""), str(row.get("kalshi_url_reason") or ""))] += 1
    return [
        {"status": status, "reason": reason, "count": count}
        for (status, reason), count in counter.most_common(12)
    ]


def _next_actions(summary: dict[str, Any], status_counts: dict[str, int]) -> list[str]:
    actions: list[str] = []
    if summary["ui_visible_opportunities_without_clickable_verified_url"]:
        actions.append("Remove direct-review visibility for any row missing a verified exact Kalshi URL.")
    if summary["stale_catalog"]:
        actions.append("Refresh the Kalshi market catalog and rerun opportunity-link-audit.")
    if summary["unverified_missing_web_url_or_slug"]:
        actions.append("Persist real Kalshi web URLs or official slug fields during market ingestion.")
    if summary["deterministic_catalog_url_proposals"]:
        actions.append("Run Phase 3AR URL repair to persist official URLs for exact catalog proposals.")
    if summary["not_found_in_catalog"] or summary["missing_market_ticker"]:
        actions.append("Repair market_ticker lineage before ranking opportunities.")
    if (
        summary["synthetic_internal"]
        or summary["composite_local"]
        or summary["placeholder_blocked"]
        or summary["partial_provenance"]
    ):
        actions.append("Keep synthetic, composite, placeholder, and partial-provenance rows diagnostic-only.")
    if summary["general_source_not_safe"]:
        actions.append("Complete source-readiness evidence before general-source rows can show trade links.")
    if not actions and status_counts:
        actions.append("Contract holds; rerun after the next market catalog refresh.")
    if not actions:
        actions.append("No opportunity rows found; run the opportunity scanner after market data is fresh.")
    return actions


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AO Opportunity Link Audit",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        "- Live/demo execution: blocked",
        "- Order submission/cancel/replace: blocked",
        "- Paper trade creation: blocked",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Status Counts", ""])
    for key, value in payload["status_counts"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Top Blockers",
            "",
            "| Status | Count | Reason |",
            "| --- | ---: | --- |",
        ]
    )
    blockers = payload["top_blocker_reasons"]
    if not blockers:
        lines.append("| none | 0 |  |")
    for row in blockers:
        lines.append(f"| {row['status']} | {row['count']} | {row['reason']} |")
    lines.extend(["", "## Next Actions", ""])
    for action in payload["next_actions"]:
        lines.append(f"- {action}")
    lines.extend(
        [
            "",
            "## Contract",
            "",
            f"- Direct review missing verified URL: {summary['ui_visible_opportunities_without_clickable_verified_url']}",
            f"- Contract pass: {summary['passes_contract']}",
            "",
        ]
    )
    return "\n".join(lines)


def _write_broken_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "ticker",
        "title",
        "model",
        "ranked_at",
        "kalshi_url_status",
        "kalshi_url_reason",
        "kalshi_url",
        "market_ticker",
        "event_ticker",
        "series_ticker",
        "catalog_last_seen_at",
        "source_lineage",
        "ui_contract",
        "malformed_or_mismatched_url",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def _write_manifest(path: Path, artifacts: list[Path]) -> None:
    lines = []
    for artifact in artifacts:
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        lines.append(f"{digest}  {artifact.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
