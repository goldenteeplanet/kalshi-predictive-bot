"""Bounded captured alpha arithmetic; never calibration or candidate admission."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from html import escape
from typing import Any

from .current_assessment_view import _number, latest_assessment_batch
from .current_research_index import ResearchValidationSession
from .store import aware

ASSETS = ('BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'UNKNOWN')
PROBABILITY_BANDS = ('0-10%', '10-25%', '25-40%', '40-60%', '60-75%', '75-90%', '90-100%')
PRICE_BANDS = ('1-10c', '10-25c', '25-40c', '40-60c', '60-75c', '75-90c', '90-99c')


def _funnel(rows: list[dict[str, Any]]) -> dict[str, Any]:
    gross = [Decimal(r['gross_edge']) for r in rows if r['gross_edge'] is not None]
    after = [Decimal(r['after_fee']) for r in rows if r['after_fee'] is not None]
    return dict(
        side_assessments=len(rows),
        forecast_ready_markets=len({r['ticker'] for r in rows if r['probability'] is not None}),
        forecast_ready_sides=sum(r['probability'] is not None for r in rows),
        executable_forecast_sides=len(gross), gross_positive=sum(v > 0 for v in gross),
        gross_thresholds_cents={str(c): sum(v > Decimal(c) / 100 for v in gross)
                                for c in (1, 3, 5, 7, 10)},
        after_fee_known=len(after), after_fee_positive=sum(v > 0 for v in after),
        after_fee_gt_5c=sum(v > Decimal('.05') for v in after),
        after_fee_thresholds_cents={str(c): sum(v > Decimal(c) / 100 for v in after)
                                   for c in (0, 2, 5, 7, 10)},
        missing_forecast=sum(r['probability'] is None for r in rows),
        missing_executable_ask=sum(r['executable_price'] is None for r in rows),
        missing_supported_fee=sum(r['recorded_fee'] is None for r in rows),
        gross_distribution=[str(v) for v in sorted(gross)],
        after_fee_distribution=[str(v) for v in sorted(after)],
        disagreement_thresholds_pp={str(c): sum(
            r['model_market_disagreement'] is not None
            and abs(Decimal(r['model_market_disagreement'])) >= Decimal(c) / 100 for r in rows)
            for c in (3, 5, 7, 10)},
    )


def _price_band(value: str | None) -> str:
    if value is None:
        return 'UNKNOWN'
    price = Decimal(value)
    if price < Decimal('.01') or price > Decimal('.99'):
        return 'OUTSIDE_DECLARED_BANDS'
    for boundary, label in zip(('.10', '.25', '.40', '.60', '.75', '.90', '1'),
                               PRICE_BANDS, strict=True):
        if price < Decimal(boundary):
            return label
    raise ValueError('ALPHA_PRICE_BOUND')


def _probability_band(value: str | None) -> str:
    if value is None:
        return 'UNKNOWN'
    p = Decimal(value)
    for boundary, label in zip(('.10', '.25', '.40', '.60', '.75', '.90', '1.01'),
                               PROBABILITY_BANDS, strict=True):
        if p < Decimal(boundary):
            return label
    raise ValueError('ALPHA_PROBABILITY_BOUND')


def _horizon(value: Any) -> str:
    # Scanner JSON stores this descriptive duration as a float, unlike probabilities.
    hours = _number(str(value) if type(value) is float else value, '0', '87600')
    if hours is None:
        return 'UNKNOWN'
    minutes = hours * 60
    for bound, label in ((1, 'LE_1_MIN'), (5, 'GT_1_LE_5_MIN'),
                         (15, 'GT_5_LE_15_MIN'), (60, 'GT_15_LE_60_MIN')):
        if minutes <= bound:
            return label
    return 'GT_60_MIN'


def captured_alpha_analysis(records: list[dict[str, Any]], *, now: datetime) -> dict[str, Any]:
    """Use at most 600 strictly read AS envelopes; select latest before economics.

    Input provenance is the caller's responsibility. This function checks recorded
    arithmetic, not provider originals. No performance or calibration is inferred
    from forecast probabilities alone, and stale captures have no current winner.
    """
    if len(records) > 600 or any(e.get('record_kind') != 'ASSESSMENT' for e in records):
        raise ValueError('ALPHA_BOUNDED_ASSESSMENT_INPUT_REQUIRED')
    batch = latest_assessment_batch(records, now=now)
    result: dict[str, Any] = dict(
        version='CAPTURED_ALPHA_ANALYSIS_V1', status=batch['status'],
        assessed_at=batch['assessed_at'], freshness=batch['freshness'],
        age_seconds=batch['age_seconds'], scan_sha256=batch['scan_sha256'],
        scope='JOURNAL_AND_ARITHMETIC_ONLY', original_replay=False,
        batch_completeness='UNKNOWN', paper_eligible=False, full_net_ev=None,
        current_best_semantics='RECENT_RESEARCH_CAPTURE_EXECUTION_UNVERIFIED',
        uncertainty_status='UNKNOWN', calibration_value='NOT_EVALUATED_BY_ALPHA_VIEW',
        model_vs_market_performance=None, effective_independent_n=None,
        horizon_semantics='REMAINING_TIME_TO_OBSERVATION_NOT_MARKET_FAMILY',
        probability_band_semantics='LOWER_INCLUSIVE_UPPER_EXCLUSIVE_LAST_INCLUDES_ONE',
        price_band_semantics='EXECUTABLE_SIDE_ASK_LOWER_INCLUSIVE_UPPER_EXCLUSIVE_LAST_INCLUDES_99C',
        rows=[], by_asset={}, by_horizon={}, by_probability={}, by_price={}, funnel=None,
        best_captured_after_fee=None, best_captured_divergence=None,
        current_best_after_fee=None, current_best_divergence=None,
    )
    if batch['status'] != 'CAPTURED_ARITHMETIC_CHECKED':
        result['blocker'] = batch.get('blocker', 'NO_ASSESSMENTS')
        return result
    latest = aware(batch['assessed_at'])
    originals = {(e['record']['ticker'], e['record']['side']): e['record']
                 for e in records if aware(e['record']['assessed_at']) == latest}
    with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
        rows = []
        for checked in batch['rows']:
            record = originals[(checked['ticker'], checked['side'])]
            row = dict(checked)
            forecast = record.get('forecast')
            model = forecast.get('model') if isinstance(forecast, dict) else None
            row.update(asset=record.get('asset') if record.get('asset') in ASSETS else 'UNKNOWN',
                       model=model if isinstance(model, str) and 0 < len(model) <= 200 else 'UNKNOWN',
                       horizon=_horizon(record.get('observation_close_hours')),
                       probability_band=_probability_band(row['probability']),
                       price_band=_price_band(row['executable_price']),
                       market_midpoint=None, model_market_disagreement=None,
                       midpoint_evidence='MISSING_SAME_BOOK_TWO_SIDED_ASKS',
                       after_fee=None, trading_value='UNKNOWN')
            if row['gross_edge'] is not None and row['recorded_fee'] is not None:
                after = Decimal(row['gross_edge']) - Decimal(row['recorded_fee'])
                row['after_fee'] = str(after)
                row['trading_value'] = ('POSITIVE_AFTER_RECORDED_FEE_RESEARCH_ONLY' if after > 0
                                        else 'NONPOSITIVE_AFTER_RECORDED_FEE')
            rows.append(row)
        sides = {(r['ticker'], r['side']): r for r in rows}
        for row in rows:
            opposite = 'NO' if row['side'] == 'YES' else 'YES'
            other = sides.get((row['ticker'], opposite))
            original = originals[(row['ticker'], row['side'])]
            paired = originals.get((row['ticker'], opposite), {})
            book, pair_book = original.get('book_source'), paired.get('book_source')
            same_book = (isinstance(book, dict) and isinstance(pair_book, dict)
                         and book == pair_book and isinstance(book.get('sha256'), str)
                         and len(book['sha256']) == 64
                         and all(c in '0123456789abcdef' for c in book['sha256']))
            if (other and same_book and row['executable_price'] is not None
                    and other['executable_price'] is not None):
                ask, bid = Decimal(row['executable_price']), 1-Decimal(other['executable_price'])
                if bid <= ask:
                    mid = (bid + ask) / 2
                    row['market_midpoint'] = str(mid)
                    row['midpoint_evidence'] = 'IMPLIED_FROM_SAME_CAPTURED_BOOK_ASKS_NOT_REPLAYED'
                    if row['probability'] is not None:
                        row['model_market_disagreement'] = str(Decimal(row['probability']) - mid)
        result['rows'] = rows
        for field, output in (('asset', 'by_asset'), ('horizon', 'by_horizon'),
                              ('probability_band', 'by_probability'), ('price_band', 'by_price')):
            keys = (ASSETS if field == 'asset' else
                    (*PRICE_BANDS, 'UNKNOWN', 'OUTSIDE_DECLARED_BANDS') if field == 'price_band'
                    else sorted({r[field] for r in rows}))
            result[output] = {key: _funnel([r for r in rows if r[field] == key]) for key in keys}
        result['funnel'] = _funnel(rows)
        for metric, output in (('after_fee', 'after_fee'),
                               ('model_market_disagreement', 'divergence')):
            known = [r for r in rows if r[metric] is not None]
            if known:
                winner = max(known, key=lambda r: (
                    abs(Decimal(r[metric])) if output == 'divergence' else Decimal(r[metric])))
                best = {key: winner[key] for key in ('ticker', 'side', 'asset', metric)}
                result['best_captured_' + output] = best
                if batch['freshness'] == 'RECENT_CAPTURE_NOT_LIVE':
                    result['current_best_' + output] = best
    return result


def indexed_alpha_analysis(session: ResearchValidationSession, *, now: datetime) -> dict[str, Any]:
    """Bounded same-snapshot originals; session failures propagate without fallback."""
    ids = session.latest_assessment_ids()
    records = [json.loads(session.read_original(key)) for key in ids]
    result = captured_alpha_analysis(records, now=now)
    _ = session.manifest
    return result


def render_alpha_analysis(current: dict[str, Any]) -> str:
    """Current mission context plus captured economics, before the legacy report."""
    alpha = (current.get('latest_assessment_batch') or {}).get('alpha_analysis') or {}
    funnel = alpha.get('funnel') or {}

    def text(value: Any) -> str:
        return escape('Unknown' if value is None else str(value))

    def best(value: Any, metric: str) -> str:
        if not isinstance(value, dict) or value.get(metric) is None:
            return 'Unknown'
        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
            cents = Decimal(value[metric]) * 100
            return (f'{text(value.get("asset"))} {text(value.get("ticker"))} '
                    f'{text(value.get("side"))}: {cents:+.6f} cents per $1 payout')

    cards = ''.join(f'<div><strong>{label}</strong><p>{text(value)}</p></div>'
                    for label, value in (
                        ('Prospective shadows', current.get('prospective_shadow_count')),
                        ('Evaluated shadows', current.get('evaluated_shadow_count')),
                        ('Open shadows', (current.get('shadow_state_counts') or {}).get('OPEN')),
                        ('Awaiting official FINAL',
                         (current.get('shadow_state_counts') or {}).get('AWAITING_FINAL')),
                        ('Forecast-ready markets', funnel.get('forecast_ready_markets')),
                        ('Forecast-ready sides', funnel.get('forecast_ready_sides')),
                        ('Gross-positive sides', funnel.get('gross_positive')),
                        ('After-fee positive sides', funnel.get('after_fee_positive')),
                        ('After-fee >5 cents', funnel.get('after_fee_gt_5c')),
                    ))
    counts = funnel.get('gross_thresholds_cents') or {}
    thresholds = ', '.join(f'&gt;{c}c: {text(counts.get(str(c)))}' for c in (1, 3, 5, 7, 10))
    after_counts = funnel.get('after_fee_thresholds_cents') or {}
    after_thresholds = ', '.join(f'&gt;{c}c: {text(after_counts.get(str(c)))}'
                                for c in (0, 2, 5, 7, 10))
    tables = ''
    for key, label in (('by_asset', 'Alpha by asset'), ('by_horizon', 'Alpha by horizon'),
                       ('by_probability', 'Alpha by probability'),
                       ('by_price', 'Alpha by executable price')):
        body = ''.join('<tr>' + ''.join(f'<td>{text(value)}</td>' for value in (
            name, item['forecast_ready_sides'], item['gross_positive'],
            item['gross_thresholds_cents']['5'], item['after_fee_positive'],
            item['after_fee_gt_5c'])) + '</tr>' for name, item in (alpha.get(key) or {}).items())
        tables += (f'<h3>{label}</h3><div class="table"><table><thead><tr>'
                   '<th>Segment</th><th>Forecast sides</th><th>Gross positive</th>'
                   '<th>Gross &gt;5c</th><th>After-fee positive</th><th>After-fee &gt;5c</th>'
                   f'</tr></thead><tbody>{body}</tbody></table></div>')
    return (
        '<section id="alpha-discovery"><h2>Alpha discovery and prospective calibration</h2>'
        f'<p>Status: {text(alpha.get("status"))}; captured at {text(alpha.get("assessed_at"))}; '
        f'freshness: {text(alpha.get("freshness"))}.</p><div class="metrics">{cards}</div>'
        f'<h3>Gross edge funnel</h3><p>{thresholds}</p>'
        f'<h3>After-fee edge funnel</h3><p>{after_thresholds}</p>'
        '<p>Recorded supported fee estimates only; execution '
        'impact and calibrated uncertainty are not deducted in this funnel.</p>'
        '<p>Recent research captures below have unverified execution; they are not current '
        'executable opportunities.</p>'
        f'<p>Recent research divergence: '
        f'{best(alpha.get("current_best_divergence"), "model_market_disagreement")}</p>'
        f'<p>Recent after-fee edge: {best(alpha.get("current_best_after_fee"), "after_fee")}</p>'
        f'<p>Best captured after-fee evidence: '
        f'{best(alpha.get("best_captured_after_fee"), "after_fee")}</p>'
        '<p>Stale captures are historical evidence only. Horizon means remaining time to '
        'observation, not a supported market interval. Missing values are not zero.</p>'
        + tables
        + '<h3>Model vs market performance and calibration status</h3>'
        '<p>Not evaluated by this assessment-only view. Brier, log loss, holdout superiority '
        'and effective independent N require separate prospectively bound outcome evidence. '
        'Calibration value is separate from trading value; '
        'accurate forecasts may lose after fees.</p>'
        '<h3>Paper blocker</h3><p>Full net EV: Unknown. Paper eligibility: No. '
        'Gross or after-fee divergence does not establish calibrated uncertainty, settlement '
        'authority, impact, or Phase 3M/3N approval. Provider originals are not replayed by '
        'this view; journal checks and recorded arithmetic only. Batch completeness: Unknown.</p>'
        '</section>'
    )
