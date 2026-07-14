from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json, upsert_market, upsert_settlement
from kalshi_predictor.data.schema import Market, Settlement
from kalshi_predictor.kalshi.client import KalshiClient, KalshiClientError
from kalshi_predictor.paper.models import ORDER_FILLED
from kalshi_predictor.paper.settlement_reconciliation import (
    PAPER_ONLY_SAFETY,
    build_paper_settlement_reconciliation,
)
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now

PHASE3AA_R6_VERSION = "phase3aa_r6_v1"
PHASE3AA_R6_MODE = "PAPER_ONLY_LOCAL_COMPOSITE_SETTLEMENT_RESOLVER"
DEFAULT_OUTPUT_DIR = Path("reports/phase3aa_r6")
LOCAL_DERIVED_TICKER_PREFIXES = (
    "KXMVECROSSCATEGORY-",
    "KXMVESPORTSMULTIGAMEEXTENDED-",
)
LOCAL_COMPOSITE_BLOCK_REASON = "LOCAL_DERIVED_COMPOSITE_NO_EXACT_SETTLEMENT"
SUPPORTED_COMPONENT_SIDES = {"yes", "no"}
SUPPORTED_RULE = "ALL_SELECTED_COMPONENT_SIDES_WIN"
SOURCE_SETTLED_STATUSES = {"settled", "resolved", "finalized"}
SOURCE_CLOSED_STATUSES = {"closed"}


class ExactMarketClient(Protocol):
    def get_market(self, ticker: str) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class Phase3AAR6ArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path


