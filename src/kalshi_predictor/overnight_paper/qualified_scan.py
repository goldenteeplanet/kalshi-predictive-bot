"""Bounded diagnostic repricing of prepared, pinned-policy candidates only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from kalshi_predictor.opportunities.scanner import top5_orderbook_notional
from kalshi_predictor.opportunities.scoring import score_liquidity

from . import rule_verifier
from .books import qualify_book
from .discovery import PublicArchive
from .evaluation_dataset import evaluate_dataset, read_records
from .gate_context import QualificationContext, verify_context_gate
from .provenance import Artifact
from .source_health import aware
from .timing import verify_settlement_horizon

# Availability only; provenance and chronological model evaluation are separate.
SUPPORTED_MODELS = {
    "KXTEMPNYCH": "weather_v2",
    "KXTEMPCHIH": "weather_v2",
    "KXTEMPLAXH": "weather_v2",
    "KXBTC15M": "crypto_v2",
    "KXBTC": "crypto_v2",
}
OUTPUT_FIELDS = (
    "ticker",
    "event",
    "category",
    "source_status",
    "rule_status",
    "book_status",
    "forecast_status",
    "model",
    "probability",
    "executable_price",
    "fees",
    "slippage",
    "uncertainty",
    "net_ev",
    "size",
    "risk",
    "first_blocker",
)


@dataclass(frozen=True)
class PreparedScanCandidate:
    decision: dict[str, Any]
    context: QualificationContext
    evaluation_records: tuple[Artifact, ...] = ()


def shortlist_candidates(
    prepared: tuple[PreparedScanCandidate, ...],
    *,
    now: datetime,
    max_candidates: int = 10,
    registry: tuple[rule_verifier.CertifiedRulePolicy, ...] | None = None,
) -> dict[str, Any]:
    """Pure review. A supplied boolean or passing report cannot prepare a model."""
    if not 1 <= max_candidates <= 20:
        raise ValueError("SCAN_CANDIDATE_BUDGET_1_TO_20")
    policies = rule_verifier.CERTIFIED_RULE_POLICIES if registry is None else registry
    rows: list[dict[str, Any]] = []
    selected: list[tuple[PreparedScanCandidate, dict[str, Any]]] = []
    used_events: set[str] = set()
    by_ticker: dict[str, list[PreparedScanCandidate]] = {}
    for item in prepared:
        by_ticker.setdefault(str(item.decision.get("ticker")), []).append(item)
    # Interleave model families while preserving earliest observations in each;
    # one busy family cannot consume the entire bounded book budget.
    groups: dict[str, list[rule_verifier.CertifiedRulePolicy]] = {}
    for policy in sorted(policies, key=lambda p: (p.observation_time, p.series, p.ticker)):
        groups.setdefault(SUPPORTED_MODELS.get(policy.series, policy.series), []).append(policy)
    ordered = [
        group[index]
        for index in range(max((len(g) for g in groups.values()), default=0))
        for group in groups.values()
        if index < len(group)
    ]
    for policy in ordered:
        row = dict.fromkeys(OUTPUT_FIELDS)
        row.update(
            ticker=policy.ticker,
            event=policy.event_id,
            rule_status="UNVERIFIED",
            book_status="NOT_FETCHED",
            forecast_status="NOT_PREPARED",
        )
        rows.append(row)
        choices = by_ticker.get(policy.ticker, [])
        if len(choices) != 1:
            row["first_blocker"] = (
                "FORECAST_NOT_PREPARED" if not choices else "AMBIGUOUS_PREPARATION"
            )
            continue
        item = choices[0]
        decision = item.decision
        row.update(category=decision.get("category"), model=decision.get("model_name"))
        rule = rule_verifier.verify_settlement_rule(
            decision=decision,
            documents=item.context.rule_documents,
            registry=policies,
        )
        timing = verify_settlement_horizon(decision=decision, rule=rule, now=now)
        if not rule.passed or not timing.passed:
            row["first_blocker"] = (rule.blockers or timing.blockers)[0]
            continue
        row["rule_status"] = "CERTIFIED"
        if policy.series not in SUPPORTED_MODELS or SUPPORTED_MODELS[policy.series] != decision.get(
            "model_name"
        ):
            row["first_blocker"] = "MODEL_NOT_READY"
            continue
        if item.context.provenance is None:
            row["first_blocker"] = "FORECAST_NOT_PREPARED"
            continue
        if not verify_context_gate(9, inputs=decision, context=item.context, now=now):
            row.update(source_status="UNVERIFIED", first_blocker="FORECAST_PROVENANCE_INVALID")
            continue
        row.update(source_status="FRESH_ORIGINALS_VERIFIED", forecast_status="PROVENANCE_VERIFIED")
        evaluation = evaluate_dataset(item.evaluation_records, as_of=now)
        if not evaluation.ready:
            row["first_blocker"] = "MODEL_NOT_READY"
            continue
        records = read_records(item.evaluation_records)
        evaluated_models = {
            (x["record"].get("model_name"), x["record"].get("model_version"))
            for x in records
            if x["record"].get("kind") == "policy-v1"
        }
        if evaluated_models != {(decision.get("model_name"), decision.get("model_version"))}:
            row["first_blocker"] = "MODEL_EVALUATION_IDENTITY_MISMATCH"
            continue
        if policy.event_id in used_events:
            row["first_blocker"] = "RELATED_EVENT_ALREADY_SELECTED"
            continue
        if len(selected) >= max_candidates:
            row["first_blocker"] = "SCAN_BUDGET_DEFERRED"
            continue
        used_events.add(policy.event_id)
        row["forecast_status"] = "MODEL_EVALUATION_VERIFIED"
        selected.append((item, row))
    return {
        "status": "NO_CERTIFIED_FAMILY" if not policies else "PREPARATION_REVIEWED",
        "coverage": "PINNED_POLICY_TICKERS_ONLY",
        "rows": rows,
        "selected": selected,
    }


def run_qualified_scan(
    archive_root: Path,
    *,
    prepared: tuple[PreparedScanCandidate, ...] = (),
    now: datetime | None = None,
    max_candidates: int = 10,
    timeout_seconds: int = 120,
    max_spread: Decimal = Decimal("0.10"),
) -> dict[str, Any]:
    """PublicArchive owns GET budgets and rate limits. No ledger or order calls.

    Refreshed quotes invalidate prior decisions: downstream coordinator must
    construct a new decision ID and recalculate EV, sizing and risk.
    """
    if not 1 <= timeout_seconds <= 600:
        raise ValueError("SCAN_TIMEOUT_1_TO_600")
    reference = aware(now) if now is not None else datetime.now(UTC)
    result = shortlist_candidates(prepared, now=reference, max_candidates=max_candidates)
    selected = result.pop("selected")
    result.update(
        generated_at=reference.isoformat(),
        orders_created=0,
        database_writes=0,
        network_requests=0,
        books_passed=0,
        qualified_candidates=0,
    )
    if not selected:
        return result
    public = PublicArchive(archive_root, max_requests=3 * max_candidates, seconds=timeout_seconds)
    for item, row in selected:
        try:
            ticker = row["ticker"]
            market = public.get("/markets/" + ticker)["market"]
            event = public.get("/events/" + row["event"])["event"]
            if (
                market.get("ticker") != ticker
                or market.get("event_ticker") != row["event"]
                or event.get("event_ticker") != row["event"]
                or event.get("series_ticker") != item.decision["series"]
                or market.get("status") not in {"open", "active"}
                or aware(market.get("close_time")) != aware(item.decision["market_close_time"])
                or aware(market.get("close_time")) <= datetime.now(UTC)
            ):
                raise ValueError("CURRENT_MARKET_IDENTITY_OR_CLOCK_MISMATCH")
            raw = public.get("/markets/" + ticker + "/orderbook", {"depth": 5})
            depth = top5_orderbook_notional(ticker=ticker, raw_orderbook_json=json.dumps(raw))
            liquidity = score_liquidity(
                volume=market.get("volume_fp"),
                open_interest=market.get("open_interest_fp"),
                liquidity=max(Decimal(str(market.get("liquidity_dollars") or 0)), depth),
            )
            book = qualify_book(
                raw,
                received_at=aware(public.receipts[-1]["received_at"]),
                now=datetime.now(UTC),
                max_spread=max_spread,
                liquidity_score=liquidity,
                price_ranges=market.get("price_ranges"),
            )
            row.update(
                book_status="EXECUTABLE" if book["executable"] else "REJECTED",
                book=book,
                first_blocker="NEW_DECISION_REQUIRED" if book["executable"] else "BOOK_REJECTED",
            )
            result["books_passed"] += int(book["executable"])
        except (ValueError, RuntimeError, KeyError, TypeError, httpx.HTTPError) as exc:
            row["first_blocker"] = str(exc)
            if public.rate_limited:
                for _, deferred in selected:
                    if deferred["first_blocker"] is None:
                        deferred["first_blocker"] = "PUBLIC_RATE_LIMITED_CAPTURE_STOPPED"
                break
        finally:
            result["network_requests"] = len(public.receipts)
    (archive_root / "qualified_scan.json").write_text(json.dumps(result, indent=2))
    return result
