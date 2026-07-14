from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import SportsMarketLink
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.time import utc_now

PHASE_3_SPORTS_LINK_CLEANUP_VERSION = "sports_link_cleanup_v1"

SAFE_SOURCES = {
    "verified_schedule",
    "kalshi_event_derived",
    "market-derived-fallback",
    "broad-match-quarantine",
}


@dataclass(frozen=True)
class SportsLinkCleanupArtifactSet:
    output_path: Path
    json_path: Path
    rows_path: Path


def build_sports_link_cleanup(
    session: Session,
    *,
    settings: Settings | None = None,
    apply: bool = False,
    max_links_per_ticker: int | None = None,
    delete_batch_size: int = 5000,
) -> dict[str, Any]:
    """Find and optionally delete legacy broad sports-link fanout rows.

    This pass is intentionally narrow: it only targets legacy direct-link rows whose
    reason looks like the old broad game matcher. That includes fanout rows aimed at
    Kalshi-event-derived games, but preserves the base derived row itself, verified
    schedule rows, market-derived fallback rows, and quarantine rows.
    """
    resolved = settings or get_settings()
    threshold = max_links_per_ticker or resolved.sports_max_direct_links_per_market
    threshold = max(1, int(threshold))
    rows = _sports_link_rows(session)

    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    provenance_counts: Counter[str] = Counter()
    preserved_counts: Counter[str] = Counter()
    for row in rows:
        row["provenance"] = _sports_link_provenance(row)
        row["is_noisy_direct_candidate"] = _is_noisy_direct_candidate(row)
        by_ticker[row["ticker"]].append(row)
        provenance_counts[row["provenance"]] += 1
        if not row["is_noisy_direct_candidate"]:
            preserved_counts[row["provenance"]] += 1

    noisy_ticker_rows: list[dict[str, Any]] = []
    eligible_ids: list[int] = []
    reason_counts: Counter[str] = Counter()
    league_counts: Counter[str] = Counter()
    for ticker, ticker_rows in by_ticker.items():
        candidates = [row for row in ticker_rows if row["is_noisy_direct_candidate"]]
        if len(candidates) <= threshold:
            continue
        eligible_ids.extend(int(row["id"]) for row in candidates)
        reason_counts.update(str(row["link_reason"]) for row in candidates)
        league_counts.update(str(row["league"]) for row in candidates)
        noisy_ticker_rows.append(
            {
                "ticker": ticker,
                "total_links": len(ticker_rows),
                "noisy_direct_rows": len(candidates),
                "preserved_rows": len(ticker_rows) - len(candidates),
                "top_reason": _top_reason(candidates),
                "league_counts": dict(Counter(str(row["league"]) for row in candidates)),
                "sample_game_keys": [str(row["game_key"]) for row in candidates[:5]],
            }
        )

    noisy_ticker_rows.sort(
        key=lambda row: (int(row["noisy_direct_rows"]), int(row["total_links"])),
        reverse=True,
    )
    deleted_rows = 0
    if apply and eligible_ids:
        deleted_rows = _delete_by_id_batches(
            session,
            eligible_ids,
            batch_size=max(1, delete_batch_size),
        )

    return {
        "generated_at": utc_now().isoformat(),
        "version": PHASE_3_SPORTS_LINK_CLEANUP_VERSION,
        "mode": "PAPER_ONLY_SPORTS_LINK_CLEANUP",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "dry_run": not apply,
        "apply": apply,
        "max_links_per_ticker": threshold,
        "summary": {
            "total_sports_link_rows": len(rows),
            "distinct_tickers": len(by_ticker),
            "noisy_tickers": len(noisy_ticker_rows),
            "noisy_rows_eligible_for_cleanup": len(eligible_ids),
            "rows_deleted": deleted_rows,
            "rows_preserved": len(rows) - len(eligible_ids),
            "verified_or_derived_rows_preserved": sum(
                preserved_counts[key]
                for key in (
                    "verified_schedule",
                    "kalshi_event_derived",
                    "partial_market_derived",
                    "broad_match_quarantine",
                )
            ),
        },
        "provenance_counts": dict(sorted(provenance_counts.items())),
        "eligible_reason_counts": dict(reason_counts.most_common(25)),
        "eligible_league_counts": dict(sorted(league_counts.items())),
        "top_noisy_tickers": noisy_ticker_rows[:50],
        "cleanup_policy": {
            "targets": (
                "Rows with broad direct-match link reasons on tickers whose candidate "
                "count exceeds max_links_per_ticker."
            ),
            "preserves": (
                "base kalshi_event_derived, verified_schedule, market-derived "
                "fallback, and broad-match-quarantine rows."
            ),
            "apply_required": "Rows are deleted only when --apply is passed.",
        },
        "recommended_next_action": _recommended_next_action(
            apply=apply,
            eligible_count=len(eligible_ids),
            deleted_rows=deleted_rows,
        ),
        "next_commands": _next_commands(apply=apply, eligible_count=len(eligible_ids)),
    }