def build_phase3aa_r6_composite_settlement_resolver(
    session: Session,
    *,
    write_settlements: bool = False,
    refresh_components: bool = False,
    component_refresh_limit: int | None = None,
    client: ExactMarketClient | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Resolve local composite tickers only from exact component settlement evidence."""

    session.flush()
    reconciliation = build_paper_settlement_reconciliation(session, limit=limit)
    generated_at = utc_now()
    candidate_rows = _candidate_rows(reconciliation["rows"])
    missing_component_tickers = _missing_component_tickers(session, candidate_rows)
    component_refresh = _component_refresh_skipped(
        missing_component_tickers,
        refresh_components=refresh_components,
        component_refresh_limit=component_refresh_limit,
    )
    if refresh_components and missing_component_tickers:
        component_refresh = _refresh_missing_component_settlements(
            session,
            missing_component_tickers,
            client=client,
            limit=component_refresh_limit,
        )
        session.flush()
    rows = [
        _resolve_composite_row(
            session,
            row,
            generated_at=generated_at,
            write_settlements=write_settlements,
        )
        for row in candidate_rows
    ]
    summary = _summary(
        rows,
        reconciliation,
        write_settlements=write_settlements,
        component_refresh=component_refresh,
    )
    return {
        "generated_at": generated_at.isoformat(),
        "phase": "3AA-R6",
        "phase_version": PHASE3AA_R6_VERSION,
        "mode": PHASE3AA_R6_MODE,
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "safety": {
            "live_or_demo_execution": False,
            "exchange_reads": False,
            "exchange_writes": False,
            "paper_pnl_writes": False,
            "settlement_rows_write_enabled": write_settlements,
            "same_composite_ticker_required": True,
            "component_exact_settlements_required": True,
            "component_refresh_enabled": refresh_components,
            "sibling_resolution_allowed": False,
            "direct_placeholder_upgrades": False,
        },
        "limit": limit,
        "component_refresh_limit": component_refresh_limit,
        "source_reconciliation_summary": reconciliation["summary"],
        "summary": summary,
        "component_refresh": component_refresh,
        "classification_counts": _counts(rows, "classification"),
        "block_reason_counts": _counts(rows, "blocked_reason"),
        "component_count_summary": _component_count_summary(rows),
        "rows": rows,
        "next_commands": _next_commands(summary),
        "recommended_next_action": _recommended_next_action(summary),
    }


def write_phase3aa_r6_composite_settlement_resolver_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    write_settlements: bool = False,
    refresh_components: bool = False,
    component_refresh_limit: int | None = None,
    client: ExactMarketClient | None = None,
    limit: int | None = None,
) -> Phase3AAR6ArtifactSet:
    payload = build_phase3aa_r6_composite_settlement_resolver(
        session,
        write_settlements=write_settlements,
        refresh_components=refresh_components,
        component_refresh_limit=component_refresh_limit,
        client=client,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3aa_r6_composite_settlement_resolver.json"
    markdown_path = output_dir / "phase3aa_r6_composite_settlement_resolver.md"
    rows_path = output_dir / "phase3aa_r6_composite_settlement_resolver_rows.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    rows_path.write_text(
        json.dumps(payload["rows"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3AAR6ArtifactSet(output_dir, json_path, markdown_path, rows_path)


def _candidate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("status") == ORDER_FILLED
        and not row.get("settlement_found")
        and row.get("reason") == LOCAL_COMPOSITE_BLOCK_REASON
        and _is_local_derived_composite_ticker(str(row.get("ticker") or ""))
    ]


def _resolve_composite_row(
    session: Session,
    row: dict[str, Any],
    *,
    generated_at: Any,
    write_settlements: bool,
) -> dict[str, Any]:
    ticker = str(row.get("ticker") or "")
    base = _base_row(row, ticker)
    existing = session.get(Settlement, ticker)
    if existing is not None:
        return {
            **base,
            "classification": "ALREADY_HAS_EXACT_LOCAL_SETTLEMENT",
            "blocked_reason": None,
            "ready_to_write": False,
            "local_settlement_written": False,
            "derived_result": existing.result,
            "derived_yes_settlement_value": existing.yes_settlement_value,
            "component_evidence": [],
        }

    market = session.get(Market, ticker)
    payload = decode_json(market.raw_json) if market is not None else {}
    components = _extract_mve_components(payload)
    component_error = _component_mapping_error(market, payload, components)
    if component_error is not None:
        return {
            **base,
            "classification": "BLOCKED",
            "blocked_reason": component_error,
            "ready_to_write": False,
            "local_settlement_written": False,
            "derived_result": None,
            "derived_yes_settlement_value": None,
            "component_evidence": components,
        }

    component_evidence = [_component_evidence(session, component) for component in components]
    blocked_reason = _component_blocked_reason(component_evidence)
    if blocked_reason is not None:
        return {
            **base,
            "classification": "BLOCKED",
            "blocked_reason": blocked_reason,
            "ready_to_write": False,
            "local_settlement_written": False,
            "derived_result": None,
            "derived_yes_settlement_value": None,
            "component_evidence": component_evidence,
        }

    derived_yes_value = _derived_composite_yes_value(component_evidence)
    result = "yes" if derived_yes_value == Decimal("1") else "no"
    settlement_written = False
    if write_settlements:
        upsert_settlement(
            session,
            _local_settlement_payload(
                ticker=ticker,
                result=result,
                yes_settlement_value=derived_yes_value,
                market=market,
                component_evidence=component_evidence,
                generated_at=generated_at,
            ),
        )
        settlement_written = True

    return {
        **base,
        "classification": "READY" if not settlement_written else "LOCAL_SETTLEMENT_WRITTEN",
        "blocked_reason": None,
        "ready_to_write": True,
        "local_settlement_written": settlement_written,
        "derived_result": result,
        "derived_yes_settlement_value": decimal_to_str(derived_yes_value),
        "component_evidence": component_evidence,
    }


def _base_row(row: dict[str, Any], ticker: str) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "ticker_family": ticker.split("-", 1)[0] if ticker else "UNKNOWN",
        "paper_order_ids": [row.get("paper_order_id")],
        "paper_order_count": 1,
        "doctor_reason": row.get("reason"),
        "market_status": row.get("market_status"),
        "market_close_time": row.get("market_close_time"),
        "close_time_bucket": row.get("close_time_bucket"),
        "resolution_rule": SUPPORTED_RULE,
        "same_composite_ticker_required": True,
        "component_exact_settlements_required": True,
        "paper_pnl_realized": False,
        "live_or_demo_execution": False,
        "exchange_writes": False,
    }


def _missing_component_tickers(
    session: Session,
    candidate_rows: list[dict[str, Any]],
) -> list[str]:
    tickers: set[str] = set()
    for row in candidate_rows:
        ticker = str(row.get("ticker") or "")
        market = session.get(Market, ticker)
        payload = decode_json(market.raw_json) if market is not None else {}
        components = _extract_mve_components(payload)
        if _component_mapping_error(market, payload, components) is not None:
            continue
        for component in components:
            component_ticker = str(component.get("component_ticker") or "")
            if not component_ticker or _is_local_derived_composite_ticker(component_ticker):
                continue
            if session.get(Settlement, component_ticker) is None:
                tickers.add(component_ticker)
    return sorted(tickers)


def _component_refresh_skipped(
    missing_component_tickers: list[str],
    *,
    refresh_components: bool,
    component_refresh_limit: int | None,
) -> dict[str, Any]:
    return {
        "enabled": refresh_components,
        "limit": component_refresh_limit,
        "missing_component_tickers_before": len(missing_component_tickers),
        "component_tickers_checked": 0,
        "exact_market_rows_written": 0,
        "exact_settlement_rows_written": 0,
        "fetch_errors": 0,
        "identity_mismatches": 0,
        "source_not_settled": 0,
        "source_closed_without_outcome": 0,
        "source_settled_without_usable_outcome": 0,
        "skipped_already_settled": 0,
        "status_counts": {},
        "rows": [],
    }


def _refresh_missing_component_settlements(
    session: Session,
    missing_component_tickers: list[str],
    *,
    client: ExactMarketClient | None,
    limit: int | None,
) -> dict[str, Any]:
    tickers = missing_component_tickers[:limit] if limit is not None else missing_component_tickers
    owns_client = client is None
    resolved_client: ExactMarketClient = client or KalshiClient()
    rows: list[dict[str, Any]] = []
    try:
        for ticker in tickers:
            rows.append(_refresh_one_component_ticker(session, resolved_client, ticker))
    finally:
        close = getattr(resolved_client, "close", None)
        if owns_client and callable(close):
            close()
    return {
        "enabled": True,
        "limit": limit,
        "missing_component_tickers_before": len(missing_component_tickers),
        "component_tickers_checked": len(rows),
        "exact_market_rows_written": sum(1 for row in rows if row["exact_market_written"]),
        "exact_settlement_rows_written": sum(
            1 for row in rows if row["exact_settlement_written"]
        ),
        "fetch_errors": sum(1 for row in rows if row["source_fetch_status"] == "FETCH_ERROR"),
        "identity_mismatches": sum(
            1 for row in rows if row["source_fetch_status"] == "TICKER_IDENTITY_MISMATCH"
        ),
        "source_not_settled": sum(
            1 for row in rows if row["source_fetch_status"] == "SOURCE_NOT_SETTLED"
        ),
        "source_closed_without_outcome": sum(
            1 for row in rows if row["source_fetch_status"] == "SOURCE_CLOSED_WITHOUT_OUTCOME"
        ),
        "source_settled_without_usable_outcome": sum(
            1
            for row in rows
            if row["source_fetch_status"] == "SOURCE_SETTLED_WITHOUT_USABLE_OUTCOME"
        ),
        "skipped_already_settled": sum(
            1 for row in rows if row["source_fetch_status"] == "ALREADY_HAS_EXACT_SETTLEMENT"
        ),
        "status_counts": _counts(rows, "source_fetch_status"),
        "rows": rows,
    }


def _refresh_one_component_ticker(
    session: Session,
    client: ExactMarketClient,
    ticker: str,
) -> dict[str, Any]:
    row = {
        "ticker": ticker,
        "ticker_family": ticker.split("-", 1)[0] if ticker else "UNKNOWN",
        "source_ticker": None,
        "source_status": None,
        "source_result": None,
        "source_yes_settlement_value": None,
        "source_fetch_status": None,
        "exact_market_written": False,
        "exact_settlement_written": False,
        "paper_pnl_realized": False,
        "live_or_demo_execution": False,
        "exchange_writes": False,
    }
    if session.get(Settlement, ticker) is not None:
        row["source_fetch_status"] = "ALREADY_HAS_EXACT_SETTLEMENT"
        return row
    if _is_local_derived_composite_ticker(ticker):
        row["source_fetch_status"] = "LOCAL_DERIVED_COMPONENT_SKIPPED"
        return row

    try:
        market = client.get_market(ticker)
    except KalshiClientError as exc:
        row.update({"source_fetch_status": "FETCH_ERROR", "error": str(exc)})
        return row

    if not isinstance(market, Mapping):
        row.update(
            {
                "source_fetch_status": "INVALID_RESPONSE",
                "error": "Kalshi exact market response was not an object.",
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
                "error": "Exact endpoint returned a missing or different component ticker.",
            }
        )
        return row

    upsert_market(session, payload)
    row["exact_market_written"] = True
    if _has_usable_outcome(payload):
        settlement = upsert_settlement(session, payload)
        row.update(
            {
                "source_fetch_status": "EXACT_COMPONENT_SETTLEMENT_WRITTEN",
                "exact_settlement_written": True,
                "settlement_result": settlement.result,
                "settlement_yes_value": settlement.yes_settlement_value,
                "settled_at": settlement.settled_at.isoformat()
                if settlement.settled_at
                else None,
            }
        )
    elif _source_is_closed_without_outcome(payload):
        row["source_fetch_status"] = "SOURCE_CLOSED_WITHOUT_OUTCOME"
    elif _source_is_settled(payload):
        row["source_fetch_status"] = "SOURCE_SETTLED_WITHOUT_USABLE_OUTCOME"
    else:
        row["source_fetch_status"] = "SOURCE_NOT_SETTLED"
    return row


def _source_fields(payload: Mapping[str, Any], source_ticker: str) -> dict[str, Any]:
    return {
        "source_ticker": source_ticker or None,
        "source_status": payload.get("status"),
        "source_result": payload.get("result"),
        "source_settlement_value_dollars": payload.get("settlement_value_dollars"),
        "source_settlement_value": payload.get("settlement_value"),
        "source_yes_settlement_value": payload.get("yes_settlement_value"),
        "source_settlement_ts": (
            payload.get("settlement_ts") or payload.get("settled_time") or payload.get("settled_at")
        ),
        "source_event_ticker": payload.get("event_ticker"),
        "source_series_ticker": payload.get("series_ticker"),
        "source_title": payload.get("title"),
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


def _extract_mve_components(payload: dict[str, Any]) -> list[dict[str, Any]]:
    selected_legs = payload.get("mve_selected_legs")
    if not isinstance(selected_legs, list):
        return []

    components = []
    for index, item in enumerate(selected_legs):
        if not isinstance(item, dict):
            components.append(
                {
                    "leg_index": index,
                    "component_ticker": None,
                    "selected_side": None,
                    "component_event_ticker": None,
                    "mapping_status": "INVALID_COMPONENT_PAYLOAD",
                }
            )
            continue
        component_ticker = _text(
            item.get("market_ticker")
            or item.get("ticker")
            or item.get("component_ticker")
            or item.get("underlying_ticker")
        )
        selected_side = _normalize_side(item.get("side") or item.get("selected_side"))
        components.append(
            {
                "leg_index": index,
                "component_ticker": component_ticker,
                "selected_side": selected_side,
                "component_event_ticker": _text(item.get("event_ticker")),
                "mapping_status": "OK"
                if component_ticker and selected_side in SUPPORTED_COMPONENT_SIDES
                else "INVALID_COMPONENT_MAPPING",
            }
        )
    return components


def _component_mapping_error(
    market: Market | None,
    payload: dict[str, Any],
    components: list[dict[str, Any]],
) -> str | None:
    if market is None:
        return "MARKET_PAYLOAD_MISSING"
    if "mve_selected_legs" not in payload:
        return "MVE_SELECTED_LEGS_MISSING"
    if not components:
        return "MVE_SELECTED_LEGS_EMPTY"
    if any(component.get("mapping_status") != "OK" for component in components):
        return "INVALID_COMPONENT_MAPPING"
    if any(
        _is_local_derived_composite_ticker(str(component.get("component_ticker") or ""))
        for component in components
    ):
        return "COMPONENT_IS_LOCAL_DERIVED"
    return None


def _component_evidence(session: Session, component: dict[str, Any]) -> dict[str, Any]:
    ticker = str(component.get("component_ticker") or "")
    selected_side = str(component.get("selected_side") or "")
    settlement = session.get(Settlement, ticker) if ticker else None
    yes_value = _settlement_yes_value(settlement)
    selected_side_won = _selected_side_won(selected_side, yes_value)
    return {
        **component,
        "exact_settlement_found": settlement is not None,
        "settlement_result": settlement.result if settlement is not None else None,
        "settlement_yes_value": settlement.yes_settlement_value if settlement is not None else None,
        "settled_at": settlement.settled_at.isoformat()
        if settlement is not None and settlement.settled_at
        else None,
        "outcome_binary": yes_value in {Decimal("0"), Decimal("1")},
        "normalized_yes_value": decimal_to_str(yes_value),
        "selected_side_won": selected_side_won,
    }


def _component_blocked_reason(component_evidence: list[dict[str, Any]]) -> str | None:
    if any(not component.get("component_ticker") for component in component_evidence):
        return "INVALID_COMPONENT_MAPPING"
    if any(
        component.get("selected_side") not in SUPPORTED_COMPONENT_SIDES
        for component in component_evidence
    ):
        return "COMPONENT_SIDE_UNSUPPORTED"
    if any(not component.get("exact_settlement_found") for component in component_evidence):
        return "MISSING_COMPONENT_SETTLEMENTS"
    if any(not component.get("outcome_binary") for component in component_evidence):
        return "COMPONENT_OUTCOME_NOT_BINARY"
    if any(component.get("selected_side_won") is None for component in component_evidence):
        return "COMPONENT_OUTCOME_UNUSABLE"
    return None


def _derived_composite_yes_value(component_evidence: list[dict[str, Any]]) -> Decimal:
    if all(component["selected_side_won"] is True for component in component_evidence):
        return Decimal("1")
    return Decimal("0")


def _local_settlement_payload(
    *,
    ticker: str,
    result: str,
    yes_settlement_value: Decimal,
    market: Market | None,
    component_evidence: list[dict[str, Any]],
    generated_at: Any,
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "status": "settled",
        "result": result,
        "yes_settlement_value": decimal_to_str(yes_settlement_value),
        "settlement_ts": generated_at.isoformat(),
        "event_ticker": market.event_ticker if market is not None else None,
        "series_ticker": market.series_ticker if market is not None else None,
        "title": market.title if market is not None else None,
        "source": "phase3aa_r6_local_composite_settlement_resolver",
        "local_composite_settlement": {
            "phase": "3AA-R6",
            "phase_version": PHASE3AA_R6_VERSION,
            "resolution_rule": SUPPORTED_RULE,
            "same_composite_ticker_required": True,
            "component_exact_settlements_required": True,
            "component_count": len(component_evidence),
            "component_settlement_evidence": component_evidence,
            "paper_pnl_realized": False,
            "live_or_demo_execution": False,
            "exchange_writes": False,
        },
    }


def _settlement_yes_value(settlement: Settlement | None) -> Decimal | None:
    if settlement is None:
        return None
    value = to_decimal(settlement.yes_settlement_value)
    if value is not None and Decimal("0") <= value <= Decimal("1"):
        return value
    normalized = _normalize_result(settlement.result)
    if normalized == "yes":
        return Decimal("1")
    if normalized == "no":
        return Decimal("0")
    return None


def _selected_side_won(selected_side: str, yes_value: Decimal | None) -> bool | None:
    if yes_value not in {Decimal("0"), Decimal("1")}:
        return None
    if selected_side == "yes":
        return yes_value == Decimal("1")
    if selected_side == "no":
        return yes_value == Decimal("0")
    return None


def _normalize_result(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"yes", "y", "1", "true"}:
        return "yes"
    if normalized in {"no", "n", "0", "false"}:
        return "no"
    return normalized or None


def _normalize_side(value: object) -> str | None:
    normalized = _normalize_result(value)
    return normalized if normalized in SUPPORTED_COMPONENT_SIDES else None


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _summary(
    rows: list[dict[str, Any]],
    reconciliation: dict[str, Any],
    *,
    write_settlements: bool,
    component_refresh: dict[str, Any],
) -> dict[str, Any]:
    ready_rows = [row for row in rows if row.get("ready_to_write")]
    blocked_rows = [row for row in rows if row.get("classification") == "BLOCKED"]
    return {
        "composite_rows_reviewed": len(rows),
        "local_derived_missing_exact_settlement_before": reconciliation["reason_counts"].get(
            LOCAL_COMPOSITE_BLOCK_REASON,
            0,
        ),
        "ready_to_write_rows": len(ready_rows),
        "blocked_rows": len(blocked_rows),
        "settlements_written": sum(1 for row in rows if row.get("local_settlement_written")),
        "write_settlements_enabled": write_settlements,
        "dry_run": not write_settlements,
        "component_refresh_enabled": component_refresh["enabled"],
        "component_tickers_missing_before_refresh": component_refresh[
            "missing_component_tickers_before"
        ],
        "component_tickers_checked": component_refresh["component_tickers_checked"],
        "component_exact_market_rows_written": component_refresh[
            "exact_market_rows_written"
        ],
        "component_exact_settlement_rows_written": component_refresh[
            "exact_settlement_rows_written"
        ],
        "component_fetch_errors": component_refresh["fetch_errors"],
        "component_source_not_settled": component_refresh["source_not_settled"],
        "paper_pnl_realized": False,
        "live_or_demo_execution": False,
        "exchange_writes": False,
        "sibling_tickers_used_for_settlement": 0,
        "same_composite_ticker_required": True,
        "component_exact_settlements_required": True,
        "missing_component_settlements": sum(
            1 for row in rows if row.get("blocked_reason") == "MISSING_COMPONENT_SETTLEMENTS"
        ),
        "missing_component_mapping": sum(
            1
            for row in rows
            if row.get("blocked_reason")
            in {
                "MARKET_PAYLOAD_MISSING",
                "MVE_SELECTED_LEGS_MISSING",
                "MVE_SELECTED_LEGS_EMPTY",
                "INVALID_COMPONENT_MAPPING",
            }
        ),
        "component_outcome_not_binary": sum(
            1 for row in rows if row.get("blocked_reason") == "COMPONENT_OUTCOME_NOT_BINARY"
        ),
        "unsupported_component_side": sum(
            1 for row in rows if row.get("blocked_reason") == "COMPONENT_SIDE_UNSUPPORTED"
        ),
        "derived_yes_rows": sum(
            1 for row in ready_rows if row.get("derived_yes_settlement_value") == "1"
        ),
        "derived_no_rows": sum(
            1 for row in ready_rows if row.get("derived_yes_settlement_value") == "0"
        ),
    }


def _counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "none")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _component_count_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = [len(row.get("component_evidence") or []) for row in rows]
    if not counts:
        return {"min": 0, "max": 0, "total": 0}
    return {"min": min(counts), "max": max(counts), "total": sum(counts)}


def _next_commands(summary: dict[str, Any]) -> list[str]:
    commands = [
        "kalshi-bot phase3aa-r6-composite-settlement-resolver --output-dir reports/phase3aa_r6",
    ]
    if (
        summary["component_tickers_missing_before_refresh"] > 0
        and not summary["component_refresh_enabled"]
    ):
        commands.append(
            "kalshi-bot phase3aa-r6-composite-settlement-resolver "
            "--output-dir reports/phase3aa_r6 --refresh-components"
        )
    if summary["ready_to_write_rows"] > 0 and summary["settlements_written"] == 0:
        commands.append(
            "kalshi-bot phase3aa-r6-composite-settlement-resolver "
            "--output-dir reports/phase3aa_r6 --refresh-components --write-settlements"
        )
    if summary["settlements_written"] > 0:
        commands.append("kalshi-bot phase3aa-realize --dry-run --no-sync-settlements")
    commands.extend(
        [
            "kalshi-bot paper-settlement-doctor --output-dir "
            "reports/paper_settlement_reconciliation",
            "kalshi-bot phase3aa-r2-exact-settlement-harvest --output-dir reports/phase3aa_r2",
        ]
    )
    return commands


def _recommended_next_action(summary: dict[str, Any]) -> str:
    if summary["settlements_written"] > 0:
        return (
            "Local composite settlement rows were written for exact composite tickers. "
            "Run Phase 3AA realization in dry-run mode before any no-dry-run P&L pass."
        )
    if summary["ready_to_write_rows"] > 0:
        return (
            "Composite rows have complete exact component settlement evidence. Review "
            "the rows, then rerun with --write-settlements to create auditable local "
            "settlement rows for the same composite tickers."
        )
    if (
        summary["component_tickers_missing_before_refresh"] > 0
        and not summary["component_refresh_enabled"]
    ):
        return (
            "Composite rows are mapped, but component tickers have not been refreshed. "
            "Rerun with --refresh-components to fetch exact component settlement evidence."
        )
    if summary["component_exact_settlement_rows_written"] > 0:
        return (
            "Exact component settlement rows were written, but some composites are still "
            "blocked. Rerun the resolver after additional component outcomes settle."
        )
    if summary["missing_component_settlements"] > 0:
        return (
            "Composite rows are mapped, but at least one underlying component settlement "
            "is missing. Keep the exact-ticker harvest and settlement watch running."
        )
    if summary["missing_component_mapping"] > 0:
        return (
            "Some composite rows lack usable mve_selected_legs mappings. Keep settlement "
            "blocked until component mapping evidence is present."
        )
    return (
        "No eligible local composite settlement rows were found. Keep settlement blocked "
        "and continue the exact settlement watch."
    )


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AA-R6 Composite Settlement Resolver",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Write settlements enabled: {str(summary['write_settlements_enabled']).lower()}",
        "- Live/demo execution: false",
        "- Paper P&L realized: false",
        "- Exchange writes: false",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Composite Rows",
            "",
            "| Ticker | Components | Classification | Blocked reason | Derived result | Written |",
            "| --- | ---: | --- | --- | --- | --- |",
        ]
    )
    for row in payload["rows"][:50]:
        lines.append(
            f"| {_md(row.get('ticker'))} | {len(row.get('component_evidence') or [])} | "
            f"{_md(row.get('classification'))} | {_md(row.get('blocked_reason'))} | "
            f"{_md(row.get('derived_result'))} | "
            f"{str(bool(row.get('local_settlement_written'))).lower()} |"
        )
    if len(payload["rows"]) > 50:
        lines.append(f"| ... | {len(payload['rows']) - 50} more |  |  |  |  |")
    lines.extend(["", "## Next Action", "", payload["recommended_next_action"]])
    return "\n".join(lines) + "\n"


def _md(value: Any) -> str:
    if value is None or value == "":
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")


def _is_local_derived_composite_ticker(ticker: str) -> bool:
    return ticker.startswith(LOCAL_DERIVED_TICKER_PREFIXES)
