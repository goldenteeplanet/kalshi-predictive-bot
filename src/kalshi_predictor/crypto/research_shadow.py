"""Original-bound average forecasts in a separate, non-admitting research journal.

No ORM, exchange client, sizing, risk or paper writer is reachable from this API.
Exchange fees can be verified; unverified slippage/uncertainty keep total EV unknown.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from kalshi_predictor.crypto.cf_settlement_windows import CFWindow, CFWindowRules
from kalshi_predictor.crypto.settlement_average_model import forecast_benchmark_average
from kalshi_predictor.crypto.settlement_target import SettlementBenchmarkTarget, _json
from kalshi_predictor.kalshi.protocol_math import is_valid_market_price
from kalshi_predictor.paper.fees import verify_fee_quote

APPLICATION_ID = 0x43525348
LIMIT = 48_000_000


def now() -> datetime:
    return datetime.now(UTC)


def encode(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def same(a: Any, b: Any) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b, strict=True))
    return bool(a == b)


def at(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("AWARE_RECEIPT_REQUIRED")
    return result.astimezone(UTC)


def original(item: dict) -> bytes:
    raw = bytes.fromhex(item["hex"])
    if not 0 < len(raw) <= LIMIT or sha(raw) != item["sha256"]:
        raise ValueError("ORIGINAL_HASH_MISMATCH")
    return raw


def target_from_request(request: dict) -> SettlementBenchmarkTarget:
    t = request["target"]
    rules = dict(t["rules"])
    rules["closing"] = CFWindow(**rules["closing"])
    if rules["opening"] is not None:
        rules["opening"] = CFWindow(**rules["opening"])
    return SettlementBenchmarkTarget(
        t["symbol"],
        t["event_ticker"],
        CFWindowRules(**rules),
        t["comparator"],
        Decimal(t["threshold"]) if t["threshold"] is not None else None,
        Decimal(t["lower"]) if t["lower"] is not None else None,
        Decimal(t["upper"]) if t["upper"] is not None else None,
        original(request["rule"]),
        at(t["rule_received_at"]),
        original(request["market"]),
        at(t["market_received_at"]),
        at(t["finality_deadline"]) if t["finality_deadline"] else None,
        t["finality_basis"],
    )


def receipt(item: dict, raw: bytes, url: str, as_of: datetime) -> dict:
    result = _json(original(item))
    actual, expected = urlsplit(result.get("url", "")), urlsplit(url)
    if (
        result.get("method") != "GET"
        or actual.scheme != "https"
        or actual.netloc not in {"api.elections.kalshi.com", "external-api.kalshi.com"}
        or (actual.path, actual.query, actual.fragment) != (expected.path, expected.query, "")
        or type(result.get("http_status")) is not int
        or result["http_status"] != 200
        or result.get("source_sha256") != sha(raw)
        or result.get("original_complete") is not True
    ):
        raise ValueError("EXACT_SUCCESS_RECEIPT_REQUIRED")
    requested, received = at(result["requested_at"]), at(result["received_at"])
    if not requested <= received <= as_of or as_of - received > timedelta(seconds=60):
        raise ValueError("FRESH_VISIBLE_RECEIPT_REQUIRED")
    return result


def book_prices(raw: bytes, market: dict) -> dict[str, Decimal]:
    # Only the documented explicit dollar fixed-point shape is supported.
    if market.get("price_level_structure") not in {"linear_cent", "deci_cent", "tapered_deci_cent"}:
        raise ValueError("EXPLICIT_TICK_SCHEDULE_REQUIRED")
    if "price_ranges" in market:
        ranges = market["price_ranges"]
        if type(ranges) is not list or not 0 < len(ranges) <= 10:
            raise ValueError("INVALID_EXPLICIT_TICK_RANGES")
        previous = Decimal(0)
        for item in ranges:
            if type(item) is not dict or set(item) != {"start", "end", "step"}:
                raise ValueError("INVALID_EXPLICIT_TICK_RANGES")
            if any(type(item[k]) is not str for k in item):
                raise ValueError("EXACT_TICK_DECIMALS_REQUIRED")
            start, end, step = (Decimal(item[k]) for k in ("start", "end", "step"))
            if (
                not all(x.is_finite() for x in (start, end, step))
                or start != previous
                or not start < end <= 1
                or not 0 < step <= end - start
            ):
                raise ValueError("INVALID_EXPLICIT_TICK_RANGES")
            previous = end
        if previous != 1:
            raise ValueError("INCOMPLETE_TICK_RANGES")
    book = _json(raw)["orderbook_fp"]
    bids = {}
    top = {}
    for side in ("yes", "no"):
        levels = book[side + "_dollars"]
        if type(levels) is not list or not 0 < len(levels) <= 100:
            raise ValueError("VISIBLE_ONE_CONTRACT_DEPTH_REQUIRED")
        seen: set[Decimal] = set()
        eligible = []
        for row in levels:
            if type(row) is not list or len(row) != 2 or any(type(x) is not str for x in row):
                raise ValueError("EXACT_BOOK_DECIMALS_REQUIRED")
            price, size = map(Decimal, row)
            if (
                not price.is_finite()
                or not size.is_finite()
                or size <= 0
                or not 0 < price < 1
                or price in seen
                or not is_valid_market_price(market, price)
            ):
                raise ValueError("INVALID_BOOK_LEVEL")
            seen.add(price)
            if size >= 1:
                eligible.append(price)
        if not eligible:
            raise ValueError("VISIBLE_ONE_CONTRACT_DEPTH_REQUIRED")
        bids[side] = max(eligible)
        top[side] = max(seen)
    if top["yes"] + top["no"] >= 1:
        raise ValueError("LOCKED_OR_CROSSED_BOOK")
    asks = {"YES": Decimal(1) - bids["no"], "NO": Decimal(1) - bids["yes"]}
    if any(not is_valid_market_price(market, price) for price in asks.values()):
        raise ValueError("INVALID_EXECUTABLE_TICK")
    return asks


def source_originals() -> dict:
    root = Path(__file__).resolve().parents[1]
    names = (
        "crypto.research_shadow",
        "crypto.cf_process_inputs",
        "crypto.cf_settlement_windows",
        "crypto.settlement_average_model",
        "crypto.settlement_target",
        "crypto.settlement_rule_version",
        "paper.fees",
        "kalshi.protocol_math",
        "utils.decimals",
    )
    result = {}
    for name in names:
        module = importlib.import_module("kalshi_predictor." + name)
        if module.__file__ is None:
            raise ValueError("SOURCE_FILE_REQUIRED")
        path = Path(module.__file__).resolve()
        if path != root / (name.replace(".", "/") + ".py"):
            raise ValueError("DISPLACED_SOURCE_MODULE")
        raw = path.read_bytes()
        result[name] = {"sha256": sha(raw), "hex": raw.hex()}
    return result


def build_decision(request_raw: bytes, *, as_of: datetime) -> dict:
    """Recompute CF process and the current average model; never accept supplied p/sigma."""
    from kalshi_predictor.crypto.cf_process_inputs import estimate_cf_process

    if not 0 < len(request_raw) <= LIMIT:
        raise ValueError("BOUNDED_REQUEST_REQUIRED")
    request = _json(request_raw)
    if request.get("schema") != "crypto-average-shadow-request-v1":
        raise ValueError("REQUEST_SCHEMA_REQUIRED")
    target = target_from_request(request)
    target_record = target.validate(as_of=as_of)
    market_raw = original(request["market"])
    ticker = target.rules.market_ticker
    market_receipt = receipt(
        request["market_receipt"],
        market_raw,
        f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}",
        as_of,
    )
    if at(market_receipt["received_at"]) != target.market_received_at:
        raise ValueError("TARGET_RECEIPT_MISMATCH")
    market = _json(market_raw)["market"]
    if market.get("status") not in {"active", "open"}:
        raise ValueError("CURRENT_OPEN_MARKET_REQUIRED")

    def strike(key: str) -> Decimal:
        value = market.get(key)
        if type(value) not in (str, int, Decimal):
            raise ValueError("EXACT_ORIGINAL_STRIKE_REQUIRED")
        result = Decimal(value)
        if not result.is_finite() or result <= 0:
            raise ValueError("EXACT_ORIGINAL_STRIKE_REQUIRED")
        return result

    kind = market.get("strike_type")
    if kind == "greater":
        matched = (
            target.comparator == "ABOVE"
            and target.threshold == strike("floor_strike")
            and market.get("cap_strike") is None
        )
    elif kind == "less":
        matched = (
            target.comparator == "BELOW"
            and target.threshold == strike("cap_strike")
            and market.get("floor_strike") is None
        )
    elif kind == "between":
        matched = (
            target.comparator in {"RANGE", "RANGE_CLOSED"}
            and target.lower == strike("floor_strike")
            and target.upper == strike("cap_strike")
        )
    else:
        matched = False
    if not matched or market.get("custom_strike") not in (None, {}):
        raise ValueError("ORIGINAL_PAYOFF_BINDING_REQUIRED")
    book_raw = original(request["book"])
    book_receipt = receipt(
        request["book_receipt"],
        book_raw,
        f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook?depth=5",
        as_of,
    )
    asks = book_prices(book_raw, market)
    process = estimate_cf_process(
        original(request["cf"]),
        original(request["cf_receipt"]),
        source_sha256=request["cf"]["sha256"],
        receipt_sha256=request["cf_receipt"]["sha256"],
        target=target,
        as_of=as_of,
    )
    forecast = forecast_benchmark_average(target, process.process, as_of=as_of)
    p = Decimal(str(forecast["probability"]))
    rows = []
    for side, price in asks.items():
        fee = None
        supplied = request.get("fee_quotes", {}).get(side)
        if supplied is not None:
            payload = _json(original(supplied))
            matching = [
                _json(bytes.fromhex(c["payload_hex"]))
                for c in payload["evidence"]["captures"]
                if _json(bytes.fromhex(c["payload_hex"])).get("url")
                == f"https://external-api.kalshi.com/trade-api/v2/markets/{ticker}"
            ]
            if len(matching) != 1 or not same(matching[0]["body"], _json(market_raw)):
                raise ValueError("FEE_ORIGINAL_MARKET_MISMATCH")
            quote = verify_fee_quote(
                payload,
                ticker=ticker,
                side="BUY_" + side,
                quantity=1,
                price=price,
                simulator_floor=Decimal(payload["simulator_floor"]),
                now=as_of,
            )
            fee = quote.charge
        probability = p if side == "YES" else 1 - p
        gross = probability - price
        rows.append(
            dict(
                side=side,
                probability=str(probability),
                executable_price=str(price),
                quantity=1,
                gross_ev=str(gross),
                exchange_fee=str(fee) if fee is not None else None,
                exchange_fee_only_ev=str(gross - fee) if fee is not None else None,
                slippage=None,
                uncertainty=None,
                net_ev=None,
                net_ev_status="UNKNOWN_TOTAL_COSTS",
            )
        )
    return dict(
        schema="crypto-average-research-shadow-v1",
        event=target.event_ticker,
        ticker=ticker,
        benchmark=target.rules.index_id,
        decision_at=as_of.isoformat(),
        # Preserve the existing target fingerprint for frozen-outcome replay.
        # New SOL forecasts additionally carry their typed semantic rule binding.
        rule_version=sha(encode(target_record)),
        rule_version_authority="DECLARED_SEMANTICS_UNCERTIFIED",
        settlement_eta=market["close_time"],
        forecast=forecast,
        cf_evidence=process.evidence,
        book_received_at=book_receipt["received_at"],
        rows=rows,
        paper_eligible=False,
        execution_authority=False,
        classification="UNCERTIFIED_RESEARCH",
        request_sha256=sha(request_raw),
    )


def initialize_journal(path: Path) -> None:
    """Explicitly create a NEW dedicated journal; never adopt an application DB."""
    if any(
        p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (path, *path.parents)
    ):
        raise ValueError("LINKED_JOURNAL_REFUSED")
    with path.open("xb"):
        pass
    with closing(sqlite3.connect(path)) as db:
        db.executescript(f"""PRAGMA application_id={APPLICATION_ID};
            CREATE TABLE research_shadow(id TEXT PRIMARY KEY, request_sha TEXT UNIQUE NOT NULL,
                payload BLOB NOT NULL, payload_sha TEXT NOT NULL, recorded_at TEXT NOT NULL);
            CREATE TABLE research_completion(id TEXT PRIMARY KEY, payload BLOB NOT NULL,
                payload_sha TEXT NOT NULL);
            CREATE TRIGGER no_update BEFORE UPDATE ON research_shadow
                BEGIN SELECT RAISE(ABORT,'IMMUTABLE'); END;
            CREATE TRIGGER no_delete BEFORE DELETE ON research_shadow
                BEGIN SELECT RAISE(ABORT,'IMMUTABLE'); END;
            CREATE TRIGGER no_completion_update BEFORE UPDATE ON research_completion
                BEGIN SELECT RAISE(ABORT,'IMMUTABLE'); END;
            CREATE TRIGGER no_completion_delete BEFORE DELETE ON research_completion
                BEGIN SELECT RAISE(ABORT,'IMMUTABLE'); END;""")


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if any(
        p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (path, *path.parents)
    ):
        raise ValueError("LINKED_JOURNAL_REFUSED")
    db = sqlite3.connect(
        path.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True
    )
    if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        db.close()
        raise ValueError("DEDICATED_RESEARCH_JOURNAL_REQUIRED")
    return db


def append_decision(path: Path, request_raw: bytes, *, require_new: bool = False) -> dict:
    """Actual-clock atomic original+forecast append; identical requests are idempotent."""
    if type(require_new) is not bool:
        raise ValueError("EXACT_REQUIRE_NEW_FLAG")
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT payload,payload_sha FROM research_shadow WHERE request_sha=?",
            (sha(request_raw),),
        ).fetchone()
        if old:
            if require_new:
                raise ValueError("RESEARCH_REQUEST_ALREADY_EXISTS")
            if sha(old[0]) != old[1]:
                raise ValueError("CORRUPT_RESEARCH_RECORD")
            return _read_decision(db, old[0], old[1])
        sources = source_originals()
        as_of = now()
        decision = build_decision(request_raw, as_of=as_of)
        recorded = now()
        if not as_of <= recorded <= as_of + timedelta(seconds=60):
            raise ValueError("RESEARCH_APPEND_CLOCK_INVALID")
        target_from_request(_json(request_raw)).validate(as_of=recorded)
        if sources != source_originals():
            raise ValueError("SOURCE_CHANGED_DURING_FORECAST")
        decision["computed_at"] = recorded.isoformat()
        decision["decision_id"] = sha(
            encode(
                dict(request=sha(request_raw), sources={k: v["sha256"] for k, v in sources.items()})
            )
        )
        payload = encode(
            dict(decision=decision, request_hex=request_raw.hex(), source_originals=sources)
        )
        db.execute(
            "INSERT INTO research_shadow VALUES(?,?,?,?,?)",
            (
                decision["decision_id"],
                sha(request_raw),
                payload,
                sha(payload),
                recorded.isoformat(),
            ),
        )
        db.commit()
        durable_at = now()  # Original bytes and prediction are durable BEFORE this clock.
        status = "COMPLETE_RESEARCH"
        try:
            if not recorded <= durable_at <= as_of + timedelta(seconds=60):
                raise ValueError("RESEARCH_APPEND_CLOCK_INVALID")
            target_from_request(_json(request_raw)).validate(as_of=durable_at)
            clocks = [
                decision["book_received_at"],
                decision["forecast"]["level_observed_at"],
                _json(original(_json(request_raw)["market_receipt"]))["received_at"],
            ]
            if any(not 0 <= (durable_at - at(t)).total_seconds() <= 60 for t in clocks):
                raise ValueError("RESEARCH_SOURCE_STALE_AT_COMMIT")
            if sources != source_originals():
                raise ValueError("SOURCE_CHANGED_DURING_COMMIT")
        except ValueError:
            status = "LATE_OR_CHANGED_RESEARCH_ONLY"
        completion = encode(
            dict(
                decision_id=decision["decision_id"],
                payload_sha256=sha(payload),
                original_committed_before=durable_at.isoformat(),
                status=status,
            )
        )
        db.execute(
            "INSERT INTO research_completion VALUES(?,?,?)",
            (decision["decision_id"], completion, sha(completion)),
        )
        db.commit()
        return _read_decision(db, payload, sha(payload))


def _read_decision(db: sqlite3.Connection, raw: bytes, digest: str) -> dict:
    if sha(raw) != digest:
        raise ValueError("CORRUPT_RESEARCH_RECORD")
    decision = json.loads(raw)["decision"]
    result = db.execute(
        "SELECT payload,payload_sha FROM research_completion WHERE id=?", (decision["decision_id"],)
    ).fetchone()
    completion = {"status": "INCOMPLETE_RESEARCH"}
    if result:
        if sha(result[0]) != result[1]:
            raise ValueError("CORRUPT_COMPLETION")
        completion = json.loads(result[0])
        if completion["payload_sha256"] != digest:
            raise ValueError("COMPLETION_BINDING_MISMATCH")
    decision["journal_completion"] = completion
    return decision


def journal_status(path: Path) -> dict:
    """Read-only dashboard adapter. Historical research counts never imply eligibility."""
    rows = []
    with closing(connect(path, readonly=True)) as db:
        for raw, digest in db.execute(
            "SELECT payload,payload_sha FROM research_shadow ORDER BY recorded_at,id"
        ):
            if sha(raw) != digest:
                raise ValueError("CORRUPT_RESEARCH_RECORD")
            rows.append(_read_decision(db, raw, digest))
    return dict(
        schema="crypto-research-shadow-status-v1",
        historical_decisions=len(rows),
        completed_decisions=sum(
            r["journal_completion"]["status"] == "COMPLETE_RESEARCH" for r in rows
        ),
        historical_events=len({r["event"] for r in rows}),
        eligible=0,
        positive_complete_net_ev=0,
        rows=rows,
        execution_authority=False,
    )