def write_sports_link_cleanup_report(
    session: Session,
    *,
    output_path: Path = Path("reports/sports_link_cleanup.md"),
    json_path: Path | None = None,
    rows_path: Path | None = None,
    settings: Settings | None = None,
    apply: bool = False,
    max_links_per_ticker: int | None = None,
    delete_batch_size: int = 5000,
) -> SportsLinkCleanupArtifactSet:
    payload = build_sports_link_cleanup(
        session,
        settings=settings,
        apply=apply,
        max_links_per_ticker=max_links_per_ticker,
        delete_batch_size=delete_batch_size,
    )
    resolved_json = json_path or output_path.with_suffix(".json")
    resolved_rows = rows_path or output_path.with_name(f"{output_path.stem}_rows.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_json.parent.mkdir(parents=True, exist_ok=True)
    resolved_rows.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_render_markdown(payload), encoding="utf-8")
    resolved_json.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    resolved_rows.write_text(
        json.dumps(payload["top_noisy_tickers"], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return SportsLinkCleanupArtifactSet(output_path, resolved_json, resolved_rows)


def _sports_link_rows(session: Session) -> list[dict[str, Any]]:
    statement = select(
        SportsMarketLink.id,
        SportsMarketLink.ticker,
        SportsMarketLink.league,
        SportsMarketLink.game_key,
        SportsMarketLink.market_type,
        SportsMarketLink.link_reason,
        SportsMarketLink.raw_json,
    ).order_by(SportsMarketLink.ticker, SportsMarketLink.id)
    return [dict(row._mapping) for row in session.execute(statement)]


def _sports_link_provenance(row: dict[str, Any]) -> str:
    raw = decode_json(str(row.get("raw_json") or ""))
    source = str(raw.get("source") or "").lower()
    reason = str(row.get("link_reason") or "").lower()
    game_key = str(row.get("game_key") or "").lower()
    if source == "verified_schedule" or "verified schedule" in reason:
        return "verified_schedule"
    if source == "broad-match-quarantine" or "broad sports match rejected" in reason:
        return "broad_match_quarantine"
    if source == "kalshi_event_derived" or "kalshi-event-derived" in game_key:
        return "kalshi_event_derived"
    if (
        source == "market-derived-fallback"
        or "market-derived" in game_key
        or "market-derived" in reason
    ):
        return "partial_market_derived"
    if source in SAFE_SOURCES:
        return source
    return "other"


def _is_noisy_direct_candidate(row: dict[str, Any]) -> bool:
    if row["provenance"] in {
        "verified_schedule",
        "partial_market_derived",
        "broad_match_quarantine",
    }:
        return False
    reason = str(row.get("link_reason") or "").lower()
    return " market matched " in f" {reason} " and "game term" in reason


def _top_reason(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    return str(Counter(str(row["link_reason"]) for row in rows).most_common(1)[0][0])


def _delete_by_id_batches(session: Session, ids: list[int], *, batch_size: int) -> int:
    deleted = 0
    for start in range(0, len(ids), batch_size):
        batch = ids[start : start + batch_size]
        result = session.execute(delete(SportsMarketLink).where(SportsMarketLink.id.in_(batch)))
        deleted += int(result.rowcount or 0)
    session.flush()
    return deleted


def _recommended_next_action(*, apply: bool, eligible_count: int, deleted_rows: int) -> str:
    if eligible_count == 0:
        return "No legacy broad sports-link fanout rows were found."
    if not apply:
        return (
            "Review this dry-run report. If the eligible rows match the noisy legacy "
            "fanout, rerun with --apply."
        )
    if deleted_rows:
        return (
            "Rerun market-coverage-doctor, then resume link-remediate so the guarded "
            "linker can recreate only safe/quarantined provenance rows."
        )
    return "Apply mode ran but no rows were deleted; rerun db-writer-monitor before retrying."


def _next_commands(*, apply: bool, eligible_count: int) -> list[str]:
    if eligible_count == 0:
        return ["kalshi-bot market-coverage-doctor --output-dir reports/market_coverage"]
    if not apply:
        return [
            (
                "kalshi-bot sports-link-cleanup --apply "
                "--output reports/sports_link_cleanup.md "
                "--json-output reports/sports_link_cleanup.json"
            ),
            "kalshi-bot market-coverage-doctor --output-dir reports/market_coverage",
        ]
    return [
        "kalshi-bot market-coverage-doctor --output-dir reports/market_coverage",
        (
            "kalshi-bot link-remediate --resume --progress-every 100 "
            "--checkpoint-every 100 --stop-after-minutes 30"
        ),
    ]


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Sports Link Cleanup",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Dry run: {'YES' if payload['dry_run'] else 'NO'}",
        "- Live/demo execution: blocked; this command only inspects or deletes local link rows.",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Cleanup Policy",
            "",
            f"- Targets: {payload['cleanup_policy']['targets']}",
            f"- Preserves: {payload['cleanup_policy']['preserves']}",
            f"- Apply guard: {payload['cleanup_policy']['apply_required']}",
            "",
            "## Provenance Counts",
            "",
        ]
    )
    for key, value in payload["provenance_counts"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Eligible Reason Breakdown",
            "",
            "| Reason | Rows |",
            "| --- | ---: |",
        ]
    )
    for reason, count in payload["eligible_reason_counts"].items():
        lines.append(f"| {reason} | {count} |")
    if not payload["eligible_reason_counts"]:
        lines.append("| none | 0 |")
    lines.extend(
        [
            "",
            "## Top Noisy Tickers",
            "",
            "| Ticker | Total links | Noisy direct rows | Preserved rows | Top reason |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    for row in payload["top_noisy_tickers"][:25]:
        lines.append(
            f"| {row['ticker']} | {row['total_links']} | {row['noisy_direct_rows']} | "
            f"{row['preserved_rows']} | {row['top_reason']} |"
        )
    if not payload["top_noisy_tickers"]:
        lines.append("| none | 0 | 0 | 0 | none |")
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
            "",
            "## Next Commands",
            "",
        ]
    )
    for command in payload["next_commands"]:
        lines.append(f"- `{command}`")
    return "\n".join(lines) + "\n"
