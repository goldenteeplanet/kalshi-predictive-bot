"""Bounded original-response market preflight; never invents a forecast or admission.

This scanner resolves current market/book/cost availability. Prepared forecasts
must come from the separately verified prospective producer. Missing forecasts
remain null; this preflight does not certify a full-net scanner or paper readiness.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from kalshi_predictor.crypto.account_fee_evidence import FeeAuthorityOriginal
from kalshi_predictor.crypto.candidate_uncertainty import verify_candidate_uncertainty
from kalshi_predictor.crypto.cost_evidence import OriginalBook, _levels, _unique
from kalshi_predictor.crypto.current_calibration_evidence import (
    CurrentCalibrationOriginals,
    assess_current_conditional_calibration,
)
from kalshi_predictor.crypto.current_event_selection import (
    CurrentEventSelectionInputs,
    selected_event_rows,
)
from kalshi_predictor.crypto.current_research_intake import prepare_current_research_forecast
from kalshi_predictor.crypto.public_paper_costs import (
    SERIES_HASHES,
    public_paper_fee,
    snapshot_one_contract_impact,
)
from kalshi_predictor.crypto.settlement_target import SettlementBenchmarkTarget
from kalshi_predictor.crypto.uncertainty_contract import binary_support_net_bounds

FAMILIES = {"SOL": "KXSOLE", "ETH": "KXETH", "BTC": "KXBTC",
            "XRP": "KXXRP", "DOGE": "KXDOGE"}
API = "https://external-api.kalshi.com/trade-api/v2"
DISCOVERY_LIMIT = 2
PAGE_LIMIT = 20
MAX_PAGES_PER_FAMILY = 3
MAX_SCAN_BOOKS = 10


@dataclass(frozen=True)
class CurrentResearchInputs:
    """Original intake inputs, not a caller-authored forecast or readiness verdict."""

    target: SettlementBenchmarkTarget
    cf_original: bytes
    cf_receipt: bytes
    protocol_original: bytes
    protocol_sha256: str


def discovery_page_url(series: str, *, cursor: str = '', limit: int = PAGE_LIMIT) -> str:
    if (series not in FAMILIES.values() or type(limit) is not int
            or not 1 <= limit <= PAGE_LIMIT or not isinstance(cursor, str)
            or len(cursor) > 4096):
        raise ValueError('SCAN_PAGINATION_INPUT_INVALID')
    query: dict[str, str | int] = dict(series_ticker=series, status='open', limit=limit)
    if cursor:
        query['cursor'] = cursor
    return API+'/markets?'+urlencode(query)


def paginated_discovery_rows(
    originals: tuple[OriginalBook, ...], *, series: str, assessed_at: datetime,
    limit: int = PAGE_LIMIT,
) -> dict[str, Any]:
    """Replay an exact cursor chain, retaining duplicate sightings without extra counts."""
    if (assessed_at.utcoffset() is None
            or not 1 <= len(originals) <= MAX_PAGES_PER_FAMILY):
        raise ValueError('SCAN_PAGE_COUNT_OR_CLOCK_INVALID')
    cursor = ''
    seen_cursors = set()
    rows: dict[str, dict[str, Any]] = {}
    pages = []
    previous = None
    for index, original in enumerate(originals):
        if (original.url != discovery_page_url(series, cursor=cursor, limit=limit)
                or original.received_at.utcoffset() is None
                or not 0 <= (assessed_at-original.received_at).total_seconds() <= 300
                or (previous is not None and original.received_at < previous)
                or not 0 < len(original.payload) <= 1_000_000):
            raise ValueError('SCAN_PAGE_ORIGINAL_CHAIN_OR_FRESHNESS_INVALID')
        previous = original.received_at
        body = json.loads(original.payload, object_pairs_hook=_unique)
        markets = body.get('markets') if isinstance(body, dict) else None
        if not isinstance(markets, list) or len(markets) > limit:
            raise ValueError('SCAN_PAGE_RESPONSE_BOUND_EXCEEDED')
        next_cursor = body.get('cursor', '')
        if not isinstance(next_cursor, str) or len(next_cursor) > 4096:
            raise ValueError('SCAN_CURSOR_INVALID')
        if next_cursor and (next_cursor == cursor or next_cursor in seen_cursors):
            raise ValueError('SCAN_CURSOR_LOOP')
        if next_cursor:
            seen_cursors.add(next_cursor)
        source = {'url': original.url, 'sha256': original.sha256,
                  'received_at': original.received_at.isoformat(), 'page_index': index}
        pages.append(source | {'rows_returned': len(markets), 'has_next_page': bool(next_cursor)})
        for market in markets:
            if not isinstance(market, dict):
                raise ValueError('SCAN_MARKET_OBJECT_REQUIRED')
            ticker = market.get('ticker', '')
            if (not isinstance(ticker, str) or not ticker.startswith(series+'-')
                    or market.get('status') not in ('active', 'open')):
                raise ValueError('SCAN_MARKET_IDENTITY_OR_STATUS_INVALID')
            if ticker in rows:
                rows[ticker]['sources'].append(source)
                rows[ticker]['conflicting_sightings'] |= rows[ticker]['market'] != market
            else:
                rows[ticker] = {'market': market, 'sources': [source],
                                'conflicting_sightings': False}
        cursor = next_cursor
        if not cursor and index != len(originals)-1:
            raise ValueError('SCAN_PAGE_AFTER_TERMINAL_CURSOR')
    return {'rows': list(rows.values()), 'pages': pages, 'next_cursor': cursor,
            'pagination_incomplete': bool(cursor),
            'stop_reason': ('PAGE_BUDGET_REACHED' if len(originals) == MAX_PAGES_PER_FAMILY
                            else 'CALLER_STOPPED_EARLY') if cursor else 'TERMINAL_CURSOR',
            'raw_rows_returned': sum(page['rows_returned'] for page in pages),
            'unique_markets': len(rows)}


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


def _hours_until(raw: Any, now: datetime) -> float | None:
    try:
        value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if value.utcoffset() is None:
            return None
        return (value-now).total_seconds()/3600
    except (ValueError, TypeError, AttributeError):
        return None


def evaluate_paginated_current_research(
    *, discovery_pages: dict[str, tuple[OriginalBook, ...]], books: dict[str, OriginalBook],
    fee_originals: dict[str, tuple[FeeAuthorityOriginal, ...]], assessed_at: datetime,
    research_inputs: dict[str, CurrentResearchInputs] | None = None,
    acquisition_errors: dict[str, str] | None = None, limit: int = PAGE_LIMIT,
    calibration_originals: dict[str, CurrentCalibrationOriginals] | None = None,
    event_selections: dict[str, CurrentEventSelectionInputs] | None = None,
) -> dict[str, Any]:
    """Current research diagnostics with original-recomputed forecasts and support bounds.

    Counters count unique markets, not both sides as independent opportunities.
    Partial discovery is explicit. Every observed market gets two side rows even
    when no book or forecast exists. Research bounds never become calibrated EV
    or guarded-paper eligibility. This function does not fetch, write or trade.
    """
    inputs = research_inputs or {}
    calibration = calibration_originals or {}
    selections = event_selections or {}
    if (len(calibration) > MAX_SCAN_BOOKS
            or any(type(value) is not CurrentCalibrationOriginals for value in calibration.values())
            or sum(value.byte_count() for value in calibration.values()) > 24_000_000):
        raise ValueError('SCAN_CALIBRATION_ORIGINAL_BOUND_INVALID')
    if (assessed_at.utcoffset() is None or len(books) > MAX_SCAN_BOOKS
            or len(inputs) > MAX_SCAN_BOOKS
            or set(discovery_pages)-set(FAMILIES.values())
            or set(selections)-set(FAMILIES.values()) or set(selections) & set(discovery_pages)):
        raise ValueError('SCAN_RESEARCH_COUNT_OR_SCOPE_INVALID')
    families = []
    rows: list[dict[str, Any]] = []
    stages: dict[str, set[str]] = {name: set() for name in (
        'markets_scanned', 'observation_horizon_eligible', 'forecastable',
        'book_valid_independent_of_forecast', 'book_valid',
        'gross_positive', 'positive_after_fee', 'positive_after_snapshot_impact',
        'conservative_bounds_known', 'uncertainty_known', 'full_net_positive', 'full_net_gt_5c',
        'conditional_uncertainty_supported',
        'risk_passing', 'paper_eligible',
    )}
    first_blocker_counts: dict[str, int] = {}
    recognized = set()
    for asset, series in FAMILIES.items():
        originals = discovery_pages.get(series, ())
        family: dict[str, Any] = {
            'asset': asset, 'series': series, 'status': 'NOT_ACQUIRED', 'pages': [],
            'pagination_incomplete': None, 'fee_family_reviewed': series in SERIES_HASHES,
            'error': (acquisition_errors or {}).get(series),
        }
        families.append(family)
        if not originals and series not in selections:
            continue
        try:
            discovery = (
                selected_event_rows(selections[series], asset=asset, assessed_at=assessed_at)
                if series in selections else paginated_discovery_rows(
                    originals, series=series, assessed_at=assessed_at, limit=limit))
        except (ValueError, KeyError, TypeError) as exc:
            family.update(status='INVALID_DISCOVERY', error=str(exc))
            continue
        family.update({key: value for key, value in discovery.items() if key != 'rows'})
        family['status'] = 'MARKETS_RETURNED' if discovery['rows'] else 'EMPTY_COMPLETE_PAGE'
        if not discovery['rows'] and discovery['pagination_incomplete']:
            family['status'] = 'EMPTY_PARTIAL_PAGE'
        for sighting in discovery['rows']:
            if len(rows) >= 600:
                raise ValueError('SCAN_TOTAL_ASSESSMENT_ROW_BOUND')
            market = sighting['market']
            ticker = market['ticker']
            recognized.add(ticker)
            stages['markets_scanned'].add(ticker)
            common = []
            close_hours = _hours_until(market.get('close_time'), assessed_at)
            if close_hours is None or not 0 < close_hours <= 72:
                common.append('OBSERVATION_CLOSE_NOT_WITHIN_72H')
            else:
                stages['observation_horizon_eligible'].add(ticker)
            if sighting['conflicting_sightings']:
                common.append('CONFLICTING_DISCOVERY_SIGHTINGS')
            prepared = None
            intake = inputs.get(ticker)
            if intake is None:
                common.append('NO_CURRENT_PREPARED_FORECAST')
            else:
                try:
                    if type(intake) is not CurrentResearchInputs:
                        raise ValueError('SCAN_EXACT_ORIGINAL_INTAKE_REQUIRED')
                    if series in selections:
                        selected_at = selections[series].selected_at
                        protocol = json.loads(intake.protocol_original)
                        receipt = json.loads(intake.cf_receipt)
                        declared = datetime.fromisoformat(protocol['declared_at'])
                        requested = datetime.fromisoformat(receipt['requested_at'])
                        if (declared.utcoffset() is None or requested.utcoffset() is None
                                or not selected_at <= declared <= requested):
                            raise ValueError('SCAN_FORECAST_MUST_FOLLOW_FROZEN_SELECTION')
                    target_market = json.loads(intake.target.market_original)['market']
                    if target_market != market or intake.target.symbol != asset:
                        raise ValueError('SCAN_INTAKE_DISCOVERY_MARKET_MISMATCH')
                    prepared = prepare_current_research_forecast(
                        target=intake.target, cf_original=intake.cf_original,
                        cf_receipt=intake.cf_receipt, protocol_original=intake.protocol_original,
                        protocol_sha256=intake.protocol_sha256, as_of=assessed_at)
                    if common:
                        prepared = None
                    else:
                        stages['forecastable'].add(ticker)
                except (ValueError, KeyError, TypeError) as exc:
                    common.append('FORECAST_INTAKE_REJECTED:'+str(exc))
            depth = None
            book = books.get(ticker)
            if book is None:
                common.append('BOOK_NOT_ACQUIRED')
            else:
                try:
                    if series in selections and book.received_at < selections[series].selected_at:
                        raise ValueError('SCAN_BOOK_MUST_FOLLOW_FROZEN_SELECTION')
                    depth = _levels(book, ticker, require_two_sided=False)
                except (ValueError, KeyError, TypeError) as exc:
                    common.append('BOOK_INVALID:'+str(exc))
            market_rows = []
            for side, opposite in (('YES', 'no'), ('NO', 'yes')):
                blockers = list(common)
                row: dict[str, Any] = {
                    'asset': asset, 'series': series, 'ticker': ticker, 'side': side,
                    'discovery_sources': sighting['sources'],
                    'close_time': market.get('close_time'),
                    'expected_expiration_time': market.get('expected_expiration_time'),
                    'latest_expiration_time': market.get('latest_expiration_time'),
                    'legacy_expiration_time': market.get('expiration_time'),
                    'observation_close_hours': close_hours,
                    'paper_horizon_verified': False,
                    'paper_horizon_blocker': 'CERTIFIED_FINAL_SETTLEMENT_BOUND_REQUIRED',
                    'rule_certified': False, 'calibrated': False, 'paper_eligible': False,
                    'rule_status': 'RULE_NOT_CERTIFIED_BY_SCAN',
                    'risk_status': 'NOT_EVALUATED_MISSING_QUALIFICATION_PREREQUISITES',
                    'scope': 'CURRENT_UNCALIBRATED_RESEARCH',
                    'forecast': prepared, 'forecast_probability': None,
                    'executable_price': None, 'fee': None, 'snapshot_impact': None,
                    'gross_edge': None, 'after_fee': None, 'after_execution': None,
                    'uncertainty': None, 'full_net_ev': None, 'conservative_bound': None,
                    'conditional_calibration': None,
                    'candidate_uncertainty_applicability': None,
                    'net_lower_bound': None, 'net_upper_bound': None,
                    'book_source': ({'url': book.url, 'sha256': book.sha256,
                                     'received_at': book.received_at.isoformat()}
                                    if book else None),
                    'execution_authority': False,
                }
                probability = None
                if prepared is not None:
                    probability = Decimal(prepared['probability_yes'])
                    if side == 'NO':
                        probability = 1-probability
                    row['forecast_probability'] = str(probability)
                if depth is not None and book is not None:
                    if not depth[opposite]:
                        blockers.append('NO_EXECUTABLE_ASK')
                    else:
                        price = Decimal(1)-depth[opposite][0][0]
                        impact = snapshot_one_contract_impact(
                            ticker=ticker, side=side, price=price,
                            originals=(book,), decision_at=assessed_at)
                        fee = public_paper_fee(series=series, price=price,
                                               originals=fee_originals.get(series, ()),
                                               assessed_at=assessed_at)
                        row.update(executable_price=str(price), fee=fee, snapshot_impact=impact)
                        if (prepared is not None and probability is not None
                                and ticker in calibration):
                            evidence = assess_current_conditional_calibration(
                                originals=calibration[ticker], ticker=ticker,
                                event_id=market.get('event_ticker', ''), series=series,
                                model_version=prepared['model'], side=side,
                                selected_probability=probability, executable_price=price,
                                decision_at=assessed_at, book=book,
                                fee_originals=fee_originals.get(series, ()))
                            row['conditional_calibration'] = evidence
                            cal_originals = calibration[ticker]
                            applicability = verify_candidate_uncertainty(
                                decision=dict(ticker=ticker,
                                    event_id=market.get('event_ticker', ''), series=series,
                                    side='BUY_'+side, selected_probability=str(probability),
                                    model_version=prepared['model'], segment=cal_originals.segment,
                                    decision_at=assessed_at.isoformat(),
                                    market=market, forecast=prepared,
                                    discovery_sources=sighting['sources'],
                                    book_sha256=book.sha256),
                                policy_version=cal_originals.policy_version,
                                dataset=cal_originals.dataset, protocol=cal_originals.protocol,
                                independence_review=cal_originals.independence_review)
                            row['candidate_uncertainty_applicability'] = applicability
                            blockers.extend(applicability['blockers'])
                            blockers.extend(evidence['blockers'])
                            if evidence['conditional_uncertainty_value'] is not None:
                                stages['conditional_uncertainty_supported'].add(ticker)
                        blockers.extend(impact['blockers']+fee['blockers'])
                        if impact['value'] is not None:
                            stages['book_valid_independent_of_forecast'].add(ticker)
                            if prepared is not None:
                                stages['book_valid'].add(ticker)
                        if probability is not None and impact['value'] is not None:
                            gross = probability-price
                            row['gross_edge'] = str(gross)
                            if gross > 0:
                                stages['gross_positive'].add(ticker)
                            if fee['value'] is not None:
                                after_fee = gross-Decimal(fee['value'])
                                after_execution = after_fee-Decimal(impact['value'])
                                row.update(after_fee=str(after_fee),
                                           after_execution=str(after_execution))
                                if after_fee > 0:
                                    stages['positive_after_fee'].add(ticker)
                                if after_execution > 0:
                                    stages['positive_after_snapshot_impact'].add(ticker)
                                assert prepared is not None
                                bound, lower, upper = binary_support_net_bounds(
                                    selected_probability=probability, executable_price=price,
                                    fee=Decimal(fee['value']),
                                    snapshot_impact=Decimal(impact['value']),
                                    model=prepared['model'], segment=asset,
                                    assessed_at=assessed_at)
                                row.update(conservative_bound=json.loads(
                                               json.dumps(asdict(bound), default=str)),
                                           net_lower_bound=str(lower), net_upper_bound=str(upper))
                                stages['conservative_bounds_known'].add(ticker)
                blockers.extend(['CALIBRATED_UNCERTAINTY_UNKNOWN', 'CALIBRATION_UNVERIFIED',
                                 'RULE_NOT_CERTIFIED_BY_SCAN',
                                 'CERTIFIED_FINAL_SETTLEMENT_BOUND_REQUIRED'])
                row['blockers'] = list(dict.fromkeys(blockers))
                row['first_blocker'] = row['blockers'][0]
                rows.append(row)
                market_rows.append(row)
            first = market_rows[0]['first_blocker']
            first_blocker_counts[first] = first_blocker_counts.get(first, 0)+1
    if set(books)-recognized or set(inputs)-recognized or set(calibration)-recognized:
        raise ValueError('SCAN_BOOK_OR_INTAKE_OUTSIDE_VERIFIED_DISCOVERY')
    return {
        'version': ('EVENT_SELECTED_CURRENT_RESEARCH_V1' if selections
                    else 'PAGINATED_CURRENT_RESEARCH_V2'), 'assessed_at': assessed_at.isoformat(),
        'scope': 'BOUNDED_DISCOVERY_CURRENT_RESEARCH_NOT_PAPER_ADMISSION',
        'families': families, 'rows': rows,
        'funnel_count_unit': 'UNIQUE_MARKETS_WITH_AT_LEAST_ONE_PASSING_SIDE',
        'funnel': {**{name: len(tickers) for name, tickers in stages.items()},
                   'horizon_eligible': 0, 'forecast_ready': len(stages['forecastable']),
                   'phase_3m_pass': 0, 'phase_3n_allow': 0},
        'paper_horizon_policy': 'CERTIFIED_REVIEW_INCLUSIVE_FINAL_DEADLINE_WITHIN_72H',
        'risk_evaluation_status': 'NOT_EVALUATED_MISSING_QUALIFICATION_PREREQUISITES',
        'first_blocker_counts': first_blocker_counts,
        'request_limits': {'pages_per_family': MAX_PAGES_PER_FAMILY,
                           'rows_per_page': limit, 'books_total': MAX_SCAN_BOOKS},
        'research_evaluator_operational': True, 'full_net_scanner_operational': False,
        'execution_authority': False, 'database_writes': 0, 'paper_orders_created': 0,
    }
