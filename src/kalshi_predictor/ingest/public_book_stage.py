"""Bounded production REST originals staged for the existing guarded writer."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

BASE = "https://external-api.kalshi.com/trade-api/v2"
LIMIT = 1_000_000


def now():
    return datetime.now(UTC)


def encode(value):
    return json.dumps(value, sort_keys=True, allow_nan=False).encode()


def write(path, raw):
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def public_get(url):
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(
        Request(url, headers={"User-Agent": "KalshiReadOnlyResearch/1.0"}), timeout=10
    ) as response:
        return response.read(LIMIT + 1), response.status


def stage_public_books(
    *, tickers, staging_dir: Path, evidence_dir: Path, get=public_get, clock=now
):
    selected = list(dict.fromkeys(tickers))
    if not 1 <= len(selected) <= 6 or any(
        not re.fullmatch(r"[A-Z0-9.-]{1,100}", t) for t in selected
    ):
        raise ValueError("BOUNDED_EXACT_TICKERS_REQUIRED")
    evidence_dir.mkdir(parents=True, exist_ok=False)
    staging_dir.mkdir(parents=True, exist_ok=True)
    write(
        evidence_dir / "reservation.json",
        encode(
            dict(
                recorded_at=clock().isoformat(),
                tickers=selected,
                max_gets=2 * len(selected),
                retry=False,
            )
        ),
    )
    staged, errors, requests = [], [], 0
    for index, ticker in enumerate(selected):
        originals = {}
        try:
            for kind, suffix in (("market", ""), ("book", "/orderbook?depth=10")):
                url = BASE + "/markets/" + ticker + suffix
                requested = clock()
                requests += 1
                raw, status = get(url)
                received = clock()
                if type(raw) is not bytes:
                    raise ValueError("ORIGINAL_BYTES_REQUIRED")
                receipt = dict(
                    url=url,
                    status=status,
                    requested_at=requested.isoformat(),
                    received_at=received.isoformat(),
                    sha256=hashlib.sha256(raw).hexdigest(),
                )
                write(evidence_dir / f"{index}-{kind}.original", raw[: LIMIT + 1])
                write(evidence_dir / f"{index}-{kind}.receipt.json", encode(receipt))
                if (
                    status != 200
                    or len(raw) > LIMIT
                    or not 0 <= (received - requested).total_seconds() <= 15
                ):
                    raise ValueError("PUBLIC_RESPONSE_INVALID")
                originals[kind] = dict(receipt=receipt, payload_hex=raw.hex())
            market = json.loads(bytes.fromhex(originals["market"]["payload_hex"]))["market"]
            book = json.loads(bytes.fromhex(originals["book"]["payload_hex"]))
            timestamp = clock().isoformat()
            payload = dict(
                category="websocket_orderbook_snapshot",
                version="public_rest_originals_v1",
                ticker=ticker,
                source_environment="production",
                rest_base_url=BASE,
                source_kind="PUBLIC_REST",
                staged_at=timestamp,
                market=market,
                orderbook=book,
                originals=originals,
                safety=dict(execution_enabled=False, orders_submitted=0),
            )
            validate_public_stage(payload, as_of=clock())
            path = staging_dir / f"public-rest-{ticker}-{uuid4().hex}.json"
            pending = path.with_suffix(".pending")
            write(pending, encode(payload))
            os.link(pending, path)
            pending.unlink()
            staged.append(str(path))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(dict(ticker=ticker, error=type(exc).__name__ + ":" + str(exc)))
    result = dict(
        status="COMPLETE" if not errors else "COMPLETE_WITH_ERRORS",
        requests=requests,
        staged=staged,
        errors=errors,
        execution_enabled=False,
    )
    write(evidence_dir / "terminal.json", encode(result))
    return result


def validate_public_stage(payload, *, as_of):
    if (
        payload.get("version") != "public_rest_originals_v1"
        or payload.get("rest_base_url") != BASE
        or payload.get("source_environment") != "production"
    ):
        raise ValueError("PUBLIC_STAGE_IDENTITY")
    ticker = payload["ticker"]
    if not re.fullmatch(r"[A-Z0-9.-]{1,100}", ticker):
        raise ValueError("PUBLIC_STAGE_TICKER")
    previous = None
    for kind, suffix in (("market", ""), ("book", "/orderbook?depth=10")):
        original = payload["originals"][kind]
        raw = bytes.fromhex(original["payload_hex"])
        receipt = original["receipt"]
        requested = datetime.fromisoformat(receipt["requested_at"])
        received = datetime.fromisoformat(receipt["received_at"])
        if (
            len(raw) > LIMIT
            or hashlib.sha256(raw).hexdigest() != receipt["sha256"]
            or receipt["status"] != 200
            or receipt["url"] != BASE + "/markets/" + ticker + suffix
        ):
            raise ValueError("PUBLIC_ORIGINAL_BINDING")
        if (
            not requested <= received <= as_of
            or not 0 <= (as_of - requested).total_seconds() <= 60
            or (previous is not None and requested < previous)
        ):
            raise ValueError("PUBLIC_ORIGINAL_CHRONOLOGY")
        previous = received
        decoded = json.loads(raw)
        if (decoded["market"] if kind == "market" else decoded) != payload[
            "market" if kind == "market" else "orderbook"
        ]:
            raise ValueError("PUBLIC_ORIGINAL_CHANGED")
    market = payload["market"]
    if (
        market.get("ticker") != ticker
        or market.get("status") not in ("active", "open")
        or datetime.fromisoformat(market["close_time"].replace("Z", "+00:00")) <= as_of
    ):
        raise ValueError("PUBLIC_MARKET_NOT_ACTIVE")
    if not isinstance(payload["orderbook"].get("orderbook_fp"), dict):
        raise ValueError("PUBLIC_BOOK_SCHEMA")
    if not previous <= datetime.fromisoformat(payload["staged_at"]) <= as_of:
        raise ValueError("PUBLIC_STAGE_CLOCK")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", action="append", required=True)
    parser.add_argument("--staging-dir", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            stage_public_books(
                tickers=args.ticker, staging_dir=args.staging_dir, evidence_dir=args.evidence_dir
            ),
            indent=2,
        )
    )
