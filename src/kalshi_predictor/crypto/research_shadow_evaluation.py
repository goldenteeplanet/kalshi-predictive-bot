"""Offline evaluation of immutable, unqualified settlement-average shadows.

No model reruns, network, paper accounting, calibration release, or journal writes.
Per-window scenarios remain separate. Event weighting does not assert independence.
"""

from __future__ import annotations

import math
import os
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from kalshi_predictor.crypto import research_shadow as S
from kalshi_predictor.crypto.settlement_target import _json


def load_capture(root: Path) -> dict[str, bytes]:
    total = 0

    def read(path):
        nonlocal total
        size = path.stat().st_size
        if not 0 < size <= 48_000_000 or total + size > 128_000_000:
            raise ValueError("BOUNDED_ORIGINAL_REQUIRED")
        total += size
        with path.open("rb") as f:
            raw = f.read(size + 1)
        if len(raw) != size:
            raise ValueError("ORIGINAL_CHANGED_DURING_READ")
        return raw

    completion = read(root / "completion.json")
    files = {"completion.json": completion}
    manifest = _json(completion)["files"]
    if len(manifest) > 100:
        raise ValueError("BOUNDED_CAPTURE_REQUIRED")
    for name in manifest:
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError("CAPTURE_PATH_ESCAPE")
        files[name] = read(path)
    if (root / "failure.json").exists():
        files["failure.json"] = read(root / "failure.json")
    if sum(map(len, files.values())) > 128_000_000:
        raise ValueError("BOUNDED_CAPTURE_TOTAL_REQUIRED")
    return files


def evaluation_dependencies(capture_files: dict[str, bytes]) -> dict[str, bytes]:
    """Actual executed v2 validation code, distinct from historical model code."""
    if _json(capture_files["plan.original.json"])["schema"] != "cf-average-prospective-slot-v2":
        return {}
    from kalshi_predictor.forecasting import crypto_average_shadow_route as route

    path = Path(route.__file__).resolve()
    if path != Path(__file__).resolve().parents[1] / "forecasting/crypto_average_shadow_route.py":
        raise ValueError("EVALUATION_VALIDATOR_IDENTITY")
    return {str(path): path.read_bytes()}


def write_evaluation(
    output: Path,
    journal: Path,
    capture_files: dict[str, bytes],
    *,
    completion_sha256: str,
    official: dict[str, tuple[bytes, bytes]],
) -> dict:
    """Exclusive immutable publication; recording receipt follows fsynced artifacts."""
    source_path = Path(__file__)
    source_raw = source_path.read_bytes()
    dependencies = evaluation_dependencies(capture_files)
    evaluated_at = S.now()
    result = evaluate(
        journal,
        capture_files,
        completion_sha256=completion_sha256,
        official=official,
        as_of=evaluated_at,
    )
    if any(Path(path).read_bytes() != raw for path, raw in dependencies.items()):
        raise ValueError("VALIDATOR_CHANGED_DURING_EVALUATION")
    output.mkdir(parents=False, exist_ok=False)
    artifacts = {}

    def save(name, raw):
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        artifacts[name] = S.sha(raw)

    try:
        for name, raw in capture_files.items():
            if Path(name).is_absolute() or ".." in Path(name).parts or "\\" in name or ":" in name:
                raise ValueError("SAFE_ORIGINAL_PATH_REQUIRED")
            save("capture/" + name, raw)
        with closing(S.connect(journal, readonly=True)) as db:
            for row in result["rows"]:
                decision_id = row["decision_id"]
                if len(decision_id) != 64 or any(c not in "0123456789abcdef" for c in decision_id):
                    raise ValueError("HASH_DECISION_ID_REQUIRED")
                record = db.execute(
                    "SELECT payload FROM research_shadow WHERE id=?", (decision_id,)
                ).fetchone()[0]
                complete = db.execute(
                    "SELECT payload FROM research_completion WHERE id=?", (decision_id,)
                ).fetchone()[0]
                if (
                    S.sha(record) != row["payload_sha256"]
                    or S.sha(complete) != row["completion_sha256"]
                ):
                    raise ValueError("JOURNAL_CHANGED_DURING_PUBLICATION")
                save(f"journal/{decision_id}.json", record)
                save(f"journal/{decision_id}.completion.json", complete)
        for i, (_ticker, (raw, receipt_raw)) in enumerate(sorted(official.items())):
            save(f"official/{i}.json", raw)
            save(f"official/{i}.receipt.json", receipt_raw)
        save("evaluator.original.py", source_raw)
        for raw in dependencies.values():
            save("route-validator.original.py", raw)
        save("evaluation.json", S.encode(result))
        save("aggregate.json", S.encode(aggregate([result])))
        if source_path.read_bytes() != source_raw or any(
            Path(path).read_bytes() != raw for path, raw in dependencies.items()
        ):
            raise ValueError("EVALUATOR_CHANGED_DURING_PUBLICATION")
        recorded = S.now()
        if recorded < evaluated_at:
            raise ValueError("EVALUATION_RECORDING_CLOCK_INVALID")
        save(
            "recording_receipt.json",
            S.encode(
                dict(
                    status="COMPLETE",
                    recorded_after_artifacts=recorded.isoformat(),
                    evaluator_sha256=S.sha(source_raw),
                    files=dict(artifacts),
                )
            ),
        )
    except Exception:
        with (output / "failure.json").open("xb") as f:
            f.write(S.encode(dict(status="INCOMPLETE_EVALUATION")))
            f.flush()
            os.fsync(f.fileno())
        raise
    return result


