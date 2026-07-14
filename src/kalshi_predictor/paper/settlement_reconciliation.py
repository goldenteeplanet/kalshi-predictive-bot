from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import Market, MarketLeg, PaperOrder, PaperPnl, Settlement
from kalshi_predictor.paper.models import BUY_NO, BUY_YES, ORDER_FILLED, ORDER_OPEN
from kalshi_predictor.utils.time import parse_datetime, utc_now

PAPER_ONLY_SAFETY = "PAPER_ONLY_NO_EXCHANGE_WRITES"
SUPPORTED_SETTLEMENT_SIDES = {BUY_YES, BUY_NO}
OPEN_MARKET_STATUSES = {"open", "unopened", "paused", "active"}
LOCAL_DERIVED_TICKER_PREFIXES = (
    "KXMVECROSSCATEGORY-",
    "KXMVESPORTSMULTIGAMEEXTENDED-",
)


@dataclass(frozen=True)
class PaperSettlementArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path


def build_paper_settlement_reconciliation(
    session: Session,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    statement = select(PaperOrder).order_by(desc(PaperOrder.created_at), desc(PaperOrder.id))
    if limit is not None and limit > 0:
        statement = statement.limit(limit)
    orders = list(session.scalars(statement))
    rows = [_order_reconciliation_row(session, order) for order in orders]
    counts = _reason_counts(rows)
    eligible_rows = [row for row in rows if row["eligible_to_settle_now"]]
    close_buckets = _close_time_buckets(rows)
    sibling_summary = _sibling_summary(rows)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3Y-SR",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "limit": limit,
        "summary": {
            "orders_reviewed": len(rows),
            "filled_orders_reviewed": sum(1 for row in rows if row["status"] == ORDER_FILLED),
            "open_orders_reviewed": sum(1 for row in rows if row["status"] == ORDER_OPEN),
            "exact_settlement_matches": sum(1 for row in rows if row["settlement_found"]),
            "missing_exact_settlement": sum(1 for row in rows if not row["settlement_found"]),
            "eligible_to_settle_now": len(eligible_rows),
            "side_mapping_blocked": counts.get("SIDE_MAPPING_UNSUPPORTED", 0),
            "possible_ticker_mismatches": counts.get("POSSIBLE_TICKER_MISMATCH", 0),
            "sibling_different_contract_leg": counts.get("SIBLING_DIFFERENT_CONTRACT_LEG", 0),
            "validated_sibling_requires_review": counts.get(
                "VALIDATED_SIBLING_REQUIRES_MANUAL_REVIEW",
                0,
            ),
            "malformed_tickers": counts.get("MALFORMED_TICKER", 0),
            "still_open_or_unsettled": counts.get("MARKET_STILL_OPEN", 0)
            + counts.get("NO_SETTLEMENT_YET", 0),
            "active_unsettled_close_time_buckets": close_buckets,
        },
        "sibling_summary": sibling_summary,
        "close_time_buckets": close_buckets,
        "learning_slow_settlement_guidance": _learning_slow_settlement_guidance(
            rows,
            close_buckets,
        ),
        "top_reason": _top_reason(counts),
        "reason_counts": counts,
        "eligible_trades": eligible_rows[:50],
        "rows": rows,
        "recommended_next_action": _recommended_next_action(counts, eligible_rows),
    }


