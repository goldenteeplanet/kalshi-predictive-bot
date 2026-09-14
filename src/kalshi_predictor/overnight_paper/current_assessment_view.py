"""Captured assessment arithmetic, never original replay or candidate admission."""

from __future__ import annotations

from datetime import datetime
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from html import escape
from typing import Any

from .store import aware


def empty_assessment_batch() -> dict[str, Any]:
    return dict(
        status="UNAVAILABLE",
        assessed_at=None,
        age_seconds=None,
        freshness="UNVERIFIED",
        scan_sha256=None,
        rows=[],
        recorded_row_count=None,
        unique_row_count=None,
        batch_completeness="UNKNOWN",
        scope="JOURNAL_AND_ARITHMETIC_ONLY",
        original_replay=False,
        paper_eligible=False,
        full_net_ev=None,
    )


def _number(value: Any, low: str, high: str) -> Decimal | None:
    if value is None:
        return None
    if type(value) not in (str, int) or len(str(value)) > 64:
        raise ValueError("BOUNDED_RECORDED_DECIMAL_REQUIRED")
    number = Decimal(value)
    exponent = number.as_tuple().exponent
    if (
        not number.is_finite()
        or not isinstance(exponent, int)
        or not -400 <= exponent <= 0
        or len(number.as_tuple().digits) > 28
        or not Decimal(low) <= number <= Decimal(high)
    ):
        raise ValueError("BOUNDED_RECORDED_DECIMAL_REQUIRED")
    return number


def _equal(recorded: Decimal | None, expected: Decimal) -> Decimal | None:
    if recorded is not None and recorded != expected:
        raise ValueError("RECORDED_ASSESSMENT_ARITHMETIC_MISMATCH")
    return recorded


def _row(record: dict[str, Any]) -> dict[str, Any]:
    ticker = record["ticker"]
    if len(ticker) > 240:
        raise ValueError("BOUNDED_RECORDED_TICKER_REQUIRED")
    p = _number(record.get("forecast_probability"), "0", "1")
    price = _number(record.get("executable_price"), "0", "1")
    gross = _number(record.get("gross_edge"), "-1", "1")
    after_fee = _number(record.get("after_fee"), "-2", "1")
    after_execution = _number(record.get("after_execution"), "-3", "1")
    fee, impact = record.get("fee"), record.get("snapshot_impact")
    fee_value = _number(fee.get("value"), "0", "1") if isinstance(fee, dict) else None
    impact_value = _number(impact.get("value"), "0", "1") if isinstance(impact, dict) else None
    # These are recorded support claims, not revalidated original evidence.
    fee_supported = isinstance(fee, dict) and all(
        (
            fee.get("status") == "ESTIMATED_WITH_SUPPORT",
            fee.get("method") == "PUBLIC_GENERAL_TAKER_CENT_PAPER_V1",
            fee.get("scope") == "LOCAL_PAPER_MODEL_NOT_ACCOUNT_INVOICE",
            fee.get("unit") == "USD_PER_ONE_DOLLAR_PAYOUT",
            fee.get("paper_support") is True,
            fee.get("exact_account_fee_certified") is False,
            fee.get("execution_authority") is False,
        )
    )
    impact_supported = isinstance(impact, dict) and all(
        (
            impact.get("status") == "CERTIFIED",
            impact.get("method") == "ONE_CONTRACT_SNAPSHOT_VWAP",
            impact.get("scope") == "CONDITIONAL_SIMULATED_FILL_AT_CAPTURED_BOOK",
            impact.get("unit") == "USD_PER_ONE_DOLLAR_PAYOUT",
            impact.get("fill_status") == "BOOK_FILL_PRICE_KNOWN",
            impact.get("paper_support") is True,
            impact.get("execution_authority") is False,
        )
    )
    # Reproduce default captured scanner arithmetic without inheriting caller traps/rounding.
    with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999,
                              capitals=1, clamp=0, flags=[],
                              traps=[InvalidOperation, DivisionByZero, Overflow])):
        forecast = record.get("forecast")
        if forecast is not None:
            if not isinstance(forecast, dict):
                raise ValueError("RECORDED_FORECAST_OBJECT_REQUIRED")
            yes = _number(forecast.get("probability_yes"), "0", "1")
            if yes is None:
                p = None
            else:
                p = _equal(p, yes if record["side"] == "YES" else 1-yes)
        if p is None or price is None:
            gross = None
        else:
            gross = _equal(gross, p - price)
        if gross is None or fee_value is None:
            after_fee = None
        else:
            after_fee = _equal(after_fee, gross - fee_value)
        if after_fee is None or impact_value is None:
            after_execution = None
        else:
            after_execution = _equal(after_execution, after_fee - impact_value)
    if not fee_supported:
        after_fee = after_execution = None
    if not impact_supported:
        after_execution = None
    return dict(
        ticker=ticker,
        side=record["side"],
        probability=str(p) if p is not None else None,
        executable_price=str(price) if price is not None else None,
        gross_edge=str(gross) if gross is not None else None,
        recorded_fee=str(fee_value) if fee_supported and fee_value is not None else None,
        recorded_snapshot_impact=(
            str(impact_value) if impact_supported and impact_value is not None else None
        ),
        after_recorded_costs=str(after_execution) if after_execution is not None else None,
        cost_support="RECORDED_CLAIMS_ONLY" if after_execution is not None else "UNKNOWN",
        full_net_ev=None,
        paper_eligible=False,
        original_replay=False,
    )