def evaluate(
    journal: Path,
    capture_files: dict[str, bytes],
    *,
    completion_sha256: str,
    official: dict[str, tuple[bytes, bytes]],
    as_of: datetime,
) -> dict:
    return _inspect(
        journal, capture_files, completion_sha256=completion_sha256, official=official, as_of=as_of
    )


def validate_capture(
    journal: Path, capture_files: dict[str, bytes], *, completion_sha256: str, as_of: datetime
) -> dict:
    """Full original journal/cohort validation before acquisition, with no invented label."""
    return _inspect(
        journal, capture_files, completion_sha256=completion_sha256, official=None, as_of=as_of
    )


def _inspect(
    journal: Path,
    capture_files: dict[str, bytes],
    *,
    completion_sha256: str,
    official: dict[str, tuple[bytes, bytes]] | None,
    as_of: datetime,
) -> dict:
    """All four preregistered rows, same two contract labels, no forecast recomputation.

    completion_sha256 must be retained externally before target. Caller supplies
    the complete capture originals (excluding the independently checked SQLite DB).
    """
    if "failure.json" in capture_files:
        raise ValueError("FAILED_CAPTURE_REFUSED")
    raw = capture_files["completion.json"]
    if S.sha(raw) != completion_sha256:
        raise ValueError("EXTERNAL_COMPLETION_PIN_MISMATCH")
    completion = _json(raw)
    if completion["status"] != "COMPLETE":
        raise ValueError("CAPTURE_NOT_COMPLETE")
    for name, digest in completion["files"].items():
        if name == "completion.json" or S.sha(capture_files[name]) != digest:
            raise ValueError("CAPTURE_ORIGINAL_CHANGED")
    plan = _json(capture_files["plan.original.json"])
    pins = _json(capture_files["shadow-pins.json"])
    pin_receipt = _json(capture_files["shadow-pins.recorded.json"])
    captured = S.at(completion["recorded_after_result"])
    target_at = S.at(plan["target_at"])
    routed = plan["schema"] == "cf-average-prospective-slot-v2"
    if (
        plan["schema"] not in {"cf-average-prospective-slot-v1", "cf-average-prospective-slot-v2"}
        or pins["schema"] != ("cf-shadow-pins-v2" if routed else "cf-shadow-pins-v1")
        or pins["event"] != plan["event_ticker"]
        or S.at(pins["target"]) != target_at
        or pin_receipt["sha256"] != S.sha(capture_files["shadow-pins.json"])
        or not S.at(plan["not_before"])
        <= S.at(pin_receipt["recorded_at"])
        <= captured
        <= S.at(plan["not_after"])
        < target_at
        <= as_of
        or completion["result_sha256"] != S.sha(capture_files["result.json"])
    ):
        raise ValueError("CAPTURE_CHRONOLOGY_OR_IDENTITY")
    route_sources = {}
    if routed:
        if plan.get("research_route") != "crypto_v3/settlement_average":
            raise ValueError("EXACT_RESEARCH_ROUTE_REQUIRED")
        proof = _json(capture_files["source.originals.json"])
        if not S.same(plan["source_sha256"], {k: v["sha256"] for k, v in proof.items()}):
            raise ValueError("PLANNED_ROUTE_SOURCE_CLOSURE")
        for value in proof.values():
            S.original(value)
        route_sources = {
            k.removeprefix("route."): v for k, v in proof.items() if k.startswith("route.")
        }
    elif "research_route" in plan or any("route_sha256" in p for p in pins["decisions"]):
        raise ValueError("LEGACY_CAPTURE_CANNOT_CLAIM_ROUTE")
    hypotheses = {h["name"]: h for h in plan["hypotheses"]}
    if len(hypotheses) != 2 or len(plan["hypotheses"]) != 2 or len(pins["decisions"]) != 4:
        raise ValueError("ALL_PREDECLARED_WINDOW_SCENARIOS_REQUIRED")
    required = {"LEFT_CLOSED_RIGHT_OPEN": (True, False), "LEFT_OPEN_RIGHT_CLOSED": (False, True)}
    if set(hypotheses) != set(required) or any(
        not S.same([hypotheses[k]["include_start"], hypotheses[k]["include_end"]], list(v))
        for k, v in required.items()
    ):
        raise ValueError("EXACT_PREDECLARED_SCENARIOS_REQUIRED")
    selected = _json(capture_files["selection.json"])["selected"]
    if (
        len(selected) != 2
        or len(set(selected)) != 2
        or (official is not None and set(official) != set(selected))
        or {(p["ticker"], p["hypothesis"]) for p in pins["decisions"]}
        != {(t, h) for t in selected for h in hypotheses}
        or len({p["decision_id"] for p in pins["decisions"]}) != 4
    ):
        raise ValueError("EXACT_TWO_CONTRACT_FOUR_SCENARIO_COHORT_REQUIRED")
    rows = []
    with closing(S.connect(journal, readonly=True)) as db:
        for pin in pins["decisions"]:
            item = db.execute(
                "SELECT payload,payload_sha FROM research_shadow WHERE id=?", (pin["decision_id"],)
            ).fetchone()
            c = db.execute(
                "SELECT payload,payload_sha FROM research_completion WHERE id=?",
                (pin["decision_id"],),
            ).fetchone()
            if (
                not item
                or not c
                or S.sha(item[0]) != item[1]
                or item[1] != pin["payload_sha256"]
                or S.sha(c[0]) != c[1]
                or c[1] != pin["completion_sha256"]
            ):
                raise ValueError("EXTERNAL_JOURNAL_PIN_MISMATCH")
            stored, committed = _json(item[0]), _json(c[0])
            decision = stored["decision"]
            request_raw = bytes.fromhex(stored["request_hex"])
            request = _json(request_raw)
            if (
                request_raw not in capture_files.values()
                or request.get("hypothesis") != pin["hypothesis"]
            ):
                raise ValueError("ARCHIVED_REQUEST_SCENARIO_MISMATCH")
            if (
                S.sha(request_raw) != pin["request_sha256"]
                or decision["request_sha256"] != pin["request_sha256"]
                or decision["decision_id"] != pin["decision_id"]
                or decision["ticker"] != pin["ticker"]
                or decision["event"] != pins["event"]
                or decision["rule_version"] != pin["rule_version"]
                or committed["decision_id"] != pin["decision_id"]
                or committed["payload_sha256"] != item[1]
                or committed["status"] != "COMPLETE_RESEARCH"
            ):
                raise ValueError("JOURNAL_IDENTITY_OR_COMPLETION")
            for source in stored["source_originals"].values():
                S.original(source)
            if routed and any(
                not S.same(source, proof.get(name))
                for name, source in stored["source_originals"].items()
            ):
                raise ValueError("ACTUAL_WRITER_SOURCE_NOT_PLANNED")
            target = S.target_from_request(request)
            decision_at = S.at(decision["decision_at"])
            durable = S.at(committed["original_committed_before"])
            if not decision_at <= S.at(decision["computed_at"]) <= durable <= captured:
                raise ValueError("JOURNAL_VISIBILITY")
            target.validate(as_of=durable)
            if routed:
                from kalshi_predictor.forecasting.crypto_average_shadow_route import (
                    validate_route_receipt,
                )

                route_raw = capture_files[f"route-{pin['decision_id']}.json"]
                route_completion = capture_files[f"route_completion-{pin['decision_id']}.json"]
                if (
                    S.sha(route_raw) != pin["route_sha256"]
                    or S.sha(route_completion) != pin["route_completion_sha256"]
                ):
                    raise ValueError("ROUTE_EXTERNAL_PIN_MISMATCH")
                linked = validate_route_receipt(
                    route_raw,
                    route_completion,
                    decision=dict(decision, journal_completion=committed),
                    payload_sha=item[1],
                    completion_sha=c[1],
                    source_originals=route_sources,
                    as_of=captured,
                )
                if not S.at(plan["not_before"]) <= S.at(linked["route_receipt"]["invoked_at"]):
                    raise ValueError("ROUTE_PRECEDES_PLAN")
            semantic = target.validate(as_of=decision_at)
            if (
                S.sha(S.encode(semantic)) != pin["rule_version"]
                or target.symbol != plan["symbol"]
                or target.rules.index_id != plan["benchmark"]
                or target.comparator != "RANGE_CLOSED"
                or target.rules.closing.end_ms != int(target_at.timestamp() * 1000)
                or target.rules.rounding != plan["rounding"]
                or target.rules.decimal_places != plan["decimal_places"]
            ):
                raise ValueError("PLANNED_TARGET_CHANGED")
            h = hypotheses[pin["hypothesis"]]
            if (
                type(h["include_start"]) is not bool
                or type(h["include_end"]) is not bool
                or target.rules.closing.include_start is not h["include_start"]
                or target.rules.closing.include_end is not h["include_end"]
            ):
                raise ValueError("WINDOW_SCENARIO_CHANGED")
            forecast = decision["forecast"]
            # Older frozen forecasts retain the original target shape. New SOL
            # bindings must exactly match declared semantics; never rerun a model.
            forecast_semantic = target.validate(
                as_of=decision_at,
                include_sol_rule_binding="settlement_rule_binding" in forecast["target"],
            )
            if (
                forecast["model"] != "crypto_settlement_average_research_v1"
                or not S.same(forecast["target"], _json(S.encode(forecast_semantic)))
                or forecast["model_input_as_of"] != decision["decision_at"]
                or forecast["source_sha256"] != request["cf"]["sha256"]
                or forecast["volatility_sha256"] != request["cf"]["sha256"]
            ):
                raise ValueError("FROZEN_FORECAST_SOURCE_BINDING")
            for key in (
                "cf",
                "cf_receipt",
                "book",
                "book_receipt",
                "market",
                "market_receipt",
                "rule",
            ):
                S.original(request[key])
            book_raw, market_raw = S.original(request["book"]), S.original(request["market"])
            ticker = pin["ticker"]
            for key, body, suffix in (
                ("book_receipt", book_raw, "/orderbook?depth=5"),
                ("market_receipt", market_raw, ""),
            ):
                S.receipt(
                    request[key],
                    body,
                    f"https://external-api.kalshi.com/trade-api/v2/markets/{ticker}{suffix}",
                    durable,
                )
            prices = S.book_prices(book_raw, _json(market_raw)["market"])
            # Conservative one-contract YES bid and ask from the same actual book.
            bid, ask = Decimal(1) - prices["NO"], prices["YES"]
            if not 0 < bid < ask < 1:
                raise ValueError("CREDIBLE_MARKET_BASELINE_REQUIRED")
            y, source = (
                (None, None)
                if official is None
                else _official(official[ticker], ticker, target_at, market_raw, captured, as_of)
            )
            p = Decimal(str(forecast["probability"]))
            asset_hour = target.symbol + ":" + target_at.strftime("%Y-%m-%dT%H:00Z")
            scenario = dict(
                index=target.rules.index_id,
                comparator=target.comparator,
                cadence_ms=target.rules.closing.cadence_ms,
                ticks=60,
                include_start=h["include_start"],
                include_end=h["include_end"],
                rounding=target.rules.rounding,
                decimal_places=target.rules.decimal_places,
            )
            rows.append(
                dict(
                    decision_id=pin["decision_id"],
                    event=pins["event"],
                    ticker=ticker,
                    symbol=target.symbol,
                    target_at=target_at.isoformat(),
                    asset_hour=asset_hour,
                    correlation_cluster=target.symbol + ":" + target_at.strftime("%Y-%m-%d"),
                    hypothesis=pin["hypothesis"],
                    semantic_scenario_id=S.sha(S.encode(scenario)),
                    rule_version=pin["rule_version"],
                    scenario=scenario,
                    frozen_probability=str(p),
                    average=scores(p, y) if y is not None else None,
                    market_midpoint=scores((bid + ask) / 2, y) if y is not None else None,
                    market_baseline_method="SAME_BOOK_ONE_CONTRACT_BID_ASK_MIDPOINT",
                    maximum_total_cost_budget_above_5c={
                        "YES": str(p - ask - Decimal(".05")),
                        "NO": str(1 - p - prices["NO"] - Decimal(".05")),
                    },
                    net_ev=None,
                    source=source,
                    payload_sha256=item[1],
                    completion_sha256=c[1],
                )
            )
            if routed:
                rows[-1].update(
                    research_route=plan["research_route"],
                    route_sha256=pin["route_sha256"],
                    route_completion_sha256=pin["route_completion_sha256"],
                )
    return dict(
        schema="crypto-shadow-evaluation-v1"
        if official is not None
        else "crypto-shadow-input-validation-v1",
        rows=rows,
        evaluated_at=as_of.isoformat(),
        capture_completion_sha256=completion_sha256,
        event_count=1,
        independent_n=None,
        calibration_status="DESCRIPTIVE_ONLY",
        paper_pnl=None,
        execution_authority=False,
    )