def write_paper_settlement_reconciliation(
    session: Session,
    *,
    output_dir: Path = Path("reports/paper_settlement_reconciliation"),
    limit: int | None = None,
) -> PaperSettlementArtifactSet:
    payload = build_paper_settlement_reconciliation(session, limit=limit)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "paper_settlement_reconciliation.json"
    md_path = output_dir / "paper_settlement_reconciliation.md"
    rows_path = output_dir / "paper_settlement_rows.json"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    rows_path.write_text(json.dumps(payload["rows"], indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(_render_markdown(payload), encoding="utf-8")
    return PaperSettlementArtifactSet(output_dir, json_path, md_path, rows_path)


def _order_reconciliation_row(session: Session, order: PaperOrder) -> dict[str, Any]:
    settlement = session.get(Settlement, order.ticker)
    market = session.get(Market, order.ticker)
    latest_pnl = _latest_pnl(session, order.ticker)
    close_bucket, hours_to_close = _close_time_bucket(market)
    is_local_derived_composite = _is_local_derived_composite_ticker(order.ticker)
    possible_matches = (
        []
        if settlement is not None or is_local_derived_composite
        else _possible_settlement_matches(session, order, market)
    )
    outcome = _settlement_outcome(settlement) if settlement is not None else None
    reason = _classify_order(
        order=order,
        market=market,
        settlement=settlement,
        latest_pnl=latest_pnl,
        outcome=outcome,
        possible_matches=possible_matches,
    )
    eligible = reason == "ELIGIBLE_TO_SETTLE_NOW"
    return {
        "paper_order_id": order.id,
        "ticker": order.ticker,
        "model_name": order.model_name,
        "side": order.side,
        "status": order.status,
        "forecast_id": order.forecast_id,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "quantity": order.quantity,
        "market_price": order.market_price,
        "limit_price": order.limit_price,
        "settlement_found": settlement is not None,
        "settlement_result": settlement.result if settlement is not None else None,
        "yes_settlement_value": settlement.yes_settlement_value if settlement is not None else None,
        "settled_at": settlement.settled_at.isoformat()
        if settlement is not None and settlement.settled_at
        else None,
        "settlement_outcome": str(outcome) if outcome is not None else None,
        "market_found": market is not None,
        "market_status": market.status if market is not None else None,
        "market_result": market.result if market is not None else None,
        "market_close_time": market.close_time.isoformat()
        if market is not None and market.close_time
        else None,
        "close_time_bucket": close_bucket,
        "hours_to_close": hours_to_close,
        "market_settlement_ts": market.settlement_ts.isoformat()
        if market is not None and market.settlement_ts
        else None,
        "ticker_shape": _ticker_shape(order.ticker, market),
        "is_local_derived_composite": is_local_derived_composite,
        "latest_pnl_settlement_result": latest_pnl.settlement_result if latest_pnl else None,
        "latest_pnl_notes": latest_pnl.notes if latest_pnl else None,
        "possible_settlement_matches": possible_matches,
        "settlement_resolution_policy": _settlement_resolution_policy(possible_matches),
        "eligible_to_settle_now": eligible,
        "reason": reason,
        "explanation": _reason_explanation(reason, possible_matches),
    }


def _classify_order(
    *,
    order: PaperOrder,
    market: Market | None,
    settlement: Settlement | None,
    latest_pnl: PaperPnl | None,
    outcome: Decimal | None,
    possible_matches: list[dict[str, Any]],
) -> str:
    if order.status == ORDER_OPEN:
        return "ORDER_STILL_OPEN"
    if order.status != ORDER_FILLED:
        return "ORDER_NOT_FILLED"
    if not _ticker_well_formed(order.ticker):
        return "MALFORMED_TICKER"
    if settlement is None:
        if _is_local_derived_composite_ticker(order.ticker):
            return "LOCAL_DERIVED_COMPOSITE_NO_EXACT_SETTLEMENT"
        if possible_matches:
            if any(
                match.get("leg_identity_status") == "VALIDATED_SAME_LEG"
                for match in possible_matches
            ):
                return "VALIDATED_SIBLING_REQUIRES_MANUAL_REVIEW"
            if any(
                match.get("leg_identity_status") == "DIFFERENT_CONTRACT_LEG"
                for match in possible_matches
            ):
                return "SIBLING_DIFFERENT_CONTRACT_LEG"
            return "POSSIBLE_TICKER_MISMATCH"
        if market is not None and _is_market_open(market):
            return "MARKET_STILL_OPEN"
        return "NO_SETTLEMENT_YET"
    if outcome is None:
        return "SETTLEMENT_RESULT_UNUSABLE"
    if order.side.upper() not in SUPPORTED_SETTLEMENT_SIDES:
        return "SIDE_MAPPING_UNSUPPORTED"
    if _decimal_or_none(order.market_price) is None and _decimal_or_none(order.limit_price) is None:
        return "MISSING_ENTRY_PRICE"
    if _latest_pnl_realized_settlement(latest_pnl, settlement):
        return "ALREADY_REALIZED"
    return "ELIGIBLE_TO_SETTLE_NOW"


def _settlement_outcome(settlement: Settlement) -> Decimal | None:
    value = _decimal_or_none(settlement.yes_settlement_value)
    if value is not None and Decimal("0") <= value <= Decimal("1"):
        return value
    if settlement.result is None:
        return None
    normalized = settlement.result.strip().lower()
    if normalized in {"yes", "y", "1", "true"}:
        return Decimal("1")
    if normalized in {"no", "n", "0", "false"}:
        return Decimal("0")
    return None


def _possible_settlement_matches(
    session: Session,
    order: PaperOrder,
    market: Market | None,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    candidates: dict[str, Settlement] = {}
    stem = _ticker_stem(order.ticker)
    if stem:
        for settlement in session.scalars(
            select(Settlement)
            .where(Settlement.ticker.like(f"{stem}%"), Settlement.ticker != order.ticker)
            .limit(limit)
        ):
            candidates[settlement.ticker] = settlement
    if market is not None and market.event_ticker:
        for settlement in session.scalars(
            select(Settlement)
            .join(Market, Market.ticker == Settlement.ticker)
            .where(Market.event_ticker == market.event_ticker, Settlement.ticker != order.ticker)
            .limit(limit)
        ):
            candidates[settlement.ticker] = settlement
    rows: list[dict[str, Any]] = []
    paper_signature = _leg_signature(session, order.ticker)
    for settlement in list(candidates.values())[:limit]:
        sibling_market = session.get(Market, settlement.ticker)
        sibling_signature = _leg_signature(session, settlement.ticker)
        leg_identity_status = _leg_identity_status(paper_signature, sibling_signature)
        same_event = bool(
            market is not None
            and sibling_market is not None
            and market.event_ticker
            and market.event_ticker == sibling_market.event_ticker
        )
        same_series = bool(
            market is not None
            and sibling_market is not None
            and market.series_ticker
            and market.series_ticker == sibling_market.series_ticker
        )
        same_stem = _ticker_stem(order.ticker) == _ticker_stem(settlement.ticker)
        rows.append(
            {
                "ticker": settlement.ticker,
                "result": settlement.result,
                "settled_at": settlement.settled_at.isoformat()
                if settlement.settled_at
                else None,
                "same_event": same_event,
                "same_series": same_series,
                "same_stem": same_stem,
                "event_ticker": sibling_market.event_ticker if sibling_market else None,
                "market_status": sibling_market.status if sibling_market else None,
                "leg_identity_status": leg_identity_status,
                "paper_leg_count": len(paper_signature),
                "sibling_leg_count": len(sibling_signature),
                "paper_leg_signature": paper_signature,
                "sibling_leg_signature": sibling_signature,
                "resolution_policy": (
                    "MANUAL_REVIEW_REQUIRED_BEFORE_RESOLUTION"
                    if leg_identity_status == "VALIDATED_SAME_LEG"
                    else "DO_NOT_AUTO_RESOLVE_DIFFERENT_OR_UNKNOWN_LEG"
                ),
            }
        )
    return rows


def _leg_signature(session: Session, ticker: str) -> list[dict[str, str | None]]:
    legs = list(
        session.scalars(
            select(MarketLeg)
            .where(MarketLeg.ticker == ticker)
            .order_by(MarketLeg.leg_index)
        )
    )
    return [
        {
            "side": _normalize_signature_part(leg.side),
            "category": _normalize_signature_part(leg.category),
            "market_type": _normalize_signature_part(leg.market_type),
            "entity_name": _normalize_signature_part(leg.entity_name),
            "operator": _normalize_signature_part(leg.operator),
            "threshold_value": _normalize_signature_part(leg.threshold_value),
            "unit": _normalize_signature_part(leg.unit),
        }
        for leg in legs
    ]


def _leg_identity_status(
    paper_signature: list[dict[str, str | None]],
    sibling_signature: list[dict[str, str | None]],
) -> str:
    if not paper_signature or not sibling_signature:
        return "UNKNOWN_LEG_IDENTITY"
    return (
        "VALIDATED_SAME_LEG"
        if paper_signature == sibling_signature
        else "DIFFERENT_CONTRACT_LEG"
    )


def _normalize_signature_part(value: object) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"\s+", " ", str(value)).strip().lower()
    return normalized or None


def _latest_pnl(session: Session, ticker: str) -> PaperPnl | None:
    return session.scalar(
        select(PaperPnl)
        .where(PaperPnl.ticker == ticker)
        .order_by(desc(PaperPnl.calculated_at), desc(PaperPnl.id))
        .limit(1)
    )


def _latest_pnl_realized_settlement(
    latest_pnl: PaperPnl | None,
    settlement: Settlement | None,
) -> bool:
    if latest_pnl is None or settlement is None:
        return False
    if (latest_pnl.notes or "").strip().lower() != "settled market realized paper p&l":
        return False
    return _normalize_result(latest_pnl.settlement_result) == _normalized_settlement_result(
        settlement
    )


def _normalized_settlement_result(settlement: Settlement) -> str | None:
    normalized = _normalize_result(settlement.result)
    if normalized in {"yes", "no"}:
        return normalized
    outcome = _settlement_outcome(settlement)
    if outcome == Decimal("1"):
        return "yes"
    if outcome == Decimal("0"):
        return "no"
    if outcome is not None:
        return normalized or "scalar"
    return normalized


def _normalize_result(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"yes", "y", "1", "true"}:
        return "yes"
    if normalized in {"no", "n", "0", "false"}:
        return "no"
    return normalized or None


def _ticker_shape(ticker: str, market: Market | None) -> str:
    if not _ticker_well_formed(ticker):
        return "malformed"
    series = (market.series_ticker if market is not None else None) or ticker.split("-", 1)[0]
    if series.startswith("KXMVECROSSCATEGORY"):
        return "cross_category"
    if "SPORTS" in series:
        return "sports"
    return "standard"


def _is_local_derived_composite_ticker(ticker: str) -> bool:
    return ticker.startswith(LOCAL_DERIVED_TICKER_PREFIXES)


def _ticker_well_formed(ticker: str) -> bool:
    return bool(re.fullmatch(r"KX[A-Z0-9-]+", ticker or ""))


def _ticker_stem(ticker: str) -> str | None:
    if "-" not in ticker:
        return None
    return ticker.rsplit("-", 1)[0]


def _is_market_open(market: Market) -> bool:
    status = (market.status or "").strip().lower()
    return status in OPEN_MARKET_STATUSES


def _close_time_bucket(market: Market | None) -> tuple[str, float | None]:
    if market is None or market.close_time is None:
        return "unknown", None
    close_time = parse_datetime(market.close_time)
    if close_time is None:
        return "unknown", None
    seconds = (close_time - utc_now()).total_seconds()
    hours = round(seconds / 3600, 2)
    if seconds < 0:
        return "overdue", hours
    if hours <= 6:
        return "0-6h", hours
    if hours <= 24:
        return "6-24h", hours
    if hours <= 48:
        return "1-2d", hours
    if hours <= 72:
        return "2-3d", hours
    if hours <= 168:
        return "3-7d", hours
    return "7d+", hours


def _close_time_buckets(rows: list[dict[str, Any]]) -> dict[str, int]:
    buckets = {
        key: 0
        for key in ("overdue", "0-6h", "6-24h", "1-2d", "2-3d", "3-7d", "7d+", "unknown")
    }
    for row in rows:
        if row["status"] != ORDER_FILLED or row["settlement_found"]:
            continue
        bucket = str(row.get("close_time_bucket") or "unknown")
        buckets[bucket] = buckets.get(bucket, 0) + 1
    return {key: count for key, count in buckets.items() if count}


def _sibling_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [
        match
        for row in rows
        for match in row.get("possible_settlement_matches", [])
    ]
    by_identity: dict[str, int] = {}
    for match in matches:
        status = str(match.get("leg_identity_status") or "UNKNOWN_LEG_IDENTITY")
        by_identity[status] = by_identity.get(status, 0) + 1
    return {
        "possible_sibling_settlements": len(matches),
        "identity_counts": dict(sorted(by_identity.items())),
        "not_auto_resolved": len(matches),
        "resolution_rule": (
            "Paper P&L stays unresolved unless there is an exact ticker settlement "
            "or a validated same-leg identity is explicitly reviewed."
        ),
    }


def _settlement_resolution_policy(possible_matches: list[dict[str, Any]]) -> str:
    if not possible_matches:
        return "EXACT_TICKER_ONLY"
    if any(match.get("leg_identity_status") == "VALIDATED_SAME_LEG" for match in possible_matches):
        return "EXACT_TICKER_OR_VALIDATED_SAME_LEG_AFTER_REVIEW"
    return "EXACT_TICKER_ONLY_DO_NOT_RESOLVE_SIBLING"


def _learning_slow_settlement_guidance(
    rows: list[dict[str, Any]],
    close_buckets: dict[str, int],
) -> dict[str, Any]:
    active_unsettled = [
        row for row in rows if row["status"] == ORDER_FILLED and not row["settlement_found"]
    ]
    slow_buckets = {"3-7d", "7d+", "unknown"}
    slow_count = sum(close_buckets.get(bucket, 0) for bucket in slow_buckets)
    total = len(active_unsettled)
    share = None if total == 0 else round((slow_count / total) * 100, 1)
    max_days = 1 if slow_count else 3
    return {
        "active_unsettled_trades": total,
        "slow_or_unknown_settlement_trades": slow_count,
        "slow_or_unknown_share_percent": share,
        "recommended_learning_max_days_to_settlement": max_days,
        "recommended_env": {
            "LEARNING_PRIORITIZE_FAST_SETTLEMENT": "true",
            "LEARNING_MAX_DAYS_TO_SETTLEMENT": str(max_days),
            "LEARNING_CANDIDATE_SCAN_LIMIT": "500",
            "EXECUTION_ENABLED": "false",
        },
        "recommendation": (
            "Prefer markets closing inside 24 hours until settled-paper evidence starts "
            "arriving; deprioritize slow or unknown close-time markets."
        ),
    }


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _reason_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        reason = str(row["reason"])
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _top_reason(counts: dict[str, int]) -> dict[str, Any]:
    if not counts:
        return {"reason": "NO_PAPER_TRADES", "count": 0}
    reason, count = next(iter(counts.items()))
    return {"reason": reason, "count": count, "explanation": _reason_explanation(reason, [])}


def _reason_explanation(reason: str, possible_matches: list[dict[str, Any]]) -> str:
    explanations = {
        "ELIGIBLE_TO_SETTLE_NOW": (
            "Exact settlement and side mapping exist; paper P&L can resolve it."
        ),
        "ALREADY_REALIZED": (
            "Exact settlement already matches the latest settled paper P&L row."
        ),
        "ORDER_STILL_OPEN": "Paper order has not filled yet.",
        "ORDER_NOT_FILLED": "Paper order is not in FILLED status.",
        "MALFORMED_TICKER": "Paper order ticker does not match the expected Kalshi ticker format.",
        "POSSIBLE_TICKER_MISMATCH": (
            "No exact settlement row exists, but nearby settlement tickers were found."
        ),
        "SIBLING_DIFFERENT_CONTRACT_LEG": (
            "A settled sibling exists in the same event/stem area, but parsed contract "
            "legs differ. Do not resolve this paper trade from the sibling."
        ),
        "VALIDATED_SIBLING_REQUIRES_MANUAL_REVIEW": (
            "A settled sibling has the same parsed contract-leg signature, but the ticker "
            "does not match. Leave unresolved until explicitly reviewed."
        ),
        "MARKET_STILL_OPEN": "The market is still open or paused in local market metadata.",
        "NO_SETTLEMENT_YET": (
            "No exact settlement row exists for this ticker in the local settlement table."
        ),
        "LOCAL_DERIVED_COMPOSITE_NO_EXACT_SETTLEMENT": (
            "This paper ticker is a local derived composite, not a direct Kalshi exchange "
            "market. Paper P&L stays blocked until an exact local settlement row exists "
            "for the same composite ticker."
        ),
        "SETTLEMENT_RESULT_UNUSABLE": (
            "Settlement row exists, but result/value cannot be mapped to YES or NO."
        ),
        "SIDE_MAPPING_UNSUPPORTED": (
            "Paper order side is not supported by the paper settlement P&L mapper."
        ),
        "MISSING_ENTRY_PRICE": "Paper order has no usable market or limit price.",
    }
    text = explanations.get(reason, "No specialized explanation is available.")
    if reason == "POSSIBLE_TICKER_MISMATCH" and possible_matches:
        text += f" Example possible match: {possible_matches[0]['ticker']}."
    return text


def _recommended_next_action(counts: dict[str, int], eligible_rows: list[dict[str, Any]]) -> str:
    if eligible_rows:
        return "Run kalshi-bot paper-pnl, then rerun paper-settlement-doctor."
    if counts.get("LOCAL_DERIVED_COMPOSITE_NO_EXACT_SETTLEMENT", 0):
        return (
            "Continue exact local settlement evidence watch for derived composite tickers. "
            "Do not resolve paper P&L from component, sibling, or different contract-leg tickers."
        )
    if counts.get("NO_SETTLEMENT_YET", 0) or counts.get("MARKET_STILL_OPEN", 0):
        return (
            "Continue exact-ticker settlement watch/harvest. Do not resolve paper P&L "
            "from sibling or different contract-leg tickers."
        )
    if counts.get("SIBLING_DIFFERENT_CONTRACT_LEG", 0) or counts.get(
        "VALIDATED_SIBLING_REQUIRES_MANUAL_REVIEW",
        0,
    ):
        return (
            "Review sibling/contract-leg diagnostics. Do not resolve paper P&L from "
            "siblings unless exact ticker or manually validated same-leg identity matches."
        )
    if counts.get("POSSIBLE_TICKER_MISMATCH", 0):
        return "Investigate ticker normalization between paper_orders and settlements."
    if counts.get("SETTLEMENT_RESULT_UNUSABLE", 0):
        return "Inspect settlement result/value parsing before counting resolved paper trades."
    return "No immediate settlement repair action is available from current local data."


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3Y-SR Paper Settlement Reconciliation Doctor",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Safety: {payload['paper_only_safety']}",
        "- Demo/live execution: blocked; this report is read-only diagnostics.",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    top = payload["top_reason"]
    lines.extend(
        [
            "",
            "## Top Bottleneck",
            "",
            f"- Reason: {top['reason']}",
            f"- Count: {top['count']}",
            f"- Explanation: {top.get('explanation', '')}",
            "",
            "## Reason Counts",
            "",
            "| Reason | Count |",
            "| --- | ---: |",
        ]
    )
    for reason, count in payload["reason_counts"].items():
        lines.append(f"| {reason} | {count} |")
    lines.extend(
        [
            "",
            "## Sibling / Contract Leg Reconciliation",
            "",
            payload["sibling_summary"]["resolution_rule"],
            "",
            (
                "| Order | Paper ticker | Sibling ticker | Same event | Same stem | "
                "Leg identity | Policy |"
            ),
            "| ---: | --- | --- | --- | --- | --- | --- |",
        ]
    )
    sibling_rows = [
        (row, match)
        for row in payload["rows"]
        for match in row.get("possible_settlement_matches", [])
    ]
    if sibling_rows:
        for row, match in sibling_rows[:20]:
            lines.append(
                f"| {row['paper_order_id']} | {_md(row['ticker'])} | "
                f"{_md(match['ticker'])} | {match.get('same_event')} | "
                f"{match.get('same_stem')} | {match.get('leg_identity_status')} | "
                f"{_md(match.get('resolution_policy'))} |"
            )
    else:
        lines.append("|  | None |  |  |  |  | No sibling settlement candidates found. |")
    lines.extend(
        [
            "",
            "## Active Unsettled Close-Time Buckets",
            "",
            "| Close bucket | Filled paper trades without exact settlement |",
            "| --- | ---: |",
        ]
    )
    if payload["close_time_buckets"]:
        for bucket, count in payload["close_time_buckets"].items():
            lines.append(f"| {bucket} | {count} |")
    else:
        lines.append("| None | 0 |")
    guidance = payload["learning_slow_settlement_guidance"]
    lines.extend(
        [
            "",
            "## Learning Slow-Settlement Guidance",
            "",
            f"- Active unsettled trades: {guidance['active_unsettled_trades']}",
            f"- Slow or unknown settlement trades: {guidance['slow_or_unknown_settlement_trades']}",
            (
                "- Recommended LEARNING_MAX_DAYS_TO_SETTLEMENT: "
                f"{guidance['recommended_learning_max_days_to_settlement']}"
            ),
            f"- Recommendation: {guidance['recommendation']}",
            "",
            "```bash",
        ]
    )
    for key, value in guidance["recommended_env"].items():
        lines.append(f"export {key}={value}")
    lines.append("```")
    lines.extend(
        [
            "",
            "## Eligible To Settle Now",
            "",
            "| Order | Ticker | Side | Result | Entry | Explanation |",
            "| ---: | --- | --- | --- | ---: | --- |",
        ]
    )
    if payload["eligible_trades"]:
        for row in payload["eligible_trades"][:20]:
            entry = row["market_price"] or row["limit_price"]
            lines.append(
                f"| {row['paper_order_id']} | {row['ticker']} | {row['side']} | "
                f"{row['settlement_result']} | {entry} | {row['explanation']} |"
            )
    else:
        lines.append("|  | None |  |  |  | No currently eligible paper trades found. |")
    lines.extend(
        [
            "",
            "## Sample Trade Diagnostics",
            "",
            "| Order | Ticker | Status | Settlement? | Market status | Reason | Explanation |",
            "| ---: | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["rows"][:50]:
        lines.append(
            f"| {row['paper_order_id']} | {row['ticker']} | {row['status']} | "
            f"{'yes' if row['settlement_found'] else 'no'} | "
            f"{row['market_status'] or 'unknown'} | {row['reason']} | "
            f"{row['explanation']} |"
        )
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
            "",
        ]
    )
    return "\n".join(lines)


def _md(value: object) -> str:
    return str(value or "").replace("|", "\\|")
