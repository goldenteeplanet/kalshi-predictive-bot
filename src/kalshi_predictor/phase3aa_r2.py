from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import upsert_market, upsert_settlement
from kalshi_predictor.data.schema import Settlement
from kalshi_predictor.kalshi.client import KalshiClient, KalshiClientError
from kalshi_predictor.paper.models import ORDER_FILLED
from kalshi_predictor.paper.settlement_reconciliation import (
    PAPER_ONLY_SAFETY,
    build_paper_settlement_reconciliation,
)
from kalshi_predictor.utils.time import utc_now

PHASE3AA_R2_MODE = "PAPER_ONLY_EXACT_TICKER_SETTLEMENT_HARVEST"
EXACT_TICKER_POLICY = (
    "Fetch only the exact paper order ticker. Never settle paper P&L from sibling "
    "or different contract-leg tickers."
)
SOURCE_SETTLED_STATUSES = {"settled", "resolved", "finalized"}
SOURCE_CLOSED_STATUSES = {"closed"}
LOCAL_DERIVED_TICKER_PREFIXES = (
    "KXMVECROSSCATEGORY-",
    "KXMVESPORTSMULTIGAMEEXTENDED-",
)


class ExactMarketClient(Protocol):
    def get_market(self, ticker: str) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class Phase3AAR2ArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path


