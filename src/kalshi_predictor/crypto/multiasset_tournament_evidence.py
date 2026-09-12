"""Authenticate existing frozen artifacts and normalize them; no model or HTTP calls."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from kalshi_predictor.crypto.multiasset_capture import at, digest, encode
from kalshi_predictor.crypto.multiasset_outcomes import verify_capture
from kalshi_predictor.crypto.research_shadow_evaluation import _official, scores
from kalshi_predictor.kalshi.orderbook import usable_bid_ask_book


def _read(root: Path, name: str) -> bytes:
    path = root / name
    if (
        Path(name).name != name
        or any(p.is_symlink() for p in (path, *path.parents))
        or not path.is_file()
        or not 0 < path.stat().st_size <= 16000000
    ):
        raise ValueError("UNLINKED_BOUNDED_ORIGINAL_REQUIRED")
    return path.read_bytes()


def verify_outcomes(capture_root, outcome_root, pin_raw, receipt_raw, plan_sha, *, as_of=None):
    as_of = datetime.now(UTC) if as_of is None else as_of
    if as_of.utcoffset() is None:
        raise ValueError("AWARE_AUDIT_CLOCK_REQUIRED")
    plan, decisions, capture_sha = verify_capture(capture_root, pin_raw, receipt_raw, plan_sha)
    target = at(plan["target_at"])
    if (outcome_root / "failure.json").exists():
        raise ValueError("TERMINAL_OUTCOME_FAILURE")
    completion_raw = _read(outcome_root, "completion.json")
    completion = json.loads(completion_raw)
    if (
        completion["status"] != "OFFICIAL_RESEARCH_EVALUATED"
        or completion["requests"] != 2
        or at(completion["at"]) > as_of
        or not target + timedelta(minutes=10)
        <= at(completion["at"])
        < target + timedelta(minutes=11)
        or not 1 <= len(completion["files"]) <= 30
    ):
        raise ValueError("EXACT_COMPLETED_OUTCOME_REQUIRED")
    originals = {}
    for name, expected in completion["files"].items():
        raw = _read(outcome_root, name)
        if digest(raw) != expected:
            raise ValueError("OUTCOME_ORIGINAL_HASH_MISMATCH")
        originals[name] = raw
    if (
        originals["capture-pin.json"] != pin_raw
        or originals["capture-pin-receipt.json"] != receipt_raw
    ):
        raise ValueError("OUTCOME_CAPTURE_PIN_BINDING")
    evaluation = json.loads(originals["evaluation.json"])
    if (
        evaluation["capture_completion_sha256"] != capture_sha
        or not target + timedelta(minutes=10) <= at(evaluation["at"]) <= at(completion["at"])
        or evaluation["execution_authority"] is not False
        or evaluation["paper_pnl"] is not None
    ):
        raise ValueError("EXACT_RESEARCH_EVALUATION_BINDING")
    catalog = json.loads(_read(capture_root, "catalog.original.json"))
    markets = {m["ticker"]: m for m in catalog["markets"]}
    outcomes = {}
    for index, ticker in enumerate(sorted({d["ticker"] for d in decisions})):
        outcomes[ticker] = _official(
            (originals[f"{index}.original.json"], originals[f"{index}.receipt.json"]),
            ticker,
            target,
            encode({"market": markets[ticker]}),
            min(at(d["decision_at"]) for d in decisions),
            as_of,
        )
    expected_rows = {}
    normalized = []
    economics = []
    selection = json.loads(_read(capture_root, "selection.json"))["tickers"]
    for decision in decisions:
        unsigned = {k: v for k, v in decision.items() if k != "decision_id"}
        if digest(encode(unsigned)) != decision["decision_id"]:
            raise ValueError("DECISION_ID_CONTENT_MISMATCH")
        outcome, official = outcomes[decision["ticker"]]
        forecasts = decision["models"]["models"]
        for model, forecast in forecasts.items():
            if forecast["probability"] is not None:
                expected_rows[(decision["decision_id"], model)] = {
                    "decision_id": decision["decision_id"],
                    "event": decision["event"],
                    "ticker": decision["ticker"],
                    "asset": decision["symbol"],
                    "rule_version": decision["rule_version"],
                    "model": model,
                    "score": scores(Decimal(str(forecast["probability"])), outcome),
                    "official": official,
                    "hypothetical_after_cost_pnl": None,
                    "full_net_ev": None,
                    "cost_status": "UNKNOWN_ORIGINAL_COSTS",
                    "independent_n": None,
                }
        yes_rows = [r for r in decision["rows"] if r["side"] == "YES"]
        book = yes_rows[0]["book"]
        bid, ask = book["bid_price"], book["ask_price"]
        spread = None if bid is None or ask is None else str(Decimal(ask) - Decimal(bid))
        normalized.append(
            {
                "decision_id": decision["decision_id"],
                "event": decision["event"],
                "ticker": decision["ticker"],
                "asset": decision["symbol"],
                "endpoint_hypothesis": "[t-60,t)"
                if decision["rule"]["include_start"]
                else "(t-60,t]",
                "rule_version": decision["rule_version"],
                "target_at": target.isoformat(),
                "outcome": outcome,
                "probabilities": {m: f["probability"] for m, f in forecasts.items()},
                "registered_lead_minutes": round(
                    (target - at(plan["not_before"])).total_seconds() / 60
                ),
                "actual_lead_seconds": (target - at(decision["decision_at"])).total_seconds(),
                "variance_per_second": decision["models"]["variance_per_second"],
                "spread": spread,
            }
        )
        for candidate in decision["rows"]:
            book_index = selection.index(decision["ticker"])
            book_original = json.loads(_read(capture_root, f"book-{book_index}-1.original.json"))
            original_quote = usable_bid_ask_book(book_original, side=candidate["side"])
            if encode(candidate["book"]) != encode(asdict(original_quote)):
                raise ValueError("BOOK_FIELDS_DISAGREE_WITH_ORIGINAL")
            costs = candidate["costs"]
            if costs is None:
                continue
            p = costs["probability"]
            price = costs["executable_price"]
            gross = Decimal(p) - Decimal(price)
            if gross != Decimal(costs["gross_edge"]):
                raise ValueError("STORED_GROSS_EDGE_MISMATCH")
            # This particular frozen cohort explicitly persisted all costs UNKNOWN.
            if (
                any(
                    costs[k]["value"] is not None or costs[k]["status"] != "UNKNOWN"
                    for k in ("fee", "slippage", "uncertainty")
                )
                or costs["full_net_ev"] is not None
                or costs["clears_ev_gate"] is not False
            ):
                raise ValueError("FROZEN_UNKNOWN_COST_CONTRACT_CHANGED")
            economics.append(
                {
                    "decision_id": decision["decision_id"],
                    "event": decision["event"],
                    "ticker": decision["ticker"],
                    "asset": decision["symbol"],
                    "model": candidate["model"],
                    "side": candidate["side"],
                    "rule_version": decision["rule_version"],
                    "probability": p,
                    "executable_price": price,
                    "gross_edge": str(gross),
                    "fee": costs["fee"],
                    "slippage": costs["slippage"],
                    "uncertainty": costs["uncertainty"],
                    "full_net_ev": None,
                    "full_net_shortfall": None,
                    "gross_only_shortfall_to_gate": str(max(Decimal(0), Decimal(".05") - gross)),
                    "gross_positive": gross > 0,
                    "gross_only_screen_within_5c_of_gate": Decimal(0) <= gross <= Decimal(".10"),
                    "formal_full_net_near_miss": None,
                    "hypothetical_after_cost_pnl": None,
                    "has_executable_depth": original_quote.has_executable_depth,
                    "blockers": [
                        "RULE_UNCERTIFIED",
                        "CALIBRATION",
                        "UNCERTAINTY",
                        "FEE",
                        "SLIPPAGE",
                    ],
                    "paper_eligible": False,
                }
            )
    actual_rows = {}
    for row in evaluation["rows"]:
        key = (row["decision_id"], row["model"])
        if key in actual_rows:
            raise ValueError("DUPLICATE_EVALUATION_ROW")
        actual_rows[key] = row
    if actual_rows.keys() != expected_rows.keys() or any(
        encode(actual_rows[k]) != encode(expected_rows[k]) for k in expected_rows
    ):
        raise ValueError("EVALUATION_DOES_NOT_MATCH_STORED_PROSPECTIVE_PREDICTIONS")
    _, _, after = verify_capture(capture_root, pin_raw, receipt_raw, plan_sha)
    if after != capture_sha or _read(outcome_root, "completion.json") != completion_raw:
        raise ValueError("ARTIFACT_CHANGED_DURING_AUDIT")
    return {
        "rows": normalized,
        "economics": economics,
        "capture_completion_sha256": capture_sha,
        "outcome_completion_sha256": digest(completion_raw),
        "verified_score_rows": len(expected_rows),
    }