def _official(pair, ticker, target_at, market_raw, captured, as_of):
    raw, receipt_raw = pair
    r = _json(receipt_raw)
    url = urlsplit(r["url"])
    if (
        r.get("method") != "GET"
        or type(r.get("http_status")) is not int
        or r["http_status"] != 200
        or r.get("original_complete") is not True
        or r["source_sha256"] != S.sha(raw)
        or url.scheme != "https"
        or url.netloc not in {"external-api.kalshi.com", "api.elections.kalshi.com"}
        or url.path != f"/trade-api/v2/markets/{ticker}"
        or url.query
        or url.fragment
    ):
        raise ValueError("OFFICIAL_RECEIPT_REQUIRED")
    requested, received = S.at(r["requested_at"]), S.at(r["received_at"])
    market, original = _json(raw)["market"], _json(market_raw)["market"]
    for key in (
        "ticker",
        "event_ticker",
        "market_type",
        "strike_type",
        "floor_strike",
        "cap_strike",
        "custom_strike",
        "rules_primary",
        "rules_secondary",
    ):
        if not S.same(market.get(key), original.get(key)):
            raise ValueError("OFFICIAL_CONTRACT_CHANGED")
    if (
        market.get("status") != "finalized"
        or market.get("result") not in {"yes", "no"}
        or (market.get("is_provisional") is not None and market.get("is_provisional") is not False)
    ):
        raise ValueError("STRICT_OFFICIAL_FINAL_REQUIRED")
    payout = market.get("settlement_value_dollars")
    if type(payout) not in (str, int, Decimal):
        raise ValueError("EXACT_BINARY_SETTLEMENT_PAYOUT_REQUIRED")
    payout = Decimal(payout)
    if not payout.is_finite() or payout != Decimal(int(market["result"] == "yes")):
        raise ValueError("FINAL_PAYOUT_RESULT_MISMATCH")
    if (
        S.at(market["close_time"]) != target_at
        or not captured < target_at <= requested <= received <= as_of
        or not target_at <= S.at(market["settlement_ts"]) <= received
    ):
        raise ValueError("OFFICIAL_FINAL_CLOCKS")
    return int(market["result"] == "yes"), dict(
        original_sha256=S.sha(raw),
        receipt_sha256=S.sha(receipt_raw),
        received_at=received.isoformat(),
        settlement_at=market["settlement_ts"],
    )


