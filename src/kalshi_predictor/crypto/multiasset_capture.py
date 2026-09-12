"""Bounded prospective multi-asset research capture, with no order surface."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from kalshi_predictor.crypto.catalog_liquidity import catalog_liquidity
from kalshi_predictor.crypto.cf_process_inputs import INDEX, decode_cf_original
from kalshi_predictor.crypto.doge_strikes import parse_doge_strike
from kalshi_predictor.crypto.multiasset_challengers import forecast_challengers
from kalshi_predictor.crypto.research_costs import full_costs, unknown
from kalshi_predictor.crypto.settlement_rule_version import CryptoSettlementRuleVersion
from kalshi_predictor.kalshi.orderbook import usable_bid_ask_book

BASE = "https://external-api.kalshi.com/trade-api/v2"
FAMILY = {"BTC": "KXBTC", "ETH": "KXETH", "SOL": "KXSOLE", "XRP": "KXXRP", "DOGE": "KXDOGE"}


def encode(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False
    ).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def at(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("AWARE_CLOCK_REQUIRED")
    return result


def source_manifest():
    root = Path(__file__).resolve().parents[3]
    files = list((root / "src").rglob("*.py"))
    runner = root / "scripts/multiasset_cohort_runner.py"
    if runner.exists():
        files.append(runner)
    return {str(p.relative_to(root)): digest(p.read_bytes()) for p in sorted(files)}


def persist(path, raw):
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def capture(output: Path, plan: dict, transport, *, clock=lambda: datetime.now(UTC)):
    """Six GET maximum: catalog, CF, and two snapshots for each selected book."""
    symbol = plan["symbol"]
    event = plan["event"]
    target = at(plan["target_at"])
    before, after = at(plan["not_before"]), at(plan["not_after"])
    if (
        symbol not in FAMILY
        or not re.fullmatch(r"[A-Z0-9-]{1,80}", event)
        or not event.startswith(FAMILY[symbol] + "-")
        or plan["benchmark"] != INDEX[symbol]
        or plan["max_gets"] != 6
        or plan["source_manifest"] != source_manifest()
        or not timedelta(0) < after - before <= timedelta(seconds=90)
        or not before <= clock() < after < target - timedelta(minutes=1)
    ):
        raise ValueError("EXACT_FUTURE_SOURCE_PINNED_PLAN_REQUIRED")
    if any(p.is_symlink() for p in (output, *output.parents)):
        raise ValueError("SYMLINK_OUTPUT_REFUSED")
    output.mkdir(exist_ok=False)
    manifest = {}
    requests = 0
    last = before

    def check():
        nonlocal last
        current = clock()
        if not last <= current < after:
            raise ValueError("CAPTURE_CLOCK_OR_DEADLINE")
        last = current
        return current

    def save(name, raw):
        persist(output / name, raw)
        manifest[name] = digest(raw)

    def get(label, url):
        nonlocal requests
        stamp = check()
        if requests >= 6:
            raise ValueError("REQUEST_BUDGET")
        save(
            label + ".reservation.json",
            encode({"url": url, "at": stamp.isoformat(), "attempt": requests + 1}),
        )
        requested = check()
        requests += 1
        status, raw = transport(url, min(12, (after - requested).total_seconds()))
        received = clock()
        if type(raw) is not bytes or not 0 < len(raw) <= 6000000:
            raise ValueError("ORIGINAL_SIZE_LIMIT")
        save(label + ".original.json", raw)
        receipt = {
            "url": url,
            "requested_at": requested.isoformat(),
            "received_at": received.isoformat(),
            "http_status": status,
            "sha256": digest(raw),
        }
        save(label + ".receipt.json", encode(receipt))
        if not requested <= received <= check() or status != 200:
            raise ValueError("HTTP_OR_RECEIPT_FAILURE")
        return raw, receipt

    try:
        save("protocol.json", encode(plan))
        catalog_raw, catalog_receipt = get(
            "catalog", BASE + "/markets?event_ticker=" + event + "&limit=1000"
        )
        catalog = json.loads(catalog_raw, parse_float=Decimal)
        if (
            catalog.get("cursor") != ""
            or not isinstance(catalog.get("markets"), list)
            or not 2 <= len(catalog["markets"]) <= 1000
        ):
            raise ValueError("COMPLETE_CATALOG_REQUIRED")
        markets = catalog["markets"]
        plain_markets = json.loads(catalog_raw)["markets"]
        ranges = []
        tickers = set()
        for market_index, row in enumerate(markets):
            ticker = row["ticker"]
            if (
                not re.fullmatch(r"[A-Z0-9.-]{1,128}", ticker)
                or ticker in tickers
                or row["event_ticker"] != event
                or not ticker.startswith(event + "-")
                or row.get("market_type") != "binary"
                or row.get("status") not in {"active", "open"}
                or at(row["close_time"]) != target
            ):
                raise ValueError("EXACT_ACTIVE_EVENT_REQUIRED")
            tickers.add(ticker)
            if symbol == "DOGE" and row.get("strike_type") == "custom":
                strike = parse_doge_strike(plain_markets[market_index], cutoff=check())
                if strike.operator == "between":
                    assert strike.floor is not None and strike.cap is not None
                    ranges.append((row, strike.floor, strike.cap))
            elif row.get("strike_type") == "between" and row.get("custom_strike") in (None, {}):
                lo, hi = Decimal(str(row["floor_strike"])), Decimal(str(row["cap_strike"]))
                if not lo.is_finite() or not hi.is_finite() or not 0 < lo < hi:
                    raise ValueError("FINITE_ORDERED_RANGE_REQUIRED")
                ranges.append((row, lo, hi))
        cf_raw, cf_receipt = get("cf", BASE + "/cfbenchmarks/values?id=" + INDEX[symbol])
        cf = decode_cf_original(
            cf_raw,
            sha256=digest(cf_raw),
            request_url=cf_receipt["url"],
            index_id=INDEX[symbol],
            profile="LATEST_1HZ",
        )
        if cf.server_time > at(cf_receipt["received_at"]):
            raise ValueError("CF_FUTURE_SERVER_TIME")
        ranges.sort(
            key=lambda item: (abs((item[1] + item[2]) / 2 - cf.values[-1]), item[0]["ticker"])
        )
        if len(ranges) < 2:
            raise ValueError("TWO_RANGE_CONTRACTS_REQUIRED")
        selected = ranges[:2]
        save(
            "selection.json",
            encode(
                {
                    "at": check().isoformat(),
                    "tickers": [r[0]["ticker"] for r in selected],
                    "catalog_sha256": digest(catalog_raw),
                    "cf_sha256": digest(cf_raw),
                }
            ),
        )
        books = []
        for index, (row, _, _) in enumerate(selected):
            pair = []
            for sample in range(2):
                raw, receipt = get(
                    f"book-{index}-{sample}",
                    BASE + "/markets/" + row["ticker"] + "/orderbook?depth=10",
                )
                pair.append((json.loads(raw), receipt))
            books.append(pair)
        decisions = []
        for index, (market, lower, upper) in enumerate(selected):
            book, receipt = books[index][-1]
            yes = usable_bid_ask_book(book, side="YES")
            baseline = None
            if (
                yes.has_executable_depth
                and yes.bid_price is not None
                and yes.ask_price is not None
                and 0 < yes.bid_price <= yes.ask_price < 1
            ):
                baseline = float((yes.bid_price + yes.ask_price) / 2)
            for hypothesis in plan["rule_hypotheses"]:
                rule = CryptoSettlementRuleVersion(
                    **{
                        **hypothesis,
                        "field_evidence": tuple(tuple(x) for x in hypothesis["field_evidence"]),
                    }
                )
                version = rule.bind(family=FAMILY[symbol], benchmark_id=INDEX[symbol])
                if (
                    rule.include_end is None
                    or rule.final_precision is None
                    or rule.sample_frequency_ms != 1000
                    or rule.sample_count != 60
                    or rule.start_offset_ms != -60000
                    or rule.end_offset_ms != 0
                    or rule.include_start == rule.include_end
                    or rule.final_rounding != "HALF_EVEN"
                ):
                    raise ValueError("EXPLICIT_RESEARCH_RULE_HYPOTHESIS_REQUIRED")
                precision = Decimal(rule.final_precision)
                exponent = precision.as_tuple().exponent
                if not precision.is_finite() or precision <= 0 or not isinstance(exponent, int):
                    raise ValueError("FINITE_RULE_PRECISION_REQUIRED")
                if precision != Decimal(1).scaleb(exponent):
                    raise ValueError("DECIMAL_POWER_RULE_PRECISION_REQUIRED")
                places = -exponent
                decision_time = check()
                liquidity = catalog_liquidity(
                    market,
                    catalog_sha256=digest(catalog_raw),
                    received_at=at(catalog_receipt["received_at"]),
                    decision_at=decision_time,
                )
                if (
                    not timedelta(0)
                    <= decision_time - at(receipt["received_at"])
                    <= timedelta(seconds=60)
                ):
                    raise ValueError("CURRENT_BOOK_REQUIRED")
                models = forecast_challengers(
                    timestamps_ms=tuple(cf.timestamps_ms),
                    prices=tuple(float(x) for x in cf.values),
                    decision_ms=int(decision_time.timestamp() * 1000),
                    target_ms=int(target.timestamp() * 1000),
                    lower=float(lower),
                    upper=float(upper),
                    decimal_places=places,
                    include_end=rule.include_end,
                    seed=plan["seed"] + index,
                )
                models["models"]["credible_book_midpoint_v1"] = {
                    "probability": baseline,
                    "book_sha256": receipt["sha256"],
                }
                rows = []
                for name, forecast in models["models"].items():
                    p = forecast["probability"]
                    for side in ("YES", "NO"):
                        quote = usable_bid_ask_book(
                            book, side=side, liquidity_score=liquidity["score"]
                        )
                        costs = None
                        if (
                            p is not None
                            and quote.ask_price is not None
                            and 0 < quote.ask_price < 1
                        ):
                            costs = asdict(
                                full_costs(
                                    probability=Decimal(str(p if side == "YES" else 1 - p)),
                                    executable_price=quote.ask_price,
                                    fee=unknown(
                                        "FEE_REVIEW_PENDING",
                                        "ACCOUNT_PRECISION_AND_CURRENT_FEE_REVIEW_REQUIRED",
                                    ),
                                    slippage=unknown(
                                        "BOOK_MOVEMENT_REVIEW_PENDING",
                                        "TWO_SNAPSHOTS_ARCHIVED_NOT_YET_QUALIFIED",
                                    ),
                                    uncertainty=unknown(
                                        "CALIBRATION_PENDING", "INDEPENDENT_CALIBRATION_UNPROVEN"
                                    ),
                                )
                            )
                        rows.append(
                            {
                                "model": name,
                                "side": side,
                                "forecast": forecast,
                                "book": asdict(quote),
                                "costs": costs,
                            }
                        )
                payload = {
                    "event": event,
                    "ticker": market["ticker"],
                    "symbol": symbol,
                    "decision_at": decision_time.isoformat(),
                    "target_at": target.isoformat(),
                    "rule_version": version,
                    "rule": asdict(rule),
                    "rule_status": rule.status,
                    "models": models,
                    "catalog_liquidity": liquidity,
                    "rows": rows,
                    "input_manifest": dict(manifest),
                    "protocol_sha256": digest(encode(plan)),
                    "status": "UNCERTIFIED_PROSPECTIVE_RESEARCH",
                    "execution_authority": False,
                    "paper_eligible": False,
                }
                ident = digest(encode(payload))
                payload["decision_id"] = ident
                save("decision-" + ident + ".json", encode(payload))
                decisions.append(payload)
        check()
        with closing(sqlite3.connect(output / "research.db")) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                "CREATE TABLE decisions(id TEXT PRIMARY KEY,payload BLOB NOT NULL,"
                "sha256 TEXT NOT NULL,stored_at TEXT NOT NULL)"
            )
            for payload in decisions:
                raw = encode(payload)
                db.execute(
                    "INSERT INTO decisions VALUES(?,?,?,?)",
                    (payload["decision_id"], raw, digest(raw), check().isoformat()),
                )
            db.commit()
        if source_manifest() != plan["source_manifest"]:
            raise ValueError("SOURCE_CHANGED_DURING_CAPTURE")
        manifest["research.db"] = digest((output / "research.db").read_bytes())
        save(
            "completion.json",
            encode(
                {
                    "status": "COMPLETE_RESEARCH",
                    "at": check().isoformat(),
                    "files": dict(manifest),
                    "decisions": [d["decision_id"] for d in decisions],
                    "requests": requests,
                }
            ),
        )
        check()
        return {
            "decisions": len(decisions),
            "requests": requests,
            "completion_sha256": manifest["completion.json"],
        }
    except Exception as exc:
        persist(
            output / "failure.json",
            encode(
                {"at": clock().isoformat(), "error_class": type(exc).__name__, "requests": requests}
            ),
        )
        raise
