from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

LINK_TABLES = {
    "crypto": "crypto_market_links",
    "weather": "weather_market_links",
    "economic": "economic_market_links",
    "sports": "sports_market_links",
    "news": "news_market_links",
}
INACTIVE_STATUSES = ("closed", "expired", "finalized", "inactive", "resolved", "settled")


def utc_now() -> datetime:
    return datetime.now(UTC)


def refresh_current_snapshot(connection: sqlite3.Connection, base: dict[str, Any]) -> dict[str, Any]:
    now_value = utc_now().replace(tzinfo=None).isoformat(sep=" ")
    placeholders = ",".join("?" for _ in INACTIVE_STATUSES)
    connection.execute(
        f"""
        CREATE TEMP TABLE current_coverage_tickers AS
        SELECT ticker FROM markets
        WHERE lower(trim(coalesce(status, ''))) NOT IN ({placeholders})
          AND (close_time IS NULL OR close_time > ?)
          AND (expected_expiration_time IS NULL OR expected_expiration_time > ?)
          AND (expiration_time IS NULL OR expiration_time > ?)
        """,
        (*INACTIVE_STATUSES, now_value, now_value, now_value),
    )
    connection.execute(
        "CREATE UNIQUE INDEX current_coverage_tickers_idx "
        "ON current_coverage_tickers(ticker)"
    )
    connection.execute(
        """
        CREATE TEMP TABLE current_coverage_legs AS
        SELECT category, ticker FROM market_legs
        WHERE ticker IN (SELECT ticker FROM current_coverage_tickers)
        """
    )
    connection.execute(
        "CREATE INDEX current_coverage_legs_category_ticker_idx "
        "ON current_coverage_legs(category, ticker)"
    )
    parsed = {
        str(category or "unknown"): (int(leg_count), int(market_count))
        for category, leg_count, market_count in connection.execute(
            """
            SELECT category, count(*), count(DISTINCT ticker)
            FROM current_coverage_legs
            GROUP BY category
            """
        )
    }
    linked: dict[str, int] = {}
    for category, table in LINK_TABLES.items():
        linked[category] = int(
            connection.execute(
                f"""
                SELECT count(DISTINCT ticker) FROM {table}
                WHERE ticker IN (SELECT ticker FROM current_coverage_tickers)
                  AND ticker IN (
                    SELECT ticker FROM current_coverage_legs WHERE category = ?
                  )
                """,
                (category,),
            ).fetchone()[0]
            or 0
        )
    rows = [dict(row) for row in base.get("category_rows", [])]
    for row in rows:
        category = str(row.get("category") or "unknown")
        current_legs, current_markets = parsed.get(category, (0, 0))
        current_linked = min(linked.get(category, 0), current_markets)
        current_unlinked = max(current_markets - current_linked, 0)
        row.update(
            current_parsed_legs=current_legs,
            current_parsed_markets=current_markets,
            current_linked_markets=current_linked,
            current_unlinked_markets=current_unlinked,
            current_coverage_percent=(
                f"{100 * current_linked / current_markets:.1f}%"
                if current_markets else "n/a"
            ),
            current_status_label=(
                "Connected" if current_markets and current_unlinked == 0
                else "Partial" if current_markets else "No Current Markets"
            ),
        )
    actionable = [r for r in rows if int(r.get("current_unlinked_markets") or 0) > 0]
    if actionable:
        top = max(actionable, key=lambda r: int(r["current_unlinked_markets"]))
        bottleneck = {
            "category": top["category"], "status": "UNLINKED",
            "message": f"{top['category']} has {top['current_unlinked_markets']} current parsed market(s) without a specialized link.",
            "next_action": "Run the bounded current-family linker before the next refresh.",
        }
    else:
        bottleneck = {
            "category": None, "status": "CONNECTED",
            "message": "Current parsed market coverage is complete for enabled link families.",
            "next_action": "No current-market link remediation is required.",
        }
    cards = [dict(card) for card in base.get("summary_cards", [])]
    values = {
        "Current Parsed Markets": sum(int(r.get("current_parsed_markets") or 0) for r in rows),
        "Current Unlinked Markets": sum(int(r.get("current_unlinked_markets") or 0) for r in rows),
    }
    by_label = {str(card.get("label")): card for card in cards}
    for label, value in values.items():
        if label in by_label:
            by_label[label]["value"] = value
        else:
            cards.append({"label": label, "value": value,
                          "definition": "Current indexed snapshot."})
    payload = dict(base)
    payload.update(
        generated_at=utc_now().isoformat(), refresh_mode="CURRENT_ONLY_INDEXED",
        refresh_note="Current counts refreshed independently; historical totals remain cached.",
        category_rows=rows, summary_cards=cards, bottleneck=bottleneck,
    )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get(
            "KALSHI_COVERAGE_DATABASE",
            "/home/james/kalshi-predictive-bot-data/kalshi_phase1.db",
        )),
    )
    args = parser.parse_args()
    base = json.loads(args.base.read_text(encoding="utf-8"))
    database_uri = f"file:{args.database.resolve()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True, timeout=30) as connection:
        payload = refresh_current_snapshot(connection, base)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({"status": "COMPLETE", "generated_at": payload["generated_at"]}))


if __name__ == "__main__":
    main()
