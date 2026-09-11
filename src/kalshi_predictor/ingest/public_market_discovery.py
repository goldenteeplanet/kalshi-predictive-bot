"""Bounded public catalog discovery independent of WebSocket/ranking state."""

import hashlib
import json
import math
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import zip_longest
from types import SimpleNamespace
from urllib.parse import urlencode

from kalshi_predictor.config import get_settings
from kalshi_predictor.ingest.public_book_stage import (
    BASE,
    LIMIT,
    encode,
    now,
    public_get,
    stage_public_books,
    write,
)
from kalshi_predictor.opportunities.window_eligibility import current_market_window_status


def eligible_tickers(markets, series, *, as_of, settings=None, excluded_windows=None):
    """Prioritize near-midpoint quotes inside the earliest upcoming close window."""
    settings = settings if settings is not None else get_settings()
    ranked = []
    for market in markets:
        try:
            ticker = market["ticker"]
            close = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00"))
            if (
                not re.fullmatch(r"[A-Z0-9.-]{1,100}", ticker)
                or not ticker.startswith(series + "-")
                or market.get("series_ticker", series) != series
                or market["status"] not in ("open", "active")
                or not as_of < close <= as_of + timedelta(hours=72)
            ):
                continue
            window = current_market_window_status(
                SimpleNamespace(
                    **{
                        key: market.get(key)
                        for key in (
                            "ticker",
                            "status",
                            "close_time",
                            "expected_expiration_time",
                            "expiration_time",
                            "settlement_ts",
                            "result",
                        )
                    }
                ),
                settings=settings,
                now=as_of,
            )
            if not window["current_window_eligible"]:
                if excluded_windows is not None:
                    excluded_windows.append(dict(ticker=ticker, **window))
                continue
            distance = Decimal(2)
            bid = Decimal(str(market.get("yes_bid_dollars")))
            ask = Decimal(str(market.get("yes_ask_dollars")))
            if bid.is_finite() and ask.is_finite() and 0 <= bid <= ask <= 1:
                distance = abs((bid + ask) / 2 - Decimal("0.5"))
        except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
            continue
        ranked.append((close, distance, ticker))
    return list(dict.fromkeys(ticker for _, _, ticker in sorted(ranked)))


def discover_and_stage(
    *, series, staging_dir, evidence_dir, get=public_get, clock=now, settings=None
):
    families = list(dict.fromkeys(series))
    if not 1 <= len(families) <= 6 or any(
        not isinstance(s, str) or not re.fullmatch(r"[A-Z0-9]{1,40}", s) for s in families
    ):
        raise ValueError("BOUNDED_SERIES_REQUIRED")
    settings = settings if settings is not None else get_settings()
    evidence_dir.mkdir(parents=True, exist_ok=False)
    write(
        evidence_dir / "reservation.json",
        encode(
            dict(
                at=clock().isoformat(),
                series=families,
                max_gets=len(families) + 12,
                max_catalog_rows=100 * len(families),
                max_selected=6,
                retry=False,
                minimum_minutes_to_close=str(settings.opportunity_min_time_to_close_minutes),
            )
        ),
    )
    requests, pools, catalogs, errors = 0, [], [], []
    for index, family in enumerate(families):
        requested = clock()
        # Kalshi permits close-time bounds with status omitted, not status=open.
        # Still enforce lifecycle and the exact cutoff locally after receipt.
        cutoff = requested + timedelta(
            minutes=max(0, float(settings.opportunity_min_time_to_close_minutes))
        )
        url = (
            BASE
            + "/markets?"
            + urlencode(
                dict(
                    series_ticker=family,
                    limit=100,
                    min_close_ts=math.floor(cutoff.timestamp()),
                    max_close_ts=math.ceil((requested + timedelta(hours=72)).timestamp()),
                )
            )
        )
        try:
            requests += 1
            raw, status = get(url)
            received = clock()
            write(evidence_dir / f"catalog-{index}.original", raw)
            write(
                evidence_dir / f"catalog-{index}.receipt.json",
                encode(
                    dict(
                        url=url,
                        status=status,
                        requested_at=requested.isoformat(),
                        received_at=received.isoformat(),
                        sha256=hashlib.sha256(raw).hexdigest(),
                    )
                ),
            )
            if (
                status != 200
                or len(raw) > LIMIT
                or not 0 <= (received - requested).total_seconds() <= 15
            ):
                raise ValueError("CATALOG_RESPONSE_INVALID")
            catalog = json.loads(raw)
            markets = catalog["markets"]
            if not isinstance(markets, list) or len(markets) > 100:
                raise ValueError("CATALOG_ROW_CAP")
            excluded_windows = []
            pools.append(
                eligible_tickers(
                    markets,
                    family,
                    as_of=received,
                    settings=settings,
                    excluded_windows=excluded_windows,
                )
            )
            catalogs.append(
                dict(
                    series=family,
                    rows=len(markets),
                    partial=bool(catalog.get("cursor")),
                    selection_as_of=received.isoformat(),
                    excluded_windows=excluded_windows,
                )
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(dict(series=family, error=type(exc).__name__))
    selected = list(dict.fromkeys(t for group in zip_longest(*pools) for t in group if t))[:6]
    write(evidence_dir / "selection.json", encode(dict(tickers=selected, catalogs=catalogs)))
    books = (
        stage_public_books(
            tickers=selected,
            staging_dir=staging_dir,
            evidence_dir=evidence_dir / "books",
            get=get,
            clock=clock,
        )
        if selected
        else dict(status="NO_CURRENT_MARKETS", requests=0, staged=[], errors=[])
    )
    result = dict(
        status="COMPLETE_WITH_ERRORS" if errors or books["errors"] else "COMPLETE",
        requests=requests + books["requests"],
        discovery_errors=errors,
        books=books,
        selected=selected,
        catalogs=catalogs,
        execution_enabled=False,
    )
    write(evidence_dir / "terminal.json", encode(result))
    return result