def scores(probability: Decimal, outcome: int) -> dict:
    if (
        type(probability) is not Decimal
        or not probability.is_finite()
        or not 0 <= probability <= 1
        or type(outcome) is not int
        or outcome not in (0, 1)
    ):
        raise ValueError("EXACT_PROBABILITY_BINARY_OUTCOME_REQUIRED")
    assigned = probability if outcome else 1 - probability
    loss = "POSITIVE_INFINITY" if assigned == 0 else -float(assigned.ln())
    return {
        "probability": str(probability),
        "outcome": outcome,
        "brier": str((probability - outcome) ** 2),
        "log_loss": loss,
        "probability_clipped": False,
    }


def aggregate(evaluations: list[dict]) -> dict:
    """Equal events within each predeclared scenario; equal contracts within event.

    ECE bins fixed in advance at [0,.1),...,[.9,1], weighted by these same weights.
    No finite clipping, outcome-based exclusions or independent-N claims.
    """
    groups: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    seen = set()
    seen_contracts = set()
    for evaluation in evaluations:
        if evaluation.get("schema") != "crypto-shadow-evaluation-v1":
            raise ValueError("EXACT_EVALUATION_SCHEMA_REQUIRED")
        for row in evaluation["rows"]:
            identity = row["decision_id"]
            if identity in seen:
                raise ValueError("DUPLICATE_DECISION_EVALUATION")
            seen.add(identity)
            contract = (row["semantic_scenario_id"], row["event"], row["ticker"])
            if contract in seen_contracts:
                raise ValueError("DUPLICATE_CONTRACT_SCENARIO")
            seen_contracts.add(contract)
            groups[row["semantic_scenario_id"]][row["event"]].append(row)
    result = []
    for hypothesis, events in sorted(groups.items()):
        models = {}
        for model in ("average", "market_midpoint"):
            weighted = []
            for rows in events.values():
                weight = Decimal(1) / len(events) / len(rows)
                for row in rows:
                    score = row[model]
                    recomputed = scores(Decimal(score["probability"]), score["outcome"])
                    if not S.same(score, recomputed):
                        raise ValueError("ALTERED_SCORES")
                    weighted.append((weight, score))
            bins: dict[int, list[tuple[Decimal, dict]]] = defaultdict(list)
            for weight, score in weighted:
                p = Decimal(score["probability"])
                bins[min(int(p * 10), 9)].append((weight, score))
            ece = Decimal(0)
            bin_rows = []
            for index in range(10):
                bin_items = bins[index]
                mass = sum((w for w, _ in bin_items), Decimal(0))
                mean_p = sum((w * Decimal(s["probability"]) for w, s in bin_items), Decimal(0))
                mean_y = sum((w * s["outcome"] for w, s in bin_items), Decimal(0))
                ece += abs(mean_p - mean_y)
                bin_rows.append(
                    dict(
                        index=index,
                        weight=str(mass),
                        probability=str(mean_p / mass) if mass else None,
                        outcome=str(mean_y / mass) if mass else None,
                    )
                )
            infinite = any(s["log_loss"] == "POSITIVE_INFINITY" for _, s in weighted)
            models[model] = dict(
                brier=str(sum((w * Decimal(s["brier"]) for w, s in weighted), Decimal(0))),
                log_loss="POSITIVE_INFINITY"
                if infinite
                else math.fsum(float(w) * s["log_loss"] for w, s in weighted),
                ece=str(ece),
                bins=bin_rows,
            )
        all_rows = [r for rows in events.values() for r in rows]
        result.append(
            dict(
                semantic_scenario_id=hypothesis,
                hypothesis=all_rows[0]["hypothesis"],
                contracts=len(all_rows),
                events=len(events),
                asset_hours=len({r["asset_hour"] for r in all_rows}),
                correlation_clusters=sorted({r["correlation_cluster"] for r in all_rows}),
                models=models,
            )
        )
    return dict(
        schema="crypto-shadow-descriptive-aggregate-v1",
        scenarios=result,
        weighting="EQUAL_EVENT_THEN_EQUAL_CONTRACT_WITHIN_EACH_HYPOTHESIS",
        ece_bins="FIXED_TENTHS_LEFT_CLOSED_RIGHT_OPEN_LAST_INCLUDES_ONE",
        calibration_status="DESCRIPTIVE_LOW_N_NOT_CALIBRATED",
        independent_n=None,
        paper_pnl=None,
        performance_promotion=False,
    )