def latest_assessment_batch(records: list[dict[str, Any]], *, now: datetime) -> dict[str, Any]:
    """Consume an already strictly read journal, select chronology before economics."""
    result = empty_assessment_batch()
    try:
        candidates = [
            (aware(e["record"]["assessed_at"]), e)
            for e in records
            if e["record_kind"] == "ASSESSMENT"
        ]
        if not candidates:
            return result
        latest = max(clock for clock, _ in candidates)
        batch = [e for clock, e in candidates if clock == latest]
        hashes = {e["record"]["scan_sha256"] for e in batch}
        if len(hashes) != 1 or len(batch) > 600:
            raise ValueError("AMBIGUOUS_OR_OVERSIZED_LATEST_ASSESSMENT_BATCH")
        if latest > now or any(latest > aware(e["recorded_at"]) for e in batch):
            raise ValueError("ASSESSMENT_CAPTURE_CLOCK_INVALID")
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for envelope in batch:
            record = envelope["record"]
            key = (record["ticker"], record["side"])
            if key in unique and unique[key] != record:
                raise ValueError("CONFLICTING_ASSESSMENT_IDENTITY")
            unique[key] = record
        rows = [_row(unique[key]) for key in sorted(unique)]
        age = (now - latest).total_seconds()
        result.update(
            status="CAPTURED_ARITHMETIC_CHECKED",
            assessed_at=latest.isoformat(),
            age_seconds=age,
            freshness="RECENT_CAPTURE_NOT_LIVE" if age <= 300 else "STALE",
            scan_sha256=next(iter(hashes)),
            rows=rows,
            recorded_row_count=len(batch),
            unique_row_count=len(rows),
        )
    except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
        result.update(status="INVALID_OR_AMBIGUOUS", blocker=str(exc))
    return result


def render_assessment_batch(batch: dict[str, Any]) -> str:
    def text(value: Any) -> str:
        return escape("Unknown" if value is None else str(value))

    fields = (
        ("ticker", "Contract"),
        ("side", "Side"),
        ("probability", "Captured probability"),
        ("executable_price", "Captured ask"),
        ("gross_edge", "Captured gross edge"),
        ("recorded_fee", "Recorded fee"),
        ("recorded_snapshot_impact", "Recorded snapshot impact"),
        ("after_recorded_costs", "Edge after recorded costs"),
        ("full_net_ev", "Full net EV"),
    )
    rows = "".join(
        "<tr>" + "".join(f"<td>{text(row.get(key))}</td>" for key, _ in fields) + "</tr>"
        for row in batch.get("rows", [])[:20]
    )
    return (
        "<h3>Latest recorded assessment batch</h3>"
        "<p>Captured research values in dollars per $1 payout. Journal and arithmetic checks "
        "only; provider originals have not been replayed by this view. Batch completeness is "
        "unknown. Recorded cost support is not independently verified here. "
        "These are not live quotes or paper-ready candidates.</p>"
        f"<p>Captured at: {text(batch.get('assessed_at'))}; "
        f"age in seconds: {text(batch.get('age_seconds'))}; "
        f"freshness: {text(batch.get('freshness'))}; status: {text(batch.get('status'))}.</p>"
        "<p>Full net EV: Unknown. Paper eligibility: No. "
        "Stale captures remain historical evidence only.</p>"
        f"<p>Showing {min(20, len(batch.get('rows', [])))} of "
        f"{text(batch.get('unique_row_count'))} unique rows; "
        f"{text(batch.get('recorded_row_count'))} recorded rows. "
        "Ordered by contract and side, not edge.</p>"
        "<div style='overflow:auto'><table><thead><tr>"
        + "".join(f"<th>{label}</th>" for _, label in fields)
        + "</tr></thead><tbody>"
        + rows
        + "</tbody></table></div>"
    )
