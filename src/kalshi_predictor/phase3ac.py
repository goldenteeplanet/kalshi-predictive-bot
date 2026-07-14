from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import MarketLeg, SportsFeature, SportsGame, SportsMarketLink
from kalshi_predictor.market_legs import CATEGORY_SPORTS, parse_and_store_market_legs
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.sports.derived_schedule import derive_sports_schedule_from_market_legs
from kalshi_predictor.sports.linker import link_sports_markets
from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class Phase3ACArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


def build_sports_provenance_snapshot(session: Session) -> dict[str, Any]:
    session.flush()
    links = list(session.scalars(select(SportsMarketLink)))
    parsed_sports_markets = int(
        session.scalar(
            select(func.count(func.distinct(MarketLeg.ticker))).where(
                MarketLeg.category == CATEGORY_SPORTS
            )
        )
        or 0
    )
    counts = _provenance_counts(links)
    partial_tickers = {
        link.ticker for link in links if _link_provenance(link) == "partial_market_derived"
    }
    upgraded_tickers = {
        link.ticker
        for link in links
        if _link_provenance(link) in {"kalshi_event_derived", "verified_schedule"}
    }
    return {
        "parsed_sports_markets": parsed_sports_markets,
        "sports_links": len(links),
        "sports_games": _count(session, SportsGame),
        "sports_features": _count(session, SportsFeature),
        "provenance_counts": counts,
        "partial_without_upgrade": len(partial_tickers - upgraded_tickers),
        "partial_examples": _examples(
            [link for link in links if _link_provenance(link) == "partial_market_derived"]
        ),
        "derived_examples": _examples(
            [link for link in links if _link_provenance(link) == "kalshi_event_derived"]
        ),
    }


def run_sports_provenance_repair(
    session: Session,
    *,
    settings: Settings | None = None,
    limit: int | None = None,
    parse_first: bool = True,
    refresh_features: bool = False,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    before = build_sports_provenance_snapshot(session)
    parse_result = (
        parse_and_store_market_legs(session, limit=limit, refresh=False)
        if parse_first
        else None
    )
    derived = derive_sports_schedule_from_market_legs(
        session,
        limit=limit,
        build_features=True,
        refresh_features=refresh_features,
        settings=resolved,
    )
    linked = link_sports_markets(session, league="ALL", settings=resolved)
    after = build_sports_provenance_snapshot(session)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AC",
        "mode": "PAPER_ONLY_SPORTS_PROVENANCE_REPAIR",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "before": before,
        "after": after,
        "parse_result": _parse_result(parse_result),
        "derived_schedule": asdict(derived),
        "link_sports_markets": asdict(linked),
        "upgraded_partial_links": max(
            0,
            before["partial_without_upgrade"] - after["partial_without_upgrade"],
        ),
        "recommended_next_action": _next_action(after),
    }


def write_phase3ac_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ac"),
    settings: Settings | None = None,
    limit: int | None = None,
    parse_first: bool = True,
    refresh_features: bool = False,
) -> Phase3ACArtifactSet:
    payload = run_sports_provenance_repair(
        session,
        settings=settings,
        limit=limit,
        parse_first=parse_first,
        refresh_features=refresh_features,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3ac_sports_provenance.json"
    markdown_path = output_dir / "phase3ac_sports_provenance.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3ACArtifactSet(output_dir, json_path, markdown_path)


def _provenance_counts(links: list[SportsMarketLink]) -> dict[str, int]:
    counts: dict[str, int] = {
        "verified_schedule": 0,
        "kalshi_event_derived": 0,
        "partial_market_derived": 0,
    }
    for link in links:
        provenance = _link_provenance(link)
        counts[provenance] = counts.get(provenance, 0) + 1
    return counts


def _link_provenance(link: SportsMarketLink) -> str:
    raw = decode_json(link.raw_json)
    source = str(raw.get("source") or "").lower()
    reason = str(link.link_reason or "").lower()
    game_key = str(link.game_key or "").lower()
    if "kalshi-event-derived" in game_key or source == "kalshi_event_derived":
        return "kalshi_event_derived"
    if (
        "market-derived" in game_key
        or source == "market-derived-fallback"
        or "market-derived" in reason
    ):
        return "partial_market_derived"
    return "verified_schedule"


def _examples(links: list[SportsMarketLink], *, limit: int = 10) -> list[dict[str, Any]]:
    rows = []
    for link in links[:limit]:
        rows.append(
            {
                "ticker": link.ticker,
                "league": link.league,
                "game_key": link.game_key,
                "market_type": link.market_type,
                "link_confidence": link.link_confidence,
            }
        )
    return rows


def _parse_result(value: Any | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "markets_scanned": value.markets_scanned,
        "markets_with_legs": value.markets_with_legs,
        "legs_inserted": value.legs_inserted,
        "markets_skipped_existing": value.markets_skipped_existing,
        "existing_markets_with_legs": getattr(value, "existing_markets_with_legs", 0),
    }


def _count(session: Session, table: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(table)) or 0)


def _next_action(snapshot: dict[str, Any]) -> str:
    if snapshot["partial_without_upgrade"]:
        return (
            "Sports is usable but still has partial links. Ingest verified schedules/teams "
            "to upgrade remaining market-derived provenance."
        )
    if snapshot["parsed_sports_markets"] == 0:
        return "Collect sports markets, then run market-legs-parse and this repair again."
    return "Sports provenance is connected enough for paper-only model learning."


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AC Sports Provenance Repair",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Upgraded partial links: {payload['upgraded_partial_links']}",
        "",
        "## Before",
        "",
    ]
    for key, value in payload["before"].items():
        if key.endswith("_examples"):
            continue
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## After", ""])
    for key, value in payload["after"].items():
        if key.endswith("_examples"):
            continue
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Repair Actions",
            "",
            f"- Parser: {payload['parse_result']}",
            f"- Derived schedule: {payload['derived_schedule']}",
            f"- Sports linker: {payload['link_sports_markets']}",
            "",
            "## Remaining Partial Examples",
            "",
            "| Ticker | League | Game key | Type | Confidence |",
            "| --- | --- | --- | --- | ---: |",
        ]
    )
    examples = payload["after"].get("partial_examples", [])
    if examples:
        for row in examples:
            lines.append(
                f"| {row['ticker']} | {row['league']} | {row['game_key']} | "
                f"{row['market_type']} | {row['link_confidence']} |"
            )
    else:
        lines.append("| None |  |  |  |  |")
    lines.extend(["", "## Recommended Next Action", "", payload["recommended_next_action"], ""])
    return "\n".join(lines)
