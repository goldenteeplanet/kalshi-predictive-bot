"""Bounded original-response market preflight; never invents a forecast or admission.

This scanner resolves current market/book/cost availability. Prepared forecasts
must come from the separately verified prospective producer. Missing forecasts
remain null; this preflight does not certify a full-net scanner or paper readiness.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_predictor.crypto.account_fee_evidence import FeeAuthorityOriginal
from kalshi_predictor.crypto.cost_evidence import OriginalBook, _levels, _unique
from kalshi_predictor.crypto.public_paper_costs import (
    SERIES_HASHES,
    public_paper_fee,
    snapshot_one_contract_impact,
)

FAMILIES = {"SOL": "KXSOLE", "ETH": "KXETH", "BTC": "KXBTC",
            "XRP": "KXXRP", "DOGE": "KXDOGE"}
API = "https://external-api.kalshi.com/trade-api/v2"
DISCOVERY_LIMIT = 2


def discovery_url(series: str) -> str:
    if series not in FAMILIES.values():
        raise ValueError("SCAN_FAMILY_UNSUPPORTED")
    return f"{API}/markets?series_ticker={series}&status=open&limit={DISCOVERY_LIMIT}"


def discovery_rows(original: OriginalBook, *, series: str,
                   assessed_at: datetime) -> tuple[list[dict[str, Any]], bool]:
    """Parse exact bounded discovery, retaining its incomplete-pagination flag."""
    if (assessed_at.utcoffset() is None or original.received_at.utcoffset() is None
            or original.url != discovery_url(series)
            or not 0 <= (assessed_at-original.received_at).total_seconds() <= 300
            or not 0 < len(original.payload) <= 1_000_000):
        raise ValueError("SCAN_DISCOVERY_ORIGINAL_INVALID_OR_STALE")
    body = json.loads(original.payload, object_pairs_hook=_unique)
    rows = body.get("markets")
    if not isinstance(rows, list) or len(rows) > DISCOVERY_LIMIT:
        raise ValueError("SCAN_DISCOVERY_BOUND_EXCEEDED")
    seen = set()
    for row in rows:
        ticker = row.get("ticker", "")
        if (not isinstance(ticker, str) or not ticker.startswith(series+'-')
                or ticker in seen or row.get("status") not in ("open", "active")):
            raise ValueError("SCAN_DISCOVERY_IDENTITY_OR_STATUS_INVALID")
        seen.add(ticker)
    return rows, bool(body.get("cursor"))


def evaluate_current_preflight(
    *, discoveries: dict[str, OriginalBook], books: dict[str, OriginalBook],
    fee_originals: dict[str, tuple[FeeAuthorityOriginal, ...]], assessed_at: datetime,
    acquisition_errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Produce honest stage counts from preserved responses, without DB writes.

    At most one book per family and two discovery rows per family. Counts for
    forecast-dependent stages stay zero until the prospective producer exists;
    they are observed absence, never a claim that the entire market lacks edge.
    """
    if assessed_at.utcoffset() is None or len(books) > len(FAMILIES):
        raise ValueError("SCAN_AWARE_CLOCK_AND_REQUEST_BOUND_REQUIRED")
    families: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    markets_scanned = 0
    book_valid = 0
    horizon_valid_books = 0
    for asset, series in FAMILIES.items():
        family: dict[str, Any] = {
            "asset": asset, "series": series,
            "fee_family_reviewed": series in SERIES_HASHES,
            "discovery_status": "NOT_ACQUIRED", "markets_returned": 0,
            "pagination_incomplete": None, "book_markets_examined": 0,
            "error": (acquisition_errors or {}).get(series),
        }
        families.append(family)
        original = discoveries.get(series)
        if original is None:
            continue
        try:
            markets, incomplete = discovery_rows(original, series=series, assessed_at=assessed_at)
        except (ValueError, TypeError, KeyError) as exc:
            family.update(discovery_status="INVALID_ORIGINAL", error=str(exc))
            continue
        family.update(discovery_status="MARKETS_RETURNED" if markets else "EMPTY_PAGE",
                      markets_returned=len(markets), pagination_incomplete=incomplete,
                      discovery_sha256=original.sha256,
                      discovery_received_at=original.received_at.isoformat())
        markets_scanned += len(markets)
        selected = [m for m in markets if m['ticker'] in books]
        if len(selected) > 1:
            raise ValueError("SCAN_ONE_BOOK_MARKET_PER_FAMILY_LIMIT")
        for market in selected:
            ticker = market['ticker']
            book = books[ticker]
            family['book_markets_examined'] += 1
            try:
                levels = _levels(book, ticker, require_two_sided=False)
            except (ValueError, TypeError, KeyError) as exc:
                family['book_error'] = str(exc)
                continue
            try:
                close = datetime.fromisoformat(
                    market['expiration_time'].replace('Z', '+00:00'))
                horizon = (close-assessed_at).total_seconds() / 3600
                horizon_valid = close.utcoffset() is not None and 0 < horizon <= 72
            except (ValueError, TypeError, KeyError):
                horizon = None
                horizon_valid = False
            any_valid = False
            for side, opposite in [('YES', 'no'), ('NO', 'yes')]:
                row: dict[str, Any] = {
                    "asset": asset, "series": series, "ticker": ticker, "side": side,
                    "forecast_probability": None, "forecast_source": None,
                    "gross_edge": None, "after_fee": None, "after_execution": None,
                    "uncertainty": None, "full_net_ev": None, "paper_eligible": False,
                    "horizon_hours": horizon, "close_time": market.get('close_time'),
                    "expiration_time": market.get('expiration_time'),
                    "expected_expiration_time": market.get('expected_expiration_time'),
                    "rule_status": "NOT_CERTIFIED_BY_SCAN", "calibration_status": "UNVERIFIED",
                    "book_sha256": book.sha256, "book_received_at": book.received_at.isoformat(),
                    "scope": "CURRENT_COST_PREFLIGHT_NO_PREPARED_FORECAST",
                    "first_blocker": "NO_CURRENT_PREPARED_FORECAST",
                    "blockers": ["NO_CURRENT_PREPARED_FORECAST", "UNCERTAINTY_UNKNOWN",
                                 "RULE_NOT_CERTIFIED_BY_SCAN", "CALIBRATION_UNVERIFIED"],
                    "executable_price": None, "fee": None, "snapshot_impact": None,
                }
                candidates.append(row)
                if not horizon_valid:
                    row['blockers'].append('HORIZON_NOT_VERIFIED_WITHIN_72H')
                if not levels[opposite]:
                    row['blockers'].append('NO_EXECUTABLE_ASK')
                    continue
                price = Decimal(1)-levels[opposite][0][0]
                impact = snapshot_one_contract_impact(
                    ticker=ticker, side=side, price=price, originals=(book,),
                    decision_at=assessed_at)
                fee = public_paper_fee(series=series, price=price,
                                       originals=fee_originals.get(series, ()),
                                       assessed_at=assessed_at)
                row.update(executable_price=str(price), fee=fee, snapshot_impact=impact)
                row['blockers'].extend(fee['blockers'] + impact['blockers'])
                any_valid |= impact['value'] is not None
            book_valid += int(any_valid)
            horizon_valid_books += int(any_valid and horizon_valid)
    return {
        "version": "CURRENT_COST_PREFLIGHT_V1", "assessed_at": assessed_at.isoformat(),
        "scope": "BOUNDED_PAGE_SAMPLE_NOT_EXHAUSTIVE_MARKET_SCAN",
        "families": families, "rows": candidates,
        "funnel": {"markets_scanned": markets_scanned, "forecastable": 0,
                   "book_valid_independent_of_forecast": book_valid, "book_valid": 0,
                   "book_valid_and_latest_expiration_within_72h": horizon_valid_books,
                   "gross_positive": 0, "positive_after_fee": 0,
                   "positive_after_snapshot_impact": 0, "uncertainty_known": 0,
                   "full_net_positive": 0, "full_net_gt_5c": 0,
                   "risk_passing": 0, "paper_eligible": 0},
        "first_operational_blocker": "NO_CURRENT_PREPARED_FORECAST",
        "full_net_scanner_operational": False,
        "execution_authority": False, "database_writes": 0, "forecasts_created": 0,
    }
