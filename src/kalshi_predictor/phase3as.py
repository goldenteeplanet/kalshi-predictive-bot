from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import (
    latest_links_for_table,
    linked_market_state,
    linked_market_states,
    mark_link_deprecated,
)
from kalshi_predictor.data.schema import CryptoMarketLink, SportsMarketLink
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.time import utc_now

PHASE_3AS_VERSION = "phase3as_v1"


@dataclass(frozen=True)
class Phase3ASArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path


def build_active_market_universe(
    session: Session,
    *,
    limit: int = 1000,
    mark_deprecated: bool = False,
) -> dict[str, Any]:
    """Summarize open versus closed linked markets without exchange writes."""
    session.flush()
    rows: list[dict[str, Any]] = []
    deprecated_marked = 0
    for source, table in (("crypto", CryptoMarketLink), ("sports", SportsMarketLink)):
        links = latest_links_for_table(session, table, limit=limit)
        for link in links:
            state = linked_market_state(session, source=source, link=link)
            marked_this_run = False
            if mark_deprecated and state.status_bucket == "inactive":
                marked_this_run = mark_link_deprecated(
                    link,
                    market_status=state.market_status,
                )
                if marked_this_run:
                    deprecated_marked += 1
                state = linked_market_state(session, source=source, link=link)
            row = asdict(state)
            row["deprecated_marked_this_run"] = marked_this_run
            row["next_action"] = _next_action(row)
            rows.append(row)

    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AS",
        "phase_version": PHASE_3AS_VERSION,
        "mode": "PAPER_ONLY_ACTIVE_UNIVERSE_CLEANUP",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "mark_deprecated": mark_deprecated,
        "summary": _summary(rows, deprecated_marked),
        "source_summaries": _source_summaries(rows),
        "status_counts": dict(sorted(Counter(row["status_bucket"] for row in rows).items())),
        "rows": rows,
        "inactive_examples": [row for row in rows if row["status_bucket"] == "inactive"][:50],
        "unknown_examples": [row for row in rows if row["status_bucket"] == "unknown"][:50],
        "recommended_next_action": _recommended_next_action(rows),
        "next_commands": [
            "kalshi-bot active-universe-doctor --mark-deprecated --output-dir reports/phase3as",
            "kalshi-bot crypto-forecast-doctor --limit 500 --output-dir reports/phase3ar",
            "kalshi-bot forecast --model crypto_v2",
            "kalshi-bot find-opportunities --model-name crypto_v2 --limit 100",
        ],
    }


def write_phase3as_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3as"),
    limit: int = 1000,
    mark_deprecated: bool = False,
) -> Phase3ASArtifactSet:
    payload = build_active_market_universe(
        session,
        limit=limit,
        mark_deprecated=mark_deprecated,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3as_active_universe.json"
    markdown_path = output_dir / "phase3as_active_universe.md"
    rows_path = output_dir / "active_universe_rows.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    rows_path.write_text(json.dumps(payload["rows"], indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3ASArtifactSet(output_dir, json_path, markdown_path, rows_path)


def deprecated_link_rows(
    session: Session,
    *,
    source: str,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    table = CryptoMarketLink if source == "crypto" else SportsMarketLink
    links = latest_links_for_table(session, table, limit=limit)
    return [
        asdict(state)
        for state in linked_market_states(session, source=source, links=links)
        if state.link_deprecated
    ]


def _summary(rows: list[dict[str, Any]], deprecated_marked: int) -> dict[str, Any]:
    active_rows = [row for row in rows if row["status_bucket"] == "active"]
    inactive_rows = [row for row in rows if row["status_bucket"] == "inactive"]
    unknown_rows = [row for row in rows if row["status_bucket"] == "unknown"]
    active_with_snapshot = [
        row for row in active_rows if row["has_snapshot"]
    ]
    return {
        "linked_markets_checked": len(rows),
        "active_linked_markets": len(active_rows),
        "inactive_linked_markets": len(inactive_rows),
        "unknown_status_linked_markets": len(unknown_rows),
        "active_with_snapshots": len(active_with_snapshot),
        "deprecated_link_rows": sum(1 for row in rows if row["link_deprecated"]),
        "deprecated_marked_this_run": deprecated_marked,
        "ready_universe_for_forecasts": len(active_with_snapshot),
        "closed_or_inactive_excluded": len(inactive_rows),
    }


def _source_summaries(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    summaries: dict[str, dict[str, int]] = {}
    for source in sorted({str(row["source"]) for row in rows}):
        source_rows = [row for row in rows if row["source"] == source]
        summaries[source] = {
            "linked_markets_checked": len(source_rows),
            "active": sum(1 for row in source_rows if row["status_bucket"] == "active"),
            "inactive": sum(1 for row in source_rows if row["status_bucket"] == "inactive"),
            "unknown": sum(1 for row in source_rows if row["status_bucket"] == "unknown"),
            "with_snapshots": sum(1 for row in source_rows if row["has_snapshot"]),
            "deprecated": sum(1 for row in source_rows if row["link_deprecated"]),
            "marked_this_run": sum(1 for row in source_rows if row["deprecated_marked_this_run"]),
        }
    return summaries


def _next_action(row: dict[str, Any]) -> str:
    if row["status_bucket"] == "inactive":
        return "Deprecated for new forecasts; keep only for settlement/history reconciliation."
    if not row["has_snapshot"]:
        return "Collect a fresh market/orderbook snapshot before forecasting."
    if row["status_bucket"] == "unknown":
        return "Refresh market metadata so status is explicit before relying on readiness counts."
    return "Eligible for active-universe forecast/opportunity diagnostics."


def _recommended_next_action(rows: list[dict[str, Any]]) -> str:
    inactive = sum(1 for row in rows if row["status_bucket"] == "inactive")
    active_no_snapshot = sum(
        1 for row in rows if row["status_bucket"] == "active" and not row["has_snapshot"]
    )
    if inactive:
        return "Run active-universe-doctor --mark-deprecated, then rerun forecast doctors."
    if active_no_snapshot:
        return "Repair snapshots for active linked markets before generating more opportunities."
    return "Active universe is clean enough for paper-only forecast and opportunity scans."


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AS Active Market Universe + Closed-Link Cleanup",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        "- Live/demo execution: blocked; this phase only updates local link metadata.",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Source Summaries",
            "",
            "| Source | Linked | Active | Inactive | Unknown | Snapshots | Deprecated | Marked |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for source, row in payload["source_summaries"].items():
        lines.append(
            f"| {source} | {row['linked_markets_checked']} | {row['active']} | "
            f"{row['inactive']} | {row['unknown']} | {row['with_snapshots']} | "
            f"{row['deprecated']} | {row['marked_this_run']} |"
        )
    lines.extend(
        [
            "",
            "## Inactive Link Examples",
            "",
            "| Source | Ticker | Status | Deprecated | Next action |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["inactive_examples"][:25]:
        lines.append(
            f"| {row['source']} | {row['ticker']} | {row['market_status'] or 'unknown'} | "
            f"{row['link_deprecated']} | {row['next_action']} |"
        )
    if not payload["inactive_examples"]:
        lines.append("| n/a | none | n/a | n/a | No inactive linked markets found. |")
    lines.extend(["", "## Next Commands", "", "```bash"])
    lines.extend(payload["next_commands"])
    lines.extend(
        [
            "```",
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
            "",
        ]
    )
    return "\n".join(lines)
