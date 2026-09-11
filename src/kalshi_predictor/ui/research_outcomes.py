"""Small official research evaluation projection; no paper settlement or model replay."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from decimal import Decimal
from urllib.parse import urlsplit

from kalshi_predictor.ui import research_journals as R

EVALUATOR = "src/kalshi_predictor/crypto/research_shadow_evaluation.py"


def number(value):
    if type(value) not in (str, int, float):
        raise ValueError("NUMBER_TYPE")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("FINITE_NUMBER")
    return result


def identical(a, b):
    return json.dumps(a, sort_keys=True, allow_nan=False) == json.dumps(
        b, sort_keys=True, allow_nan=False
    )


def score(value, p, y):
    if (
        value["probability_clipped"] is not False
        or type(value["outcome"]) is not int
        or value["outcome"] != y
        or number(value["probability"]) != p
        or not 0 <= p <= 1
        or number(value["brier"]) != (p - y) ** 2
    ):
        raise ValueError("SCORE_ARITHMETIC")
    assigned = p if y else 1 - p
    expected = "POSITIVE_INFINITY" if assigned == 0 else -float(assigned.ln())
    actual = value["log_loss"]
    if expected == "POSITIVE_INFINITY":
        if actual != expected:
            raise ValueError("INFINITE_LOSS_REQUIRED")
    elif (
        type(actual) not in (int, float)
        or not math.isfinite(actual)
        or abs(actual - expected) > 1e-12
    ):
        raise ValueError("LOG_LOSS_ARITHMETIC")
    return number(value["brier"]), expected


def outcome_view(base, control, slot, now):
    result: dict = dict(status="PENDING_OFFICIAL_RESEARCH", labels=[], scenarios=[])
    if slot["status"] != "COMPLETE_PIN_BOUND_DISPLAY":
        result["status"] = "CAPTURE_UNVERIFIED"
        return result
    root = base / f"slot-{slot['slot']}-official-outcome"
    evaluation = root / "evaluation"
    try:
        if any(
            R.safe(path).exists() for path in (root / "failure.json", evaluation / "failure.json")
        ):
            result["status"] = "FAILED_RESEARCH_EVALUATION"
            return result
        if not R.safe(root / "result.json").exists():
            return result
        terminal = R.decode(R.read(root / "result.json"))
        if (
            terminal["target_at"] != slot["target"]
            or terminal["execution_authority"] is not False
            or terminal["paper_pnl"] is not None
        ):
            raise ValueError("TERMINAL_IDENTITY")
        if terminal["status"] != "SCORED":
            result["status"] = (
                "PENDING_OR_UNAVAILABLE_NO_RETRY"
                if terminal["status"] == "PENDING_OR_UNAVAILABLE_NO_RETRY"
                else "PENDING_OFFICIAL_RESEARCH"
                if terminal["status"] == "PENDING"
                else "UNAVAILABLE_RESEARCH_EVALUATION"
            )
            return result
        receipt = R.decode(R.read(evaluation / "recording_receipt.json"))
        frozen = R.decode(R.read(control / "outcome-source-pins.json"))
        manifest = receipt["files"]
        if type(manifest) is not dict or len(manifest) > 100 or receipt["status"] != "COMPLETE":
            raise ValueError("BOUNDED_EVALUATION_MANIFEST")

        def original(name):
            raw = R.read(evaluation / name)
            if R.digest(raw) != manifest[name]:
                raise ValueError("EVALUATION_ORIGINAL_HASH")
            return raw

        evaluator = original("evaluator.original.py")
        if (
            R.digest(evaluator) != receipt["evaluator_sha256"]
            or R.digest(evaluator) != frozen["source_sha256"][EVALUATOR]
        ):
            raise ValueError("REVIEWED_EVALUATOR_SOURCE")
        completion_raw = original("capture/completion.json")
        if R.digest(completion_raw) != slot["capture_completion_sha256"]:
            raise ValueError("CAPTURE_COMPLETION_BINDING")
        completion = R.decode(completion_raw)
        captured = R.clock(completion["recorded_after_result"])
        if R.clock(frozen["frozen_at"]) >= captured or frozen["execution_authority"] is not False:
            raise ValueError("SOURCE_PREREGISTRATION")
        evaluated = R.decode(original("evaluation.json"))
        at = R.clock(evaluated["evaluated_at"])
        recorded = R.clock(receipt["recorded_after_artifacts"])
        target = R.clock(slot["target"])
        if (
            evaluated["schema"] != "crypto-shadow-evaluation-v1"
            or evaluated["capture_completion_sha256"] != slot["capture_completion_sha256"]
            or type(evaluated["event_count"]) is not int
            or evaluated["event_count"] != 1
            or evaluated["execution_authority"] is not False
            or evaluated["paper_pnl"] is not None
            or evaluated["independent_n"] is not None
            or evaluated["calibration_status"] != "DESCRIPTIVE_ONLY"
            or not captured < target <= at <= recorded <= now
            or len(evaluated["rows"]) != 4
        ):
            raise ValueError("EVALUATION_IDENTITY_OR_CLOCK")
        originals = {}
        for i in range(2):
            name = f"{i}-market.original"
            raw = original("capture/" + name)
            if R.digest(raw) != completion["files"][name]:
                raise ValueError("CAPTURE_MARKET_HASH")
            market = R.decode(raw)["market"]
            originals[market["ticker"]] = market
        if set(originals) != {r["ticker"] for r in slot["rows"]}:
            raise ValueError("SELECTED_MARKETS")
        labels = {}
        for i in range(2):
            raw = original(f"official/{i}.json")
            rraw = original(f"official/{i}.receipt.json")
            market = R.decode(raw)["market"]
            r = R.decode(rraw)
            ticker = market["ticker"]
            prior = originals[ticker]
            url = urlsplit(r["url"])
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
                if not identical(market.get(key), prior.get(key)):
                    raise ValueError("OFFICIAL_CONTRACT_CHANGED")
            if (
                ticker in labels
                or market["event_ticker"] != slot["event"]
                or market["market_type"] != "binary"
                or market["status"] != "finalized"
                or market["result"] not in ("yes", "no")
                or (
                    market.get("is_provisional") is not None
                    and market.get("is_provisional") is not False
                )
                or type(r["http_status"]) is not int
                or r["http_status"] != 200
                or r["method"] != "GET"
                or r["original_complete"] is not True
                or r["source_sha256"] != R.digest(raw)
                or url.scheme != "https"
                or url.netloc not in ("external-api.kalshi.com", "api.elections.kalshi.com")
                or url.path != "/trade-api/v2/markets/" + ticker
                or url.query
                or url.fragment
                or R.clock(market["close_time"]) != target
                or not target <= R.clock(r["requested_at"]) <= R.clock(r["received_at"]) <= at
                or not target <= R.clock(market["settlement_ts"]) <= R.clock(r["received_at"])
            ):
                raise ValueError("STRICT_OFFICIAL_FINAL")
            y = int(market["result"] == "yes")
            if number(market["settlement_value_dollars"]) != y:
                raise ValueError("PAYOUT_LABEL_MISMATCH")
            labels[ticker] = dict(
                label=y,
                original_sha256=R.digest(raw),
                receipt_sha256=R.digest(rraw),
                received_at=r["received_at"],
                settlement_at=market["settlement_ts"],
            )
        states = terminal["states"]
        if (
            type(states) is not list
            or len(states) != 2
            or {x["ticker"] for x in states} != set(labels)
            or any(
                x["status"] != "finalized"
                or x["original_sha256"] != labels[x["ticker"]]["original_sha256"]
                for x in states
            )
        ):
            raise ValueError("TERMINAL_ORIGINAL_IDENTITY")
        trusted = {r["decision_id"]: r for r in slot["rows"]}
        seen = set()
        groups = defaultdict(list)
        for row in evaluated["rows"]:
            ident = row["decision_id"]
            prior = trusted[ident]
            if (
                ident in seen
                or row["ticker"] != prior["ticker"]
                or row["event"] != slot["event"]
                or row["target_at"] != slot["target"]
                or row["hypothesis"] != prior["hypothesis"]
                or row["payload_sha256"] != prior["payload_sha256"]
                or row["completion_sha256"] != prior["completion_sha256"]
                or row["rule_version"] != prior["rule_version"]
                or row["market_baseline_method"] != "SAME_BOOK_ONE_CONTRACT_BID_ASK_MIDPOINT"
                or row["net_ev"] is not None
                or row["symbol"] != "SOL"
            ):
                raise ValueError("JOURNAL_EVALUATION_BINDING")
            expected_scenario = dict(
                index="SOLUSD_RTI",
                comparator="RANGE_CLOSED",
                cadence_ms=1000,
                ticks=60,
                decimal_places=4,
                rounding="HALF_EVEN",
                include_start=row["hypothesis"] == "LEFT_CLOSED_RIGHT_OPEN",
                include_end=row["hypothesis"] == "LEFT_OPEN_RIGHT_CLOSED",
            )
            if not identical(row["scenario"], expected_scenario):
                raise ValueError("DECLARED_SCENARIO_IDENTITY")
            seen.add(ident)
            label = labels[row["ticker"]]
            if not identical(row["source"], {k: v for k, v in label.items() if k != "label"}):
                raise ValueError("OFFICIAL_SOURCE_BINDING")
            p = number(prior["probability"])
            if number(row["frozen_probability"]) != p:
                raise ValueError("FROZEN_PROBABILITY")
            quotes = prior["quotes"]
            if (
                type(quotes) is not list
                or len(quotes) != 2
                or {q["side"] for q in quotes} != {"YES", "NO"}
            ):
                raise ValueError("BOUND_BOOK_QUOTES")
            prices = {q["side"]: number(q["executable_price"]) for q in quotes}
            if not 0 < 1 - prices["NO"] < prices["YES"] < 1:
                raise ValueError("ORIGINAL_BOOK_BASELINE")
            midpoint = (1 - prices["NO"] + prices["YES"]) / 2
            groups[row["hypothesis"]].append(
                dict(
                    average=score(row["average"], p, label["label"]),
                    market=score(row["market_midpoint"], midpoint, label["label"]),
                )
            )
        if set(groups) != {"LEFT_CLOSED_RIGHT_OPEN", "LEFT_OPEN_RIGHT_CLOSED"} or any(
            len(v) != 2 for v in groups.values()
        ):
            raise ValueError("TWO_CONTRACT_SCENARIOS")
        scenarios = []
        for name, values in sorted(groups.items()):
            metrics = {}
            for model in ("average", "market"):
                losses = [v[model][1] for v in values]
                metrics[model] = dict(
                    brier=str(sum(v[model][0] for v in values) / 2),
                    log_loss="POSITIVE_INFINITY"
                    if "POSITIVE_INFINITY" in losses
                    else math.fsum(losses) / 2,
                )
            scenarios.append(dict(hypothesis=name, contracts=2, events=1, metrics=metrics))
        result.update(
            status="SCORED_OFFICIAL_RESEARCH",
            labels=[
                dict(ticker=t, outcome="YES" if v["label"] else "NO")
                for t, v in sorted(labels.items())
            ],
            scenarios=scenarios,
            recorded_at=receipt["recorded_after_artifacts"],
        )
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, ArithmeticError):
        result = dict(status="UNVERIFIED_RESEARCH_EVALUATION", labels=[], scenarios=[])
    return result