def run_exact_ticker_settlement_harvest(
    session: Session,
    *,
    client: ExactMarketClient | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Harvest settlement evidence for due paper orders by exact ticker only."""

    session.flush()
    before = build_paper_settlement_reconciliation(session, limit=limit)
    candidate_rows = _due_unsettled_rows(before["rows"])
    by_ticker = _rows_by_ticker(candidate_rows)
    owns_client = client is None
    resolved_client: ExactMarketClient = client or KalshiClient()
    rows: list[dict[str, Any]] = []

    try:
        for ticker, ticker_rows in by_ticker.items():
            rows.append(_harvest_one_ticker(session, resolved_client, ticker, ticker_rows))
    finally:
        close = getattr(resolved_client, "close", None)
        if owns_client and callable(close):
            close()

    session.flush()
    after = build_paper_settlement_reconciliation(session, limit=limit)
    summary = _summary(before, after, candidate_rows, by_ticker, rows)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AA-R2",
        "mode": PHASE3AA_R2_MODE,
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "exact_ticker_policy": EXACT_TICKER_POLICY,
        "limit": limit,
        "summary": summary,
        "before": before["summary"],
        "after": after["summary"],
        "rows": rows,
        "recommended_next_action": _recommended_next_action(summary),
    }


def write_phase3aa_r2_exact_settlement_harvest_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3aa_r2"),
    client: ExactMarketClient | None = None,
    limit: int | None = None,
) -> Phase3AAR2ArtifactSet:
    payload = run_exact_ticker_settlement_harvest(session, client=client, limit=limit)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3aa_r2_exact_settlement_harvest.json"
    markdown_path = output_dir / "phase3aa_r2_exact_settlement_harvest.md"
    rows_path = output_dir / "phase3aa_r2_exact_settlement_harvest_rows.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    rows_path.write_text(
        json.dumps(payload["rows"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3AAR2ArtifactSet(output_dir, json_path, markdown_path, rows_path)


def _due_unsettled_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("status") == ORDER_FILLED
        and not row.get("settlement_found")
        and row.get("close_time_bucket") in {"overdue", "0-6h"}
    ]


def _rows_by_ticker(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "").strip()
        if not ticker:
            continue
        grouped.setdefault(ticker, []).append(row)
    return dict(sorted(grouped.items()))


def _harvest_one_ticker(
    session: Session,
    client: ExactMarketClient,
    ticker: str,
    paper_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    row = _base_harvest_row(ticker, paper_rows)
    if _is_local_derived_composite_ticker(ticker):
        row.update(
            {
                "source_fetch_status": "LOCAL_DERIVED_TICKER_NOT_EXCHANGE_MARKET",
                "ticker_family": _ticker_family(ticker),
                "retryable": False,
                "error": (
                    "This paper order ticker is a local derived composite family, not a "
                    "direct Kalshi exchange market ticker. The exact Kalshi market endpoint "
                    "is expected to return 404, so Phase 3AA-R2 will not spend API calls on "
                    "it. Paper P&L remains blocked until an exact local settlement row exists "
                    "for this same ticker."
                ),
                "exact_market_written": False,
                "exact_settlement_written": False,
                "paper_pnl_realized": False,
            }
        )
        return row
    try:
        market = client.get_market(ticker)
    except KalshiClientError as exc:
        row.update(
            {
                "source_fetch_status": "FETCH_ERROR",
                "ticker_family": _ticker_family(ticker),
                "retryable": True,
                "error": str(exc),
                "exact_market_written": False,
                "exact_settlement_written": False,
                "paper_pnl_realized": False,
            }
        )
        return row

    if not isinstance(market, Mapping):
        row.update(
            {
                "source_fetch_status": "INVALID_RESPONSE",
                "error": "Kalshi exact market response was not an object.",
                "exact_market_written": False,
                "exact_settlement_written": False,
                "paper_pnl_realized": False,
            }
        )
        return row

    payload = dict(market)
    source_ticker = str(payload.get("ticker") or "").strip()
    row.update(_source_fields(payload, source_ticker))
    if source_ticker != ticker:
        row.update(
            {
                "source_fetch_status": "TICKER_IDENTITY_MISMATCH",
                "identity_match": False,
                "error": (
                    "Exact endpoint returned a missing or different ticker; no settlement "
                    "row was written."
                ),
                "exact_market_written": False,
                "exact_settlement_written": False,
                "paper_pnl_realized": False,
            }
        )
        return row

    row["identity_match"] = True
    upsert_market(session, payload)
    row["exact_market_written"] = True
    if _has_usable_outcome(payload):
        settlement = upsert_settlement(session, payload)
        row.update(
            {
                "source_fetch_status": "EXACT_SETTLEMENT_WRITTEN",
                "exact_settlement_written": True,
                "settlement_result": settlement.result,
                "settlement_yes_value": settlement.yes_settlement_value,
                "settled_at": settlement.settled_at.isoformat()
                if settlement.settled_at
                else None,
                "paper_pnl_realized": False,
            }
        )
    elif _source_is_closed_without_outcome(payload):
        row.update(
            {
                "source_fetch_status": "SOURCE_CLOSED_WITHOUT_OUTCOME",
                "exact_settlement_written": False,
                "paper_pnl_realized": False,
            }
        )
    elif _source_is_settled(payload):
        row.update(
            {
                "source_fetch_status": "SOURCE_SETTLED_WITHOUT_USABLE_OUTCOME",
                "exact_settlement_written": False,
                "paper_pnl_realized": False,
            }
        )
    else:
        row.update(
            {
                "source_fetch_status": "SOURCE_NOT_SETTLED",
                "exact_settlement_written": False,
                "paper_pnl_realized": False,
            }
        )
    return row


def _base_harvest_row(ticker: str, paper_rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "ticker_family": _ticker_family(ticker),
        "paper_order_ids": [row.get("paper_order_id") for row in paper_rows],
        "paper_order_count": len(paper_rows),
        "before_reasons": sorted({str(row.get("reason")) for row in paper_rows}),
        "before_market_statuses": sorted(
            {str(row.get("market_status")) for row in paper_rows if row.get("market_status")}
        ),
        "before_close_time_buckets": sorted(
            {
                str(row.get("close_time_bucket"))
                for row in paper_rows
                if row.get("close_time_bucket")
            }
        ),
    }


def _is_local_derived_composite_ticker(ticker: str) -> bool:
    return ticker.startswith(LOCAL_DERIVED_TICKER_PREFIXES)


def _ticker_family(ticker: str) -> str:
    return ticker.split("-", 1)[0] if ticker else "UNKNOWN"


def _source_fields(payload: Mapping[str, Any], source_ticker: str) -> dict[str, Any]:
    return {
        "source_ticker": source_ticker or None,
        "source_status": payload.get("status"),
        "source_result": payload.get("result"),
        "source_settlement_value_dollars": payload.get("settlement_value_dollars"),
        "source_settlement_value": payload.get("settlement_value"),
        "source_yes_settlement_value": payload.get("yes_settlement_value"),
        "source_expiration_value": payload.get("expiration_value"),
        "source_settlement_ts": (
            payload.get("settlement_ts") or payload.get("settled_time") or payload.get("settled_at")
        ),
        "source_latest_expiration_time": payload.get("latest_expiration_time"),
        "source_expected_expiration_time": payload.get("expected_expiration_time"),
        "source_close_time": payload.get("close_time"),
        "source_settlement_timer_seconds": payload.get("settlement_timer_seconds"),
        "source_event_ticker": payload.get("event_ticker"),
        "source_series_ticker": payload.get("series_ticker"),
        "source_title": payload.get("title"),
        "source_subtitle": payload.get("subtitle"),
        "source_has_custom_strike": isinstance(payload.get("custom_strike"), Mapping),
        "source_custom_strike_keys": _custom_strike_keys(payload),
        "source_associated_events_count": _custom_strike_count(payload, "associated_events"),
        "source_associated_markets_count": _custom_strike_count(payload, "associated_markets"),
        "source_associated_market_sides_count": _custom_strike_count(
            payload,
            "associated_market_sides",
        ),
        "source_mve_selected_legs_count": _list_count(payload.get("mve_selected_legs")),
    }


def _has_usable_outcome(payload: Mapping[str, Any]) -> bool:
    result = payload.get("result")
    if result is not None and str(result).strip():
        return True
    return (
        payload.get("settlement_value_dollars") is not None
        or payload.get("settlement_value") is not None
        or payload.get("yes_settlement_value") is not None
    )


def _source_is_closed_without_outcome(payload: Mapping[str, Any]) -> bool:
    status = str(payload.get("status") or "").strip().lower()
    return status in SOURCE_CLOSED_STATUSES and not _has_usable_outcome(payload)


def _source_is_settled(payload: Mapping[str, Any]) -> bool:
    status = str(payload.get("status") or "").strip().lower()
    return status in SOURCE_SETTLED_STATUSES or bool(
        payload.get("settlement_ts") or payload.get("settled_time") or payload.get("settled_at")
    )


def _custom_strike_keys(payload: Mapping[str, Any]) -> list[str]:
    custom_strike = payload.get("custom_strike")
    if not isinstance(custom_strike, Mapping):
        return []
    return sorted(str(key) for key in custom_strike.keys())


def _custom_strike_count(payload: Mapping[str, Any], key: str) -> int:
    custom_strike = payload.get("custom_strike")
    if not isinstance(custom_strike, Mapping):
        return 0
    return _list_count(custom_strike.get(key))


def _list_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, Mapping):
        return len(value)
    return 0


def _summary(
    before: dict[str, Any],
    after: dict[str, Any],
    candidate_rows: list[dict[str, Any]],
    by_ticker: dict[str, list[dict[str, Any]]],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    fetch_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    for row in rows:
        fetch_status = str(row.get("source_fetch_status") or "UNKNOWN")
        fetch_counts[fetch_status] = fetch_counts.get(fetch_status, 0) + 1
        source_status = str(row.get("source_status") or "unknown")
        status_counts[source_status] = status_counts.get(source_status, 0) + 1
    return {
        "due_or_overdue_rows_reviewed": len(candidate_rows),
        "exact_tickers_checked": len(by_ticker),
        "exact_market_rows_written": sum(1 for row in rows if row.get("exact_market_written")),
        "exact_settlements_written": sum(
            1 for row in rows if row.get("exact_settlement_written")
        ),
        "source_settled_without_usable_outcome": fetch_counts.get(
            "SOURCE_SETTLED_WITHOUT_USABLE_OUTCOME",
            0,
        ),
        "source_closed_without_outcome": fetch_counts.get(
            "SOURCE_CLOSED_WITHOUT_OUTCOME",
            0,
        ),
        "source_not_settled": fetch_counts.get("SOURCE_NOT_SETTLED", 0),
        "fetch_errors": fetch_counts.get("FETCH_ERROR", 0),
        "retryable_fetch_errors": sum(
            1
            for row in rows
            if row.get("source_fetch_status") == "FETCH_ERROR" and row.get("retryable") is True
        ),
        "local_derived_not_exchange_market": fetch_counts.get(
            "LOCAL_DERIVED_TICKER_NOT_EXCHANGE_MARKET",
            0,
        ),
        "identity_mismatches": fetch_counts.get("TICKER_IDENTITY_MISMATCH", 0),
        "fetch_status_counts": dict(sorted(fetch_counts.items())),
        "source_status_counts": dict(sorted(status_counts.items())),
        "eligible_exact_settlements_before": before["summary"]["eligible_to_settle_now"],
        "eligible_exact_settlements_after": after["summary"]["eligible_to_settle_now"],
        "active_unsettled_after": after["summary"]["still_open_or_unsettled"],
        "pnl_realized": False,
        "live_or_demo_execution": False,
        "live_orders_created": 0,
        "sibling_tickers_used_for_settlement": 0,
        "local_settlement_row_count": _local_settlement_count(rows),
    }


def _local_settlement_count(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if row.get("exact_settlement_written"))


def _recommended_next_action(summary: dict[str, Any]) -> str:
    if summary["exact_settlements_written"] > 0:
        return (
            "Exact settlement rows were harvested. Run "
            "kalshi-bot phase3aa-realize --no-dry-run --no-sync-settlements."
        )
    if summary["source_closed_without_outcome"] > 0:
        return (
            "Some exact markets are closed but expose no usable settlement outcome. Run "
            "kalshi-bot phase3aa-r5-closed-market-outcome-capture --output-dir "
            "reports/phase3aa_r5 before changing parser behavior."
        )
    if summary.get("local_derived_not_exchange_market", 0) > 0 and summary["fetch_errors"] == 0:
        return (
            "Remaining due paper rows are local derived composite tickers, not direct Kalshi "
            "exchange markets. Keep settlement blocked until exact local settlement evidence "
            "exists for the same ticker; do not realize from sibling or component tickers."
        )
    if summary["fetch_errors"] > 0:
        return (
            "Some exact ticker fetches failed. Review the harvest rows, then rerun the "
            "targeted harvest before realizing P&L."
        )
    return (
        "No exact ticker settlement evidence was found. Keep the settlement watch active; "
        "do not realize P&L from sibling or different contract-leg tickers."
    )


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AA-R2 Exact Settlement Harvest",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Exact ticker policy: {payload['exact_ticker_policy']}",
        "- Paper P&L realized: false",
        "- Live/demo execution: false",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Harvest Rows",
            "",
            "| Ticker | Paper orders | Fetch status | Source status | Result | Settlement value |",
            "| --- | ---: | --- | --- | --- | --- |",
        ]
    )
    for row in payload["rows"][:50]:
        settlement_value = row.get("source_settlement_value_dollars") or row.get(
            "source_yes_settlement_value"
        )
        lines.append(
            f"| {_md(row.get('ticker'))} | {row.get('paper_order_count', 0)} | "
            f"{_md(row.get('source_fetch_status'))} | {_md(row.get('source_status'))} | "
            f"{_md(row.get('source_result'))} | {_md(settlement_value)} |"
        )
    if len(payload["rows"]) > 50:
        lines.append(f"| ... | {len(payload['rows']) - 50} more |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Next Action",
            "",
            payload["recommended_next_action"],
        ]
    )
    return "\n".join(lines) + "\n"


def _md(value: Any) -> str:
    if value is None or value == "":
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")


def exact_settlement_exists(session: Session, ticker: str) -> bool:
    return session.get(Settlement, ticker) is not None
