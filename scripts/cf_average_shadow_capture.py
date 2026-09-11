"""Six-GET SOL CF research capture. No paper/order API or certification authority."""

from __future__ import annotations

import argparse
import base64
import os
import re
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from kalshi_predictor.crypto import research_shadow as S
from kalshi_predictor.crypto.cf_process_inputs import decode_cf_original
from kalshi_predictor.crypto.settlement_target import _json

TERMS_URL = "https://assets.kalshi.com/contract_terms/CRYPTO.pdf"
TERMS_SHA = "fde90b9c0825df277b0b2b2be6239af221eafd01a624c1b2d3a9eaff2d6fe75c"
BASE = "https://external-api.kalshi.com/trade-api/v2"
LIMIT = 3_000_000


def now():
    return datetime.now(UTC)


def persist(path: Path, raw: bytes):
    with path.open("xb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())


def artifact(raw):
    return {"hex": raw.hex(), "sha256": S.sha(raw)}


def event_for(target):
    return "KXSOLE-" + target.astimezone(ZoneInfo("America/New_York")).strftime("%y%b%d%H").upper()


def terms(raw, receipt_raw, as_of):
    r = _json(receipt_raw)
    if (
        set(r) != {"url", "requested_at", "received_at", "http_status", "sha256"}
        or r["url"] != TERMS_URL
        or type(r["http_status"]) is not int
        or r["http_status"] != 200
        or r["sha256"] != S.sha(raw)
        or S.sha(raw) != TERMS_SHA
    ):
        raise ValueError("REVIEWED_TERMS_ORIGINAL_REQUIRED")
    if not S.at(r["requested_at"]) <= S.at(r["received_at"]) <= as_of:
        raise ValueError("TERMS_NOT_VISIBLE")
    return r


def bounds(row, event, target):
    if (
        type(row) is not dict
        or row.get("event_ticker") != event
        or type(row.get("ticker")) is not str
        or not re.fullmatch(r"[A-Z0-9.-]{1,128}", row["ticker"])
        or not row["ticker"].startswith(event + "-")
        or row.get("market_type") != "binary"
        or row.get("status") not in {"active", "open"}
        or S.at(row.get("close_time", "")) != target
    ):
        raise ValueError("EXACT_FUTURE_EVENT_MARKET_REQUIRED")
    if row.get("strike_type") != "between":
        return None
    if row.get("custom_strike") not in (None, {}):
        raise ValueError("UNSUPPORTED_CUSTOM_RANGE")
    values = []
    for k in ("floor_strike", "cap_strike"):
        if type(row.get(k)) not in (str, int, Decimal):
            raise ValueError("FINITE_RANGE_REQUIRED")
        x = Decimal(str(row[k]))
        exponent = x.as_tuple().exponent
        if (
            not x.is_finite()
            or x <= 0
            or len(x.as_tuple().digits) > 40
            or not isinstance(exponent, int)
            or abs(exponent) > 40
        ):
            raise ValueError("FINITE_RANGE_REQUIRED")
        values.append(x)
    if values[0] >= values[1]:
        raise ValueError("ORDERED_RANGE_REQUIRED")
    return tuple(values)


def source_proof():
    proof = S.source_originals()
    path = Path(__file__).resolve()
    proof["capture_script"] = artifact(path.read_bytes())
    return proof


def capture(
    output: Path,
    target: datetime,
    rule_raw: bytes,
    rule_receipt_raw: bytes,
    transport,
    *,
    protocol_raw: bytes,
    clock=now,
):
    """Transport is one GET(url, timeout)->(status, original bytes); never retries."""
    if target.tzinfo is None or target.minute or target.second or target.microsecond:
        raise ValueError("AWARE_EXACT_FUTURE_HOUR_REQUIRED")
    plan = _json(protocol_raw)
    expected = {
        "schema",
        "target_at",
        "event_ticker",
        "not_before",
        "not_after",
        "max_gets",
        "symbol",
        "benchmark",
        "selection",
        "hypotheses",
        "rounding",
        "decimal_places",
        "rule_authority",
        "net_costs",
        "source_sha256",
        "terms_sha256",
    }
    fixed = dict(
        schema="cf-average-prospective-slot-v1",
        symbol="SOL",
        benchmark="SOLUSD_RTI",
        selection="nearest_two_range_midpoints_then_ticker",
        max_gets=6,
        hypotheses=[
            dict(name="LEFT_CLOSED_RIGHT_OPEN", include_start=True, include_end=False),
            dict(name="LEFT_OPEN_RIGHT_CLOSED", include_start=False, include_end=True),
        ],
        rounding="HALF_EVEN",
        decimal_places=4,
        rule_authority="DECLARED_UNCERTIFIED",
        net_costs="UNKNOWN",
    )
    if (
        set(plan) != expected
        or not S.same({k: plan[k] for k in fixed}, fixed)
        or S.at(plan["target_at"]) != target
        or plan["event_ticker"] != event_for(target)
        or plan["terms_sha256"] != TERMS_SHA
        or not S.same(plan["source_sha256"], {k: v["sha256"] for k, v in source_proof().items()})
    ):
        raise ValueError("EXACT_PREDECLARED_PLAN_REQUIRED")
    not_before, not_after = S.at(plan["not_before"]), S.at(plan["not_after"])
    if not timedelta(0) < not_after - not_before <= timedelta(seconds=50):
        raise ValueError("BOUNDED_PLAN_WINDOW_REQUIRED")
    started = clock()
    if not not_before <= started < not_after < target - timedelta(minutes=1):
        raise ValueError("FUTURE_WINDOW_REQUIRED")
    if any(
        p.is_symlink() or getattr(p, "is_junction", lambda: False)()
        for p in (output, *output.parents)
    ):
        raise ValueError("LINKED_OUTPUT_REFUSED")
    output.mkdir(exist_ok=False)
    event = event_for(target)
    deadline = min(started + timedelta(seconds=50), not_after, target - timedelta(minutes=1))
    manifests = {}
    requests = 0
    last_checked = started

    def save(name, raw):
        persist(output / name, raw)
        manifests[name] = S.sha(raw)

    def check():
        nonlocal last_checked
        current = clock()
        if current < last_checked:
            raise ValueError("CAPTURE_CLOCK_REVERSED")
        last_checked = current
        if not started <= current < deadline:
            raise ValueError("CAPTURE_DEADLINE")
        return current

    try:
        r = terms(rule_raw, rule_receipt_raw, check())
        proof = source_proof()
        save("plan.original.json", protocol_raw)
        save("terms.original", rule_raw)
        save("terms.receipt.json", rule_receipt_raw)
        save("source.originals.json", S.encode(proof))
        save(
            "protocol.json",
            S.encode(
                dict(
                    schema="cf-average-capture-v1",
                    event=event,
                    target=target.isoformat(),
                    started_at=started.isoformat(),
                    deadline=deadline.isoformat(),
                    maximum_gets=6,
                    retries=0,
                    selection="TWO_NEAREST_DECIMAL_RANGE_MIDPOINT_THEN_TICKER_BEFORE_FORECAST",
                    hypotheses=["LEFT_CLOSED_RIGHT_OPEN", "LEFT_OPEN_RIGHT_CLOSED"],
                    comparator="RANGE_CLOSED",
                    decimal_places=4,
                    rounding="HALF_EVEN",
                    rule_authority="UNCERTIFIED_DIAGNOSTICS",
                    execution_authority=False,
                    fees=None,
                )
            ),
        )
        save(
            "protocol.recorded.json",
            S.encode(dict(recorded_at=check().isoformat(), sha256=manifests["protocol.json"])),
        )

        def get(label, url, cf=False):
            nonlocal requests
            reserved_at = check()
            if requests >= 6:
                raise ValueError("GET_BUDGET")
            save(
                label + ".reservation.json",
                S.encode(
                    dict(
                        method="GET",
                        url=url,
                        attempt=requests + 1,
                        reserved_at=reserved_at.isoformat(),
                        status="RESERVED_BEFORE_TRANSPORT_SEND_UNCONFIRMED",
                    )
                ),
            )
            requested = check()  # The reservation is durable before this send-attempt clock.
            requests += 1
            status, raw = transport(url, min(10.0, (deadline - requested).total_seconds()))
            received = clock()
            if type(raw) is not bytes or not 0 < len(raw) <= LIMIT:
                raise ValueError("BOUNDED_ORIGINAL_REQUIRED")
            save(label + ".original", raw)
            recorded = clock()
            rec = dict(
                method="GET",
                url=url,
                http_status=status,
                source_sha256=S.sha(raw),
                requested_at=requested.isoformat(),
                received_at=received.isoformat(),
            )
            if cf:
                rec.update(
                    schema="cf-response-receipt-v1",
                    index_id="SOLUSD_RTI",
                    profile="LATEST_1HZ",
                    recorded_at=recorded.isoformat(),
                )
            else:
                rec.update(original_complete=True, original_recorded_at=recorded.isoformat())
            rr = S.encode(rec)
            save(label + ".receipt.json", rr)
            receipt_recorded = check()
            save(
                label + ".receipt-recorded.json",
                S.encode(
                    dict(
                        receipt_sha256=S.sha(rr),
                        recorded_after_receipt=receipt_recorded.isoformat(),
                    )
                ),
            )
            if not reserved_at <= requested <= received <= recorded <= receipt_recorded <= check():
                raise ValueError("REQUEST_RECEIPT_CLOCK_REVERSED")
            if type(status) is not int or status != 200:
                raise ValueError("HTTP_ORIGINAL_UNAVAILABLE")
            return raw, rr, rec

        catalog_raw, catalog_rr, _ = get(
            "catalog", BASE + f"/markets?event_ticker={event}&limit=1000"
        )
        catalog = _json(catalog_raw)
        if (
            set(catalog) != {"markets", "cursor"}
            or catalog["cursor"] != ""
            or type(catalog["markets"]) is not list
            or not 2 <= len(catalog["markets"]) <= 1000
        ):
            raise ValueError("COMPLETE_EVENT_CATALOG_REQUIRED")
        rows = catalog["markets"]
        parsed = [(row, bounds(row, event, target)) for row in rows]
        if len({row["ticker"] for row in rows}) != len(rows):
            raise ValueError("DUPLICATE_MARKET")
        cf_raw, cf_rr, cf_rec = get("cf", BASE + "/cfbenchmarks/values?id=SOLUSD_RTI", True)
        decoded = decode_cf_original(
            cf_raw,
            sha256=S.sha(cf_raw),
            request_url=cf_rec["url"],
            index_id="SOLUSD_RTI",
            profile="LATEST_1HZ",
        )
        observed = datetime.fromtimestamp(decoded.timestamps_ms[-1] / 1000, UTC)
        if decoded.server_time > S.at(cf_rec["received_at"]) or not timedelta(
            0
        ) <= check() - observed <= timedelta(seconds=60):
            raise ValueError("CF_SELECTION_INPUT_FUTURE_OR_STALE")
        level = decoded.values[-1]
        choices = [
            (
                abs((Fraction(pair[0]) + Fraction(pair[1])) / 2 - Fraction(level)),
                row["ticker"],
                row,
                pair,
            )
            for row, pair in parsed
            if pair is not None
        ]
        choices.sort(key=lambda x: (x[0], x[1]))
        if len(choices) < 2:
            raise ValueError("TWO_FINITE_RANGES_REQUIRED")
        selected = choices[:2]
        save(
            "selection.json",
            S.encode(
                dict(
                    event=event,
                    selected=[x[1] for x in selected],
                    level=str(level),
                    cf_sha256=S.sha(cf_raw),
                    catalog_sha256=S.sha(catalog_raw),
                    selected_before_forecasting_at=check().isoformat(),
                )
            ),
        )
        acquisitions = []
        errors = []
        for i, (_, ticker, _row, _pair) in enumerate(selected):
            result = {}
            for key, url in (
                ("market", BASE + f"/markets/{ticker}"),
                ("book", BASE + f"/markets/{ticker}/orderbook?depth=5"),
            ):
                try:
                    result[key] = get(f"{i}-{key}", url)
                except (ValueError, OSError) as exc:
                    errors.append(
                        dict(ticker=ticker, stage=key, error=type(exc).__name__, reason=str(exc))
                    )
            acquisitions.append(result)
        if errors:
            save("acquisition.failures.json", S.encode(errors))
            raise ValueError("SELECTED_ORIGINAL_UNAVAILABLE_NO_FALLBACK")
        queued = []
        for i, (_, ticker, selected_row, pair) in enumerate(selected):
            market_raw, market_rr, mr = acquisitions[i]["market"]
            book_raw, book_rr, _ = acquisitions[i]["book"]
            row = _json(market_raw)["market"]
            actual = bounds(row, event, target)
            semantic_keys = (
                "ticker",
                "event_ticker",
                "market_type",
                "close_time",
                "strike_type",
                "floor_strike",
                "cap_strike",
                "custom_strike",
                "rules_primary",
                "rules_secondary",
                "price_level_structure",
                "price_ranges",
            )
            if actual != pair or not S.same(
                {k: row.get(k) for k in semantic_keys},
                {k: selected_row.get(k) for k in semantic_keys},
            ):
                raise ValueError("SELECTED_MARKET_SEMANTICS_CHANGED")
            for name, left, right in (
                ("LEFT_CLOSED_RIGHT_OPEN", True, False),
                ("LEFT_OPEN_RIGHT_CLOSED", False, True),
            ):
                request = dict(
                    schema="crypto-average-shadow-request-v1",
                    target=dict(
                        symbol="SOL",
                        event_ticker=event,
                        rules=dict(
                            market_ticker=ticker,
                            index_id="SOLUSD_RTI",
                            closing=dict(
                                start_ms=int(target.timestamp() * 1000) - 60000,
                                end_ms=int(target.timestamp() * 1000),
                                include_start=left,
                                include_end=right,
                                cadence_ms=1000,
                                expected_ticks=60,
                            ),
                            opening=None,
                            decimal_places=4,
                            rounding="HALF_EVEN",
                            amendments="REJECT",
                            rule_source=TERMS_URL,
                            rule_sha256=S.sha(rule_raw),
                        ),
                        comparator="RANGE_CLOSED",
                        threshold=None,
                        lower=str(pair[0]),
                        upper=str(pair[1]),
                        rule_received_at=r["received_at"],
                        market_received_at=mr["received_at"],
                        finality_deadline=None,
                        finality_basis="UNRESOLVED",
                    ),
                    rule=artifact(rule_raw),
                    market=artifact(market_raw),
                    market_receipt=artifact(market_rr),
                    book=artifact(book_raw),
                    book_receipt=artifact(book_rr),
                    cf=artifact(cf_raw),
                    cf_receipt=artifact(cf_rr),
                    capture_protocol_sha256=manifests["protocol.json"],
                    selection_sha256=manifests["selection.json"],
                    hypothesis=name,
                )
                raw = S.encode(request)
                filename = f"request-{i}-{name}.json"
                save(filename, raw)
                queued.append((filename, raw))
        save(
            "requests.recorded.json",
            S.encode(
                dict(recorded_at=check().isoformat(), requests={k: S.sha(v) for k, v in queued})
            ),
        )
        if source_proof() != proof or any(
            S.sha((output / k).read_bytes()) != v for k, v in manifests.items()
        ):
            raise ValueError("FROZEN_SOURCE_OR_ORIGINAL_CHANGED")
        S.initialize_journal(output / "research.db")
        results = []
        for filename, raw in queued:
            check()
            result = S.append_decision(output / "research.db", raw)
            if result["journal_completion"]["status"] != "COMPLETE_RESEARCH":
                raise ValueError("SHADOW_COMPLETION_UNAVAILABLE")
            results.append(dict(request=filename, decision=result))
        check()
        if source_proof() != proof:
            raise ValueError("SOURCE_CHANGED")
        with closing(S.connect(output / "research.db", readonly=True)) as db:
            pinned = {
                row[0]: row[1:]
                for row in db.execute(
                    "SELECT r.id,r.payload_sha,c.payload_sha FROM research_shadow r "
                    "JOIN research_completion c ON r.id=c.id"
                )
            }
        pin_rows = []
        for item in results:
            d = item["decision"]
            payload_hash, completion_hash = pinned[d["decision_id"]]
            request = _json((output / item["request"]).read_bytes())
            pin_rows.append(
                dict(
                    decision_id=d["decision_id"],
                    payload_sha256=payload_hash,
                    completion_sha256=completion_hash,
                    ticker=d["ticker"],
                    hypothesis=request["hypothesis"],
                    request_sha256=d["request_sha256"],
                    rule_version=d["rule_version"],
                )
            )
        save(
            "shadow-pins.json",
            S.encode(
                dict(
                    schema="cf-shadow-pins-v1",
                    event=event,
                    target=target.isoformat(),
                    decisions=pin_rows,
                )
            ),
        )
        save(
            "shadow-pins.recorded.json",
            S.encode(dict(recorded_at=check().isoformat(), sha256=manifests["shadow-pins.json"])),
        )
        save(
            "result.json",
            S.encode(
                dict(
                    status="RESEARCH_COMPLETE",
                    event=event,
                    event_count=1,
                    contracts=2,
                    hypotheses_per_contract=2,
                    requests=requests,
                    decisions=results,
                    execution_authority=False,
                )
            ),
        )
        completed = check()
        save(
            "completion.json",
            S.encode(
                dict(
                    status="COMPLETE",
                    recorded_after_result=completed.isoformat(),
                    result_sha256=manifests["result.json"],
                    files=manifests,
                )
            ),
        )
        check()
        return _json((output / "result.json").read_bytes())
    except Exception as exc:
        if not (output / "failure.json").exists():
            persist(
                output / "failure.json",
                S.encode(
                    dict(
                        status="FAILED_RESEARCH_ONLY",
                        at=clock().isoformat(),
                        requests=requests,
                        error=type(exc).__name__,
                        reason=str(exc),
                    )
                ),
            )
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--terms", type=Path, required=True)
    parser.add_argument("--terms-receipt", type=Path, required=True)
    args = parser.parse_args()
    # Credentials are used only to sign this fixed GET surface, never archived.
    import httpx
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key_id = os.environ["KALSHI_API_KEY_ID"]
    private = serialization.load_pem_private_key(
        Path(os.environ["KALSHI_PRIVATE_KEY_PATH"]).read_bytes(), password=None
    )

    if not isinstance(private, rsa.RSAPrivateKey):
        raise ValueError("RSA_SIGNING_KEY_REQUIRED")

    def transport(url, timeout):
        if not url.startswith(BASE + "/"):
            raise ValueError("FIXED_AUTHORITY_REQUIRED")
        stamp = str(int(time.time() * 1000))
        sig = private.sign(
            (stamp + "GET" + urlsplit(url).path).encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        headers = {
            "KALSHI-ACCESS-KEY": key_id,
            "KALSHI-ACCESS-TIMESTAMP": stamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }
        expires = time.monotonic() + timeout
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", url, headers=headers) as response:
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    if time.monotonic() > expires:
                        raise ValueError("RESPONSE_DEADLINE")
                    size += len(chunk)
                    if size > LIMIT:
                        raise ValueError("RESPONSE_LIMIT")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                if key_id.encode() in raw or headers["KALSHI-ACCESS-SIGNATURE"].encode() in raw:
                    raise ValueError("CREDENTIAL_ECHO_REFUSED")
                return response.status_code, raw

    capture(
        args.output,
        S.at(args.target),
        args.terms.read_bytes(),
        args.terms_receipt.read_bytes(),
        transport,
        protocol_raw=args.protocol.read_bytes(),
    )


if __name__ == "__main__":
    main()
