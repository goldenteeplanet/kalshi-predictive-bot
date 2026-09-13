"""Bounded assessment leaderboard; research economics never grants admission."""

from __future__ import annotations

import os
from collections import defaultdict
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from html import escape
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from kalshi_predictor.overnight_paper.alpha_analysis import render_alpha_analysis


def leaderboard(alpha: dict[str, Any]) -> list[dict[str, Any]]:
    """Describe latest checked assessments without inventing linked outcomes.

    Two sides of one model forecast count once within a segment. A forecast can
    appear in both side-price bands; segment forecast counts are not additive.
    Event/dependency counts and predictive scores require a separate bound join.
    """
    if alpha.get('status') != 'CAPTURED_ARITHMETIC_CHECKED':
        return []
    rows = alpha.get('rows')
    if not isinstance(rows, list) or len(rows) > 600:
        raise ValueError('ALPHA_LAB_BOUNDED_ROWS_REQUIRED')
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row['probability'] is None:
            continue
        key = tuple(str(row[field]) for field in ('model', 'asset', 'horizon', 'price_band'))
        groups[(key[0], key[1], key[2], key[3])].append(row)
    result = []
    with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
        for (model, asset, horizon, band), items in sorted(groups.items()):
            gross = [Decimal(r['gross_edge']) for r in items if r['gross_edge'] is not None]
            after = [Decimal(r['after_fee']) for r in items if r['after_fee'] is not None]
            result.append(dict(
                model=model, asset=asset, horizon=horizon, price_band=band,
                forecast_n=len({r['ticker'] for r in items}), event_n=None,
                dependency_group_n=None, brier=None, log_loss=None, ece=None, accuracy=None,
                executable_side_n=len(gross), fee_supported_side_n=len(after),
                gross_positive_pct=str(100 * sum(v > 0 for v in gross) / Decimal(len(gross)))
                if gross else None,
                after_fee_positive_pct=str(100 * sum(v > 0 for v in after) / Decimal(len(after)))
                if after else None,
                after_fee_gt_5c_pct=str(100 * sum(v > Decimal('.05') for v in after)
                                      / Decimal(len(after))) if after else None,
                mean_after_fee_edge=str(sum(after) / len(after)) if after else None,
                best_after_fee_edge=str(max(after)) if after else None,
                hypothetical_executable_return=None,
                status='INSUFFICIENT_DATA', holdout_validated=False,
                predictive_quality_status='OUTCOMES_NOT_JOINED',
                trading_value_status='CAPTURED_ARITHMETIC_ONLY', paper_eligible=False,
            ))
    return result


def lab_snapshot(current: dict[str, Any]) -> dict[str, Any]:
    alpha = (current.get('latest_assessment_batch') or {}).get('alpha_analysis') or {}
    return dict(schema='ALPHA_LAB_V1', scope='LATEST_ASSESSMENT_CAPTURE_NOT_HOLDOUT',
                assessed_at=alpha.get('assessed_at'), freshness=alpha.get('freshness'),
                alpha_funnel=alpha.get('funnel'), leaderboard=leaderboard(alpha),
                horizon_semantics='REMAINING_TIME_TO_OBSERVATION_NOT_MARKET_FAMILY',
                price_band_semantics=alpha.get('price_band_semantics'),
                status='INSUFFICIENT_DATA', paper_eligible=False, full_net_ev=None,
                predictive_quality='OUTCOMES_NOT_JOINED',
                trading_value='CAPTURED_AFTER_RECORDED_FEE_BEFORE_IMPACT_AND_UNCERTAINTY')


def render_lab(current: dict[str, Any]) -> str:
    report = lab_snapshot(current)

    def text(value: Any) -> str:
        return escape('Unknown' if value is None else str(value))

    def cell(row: dict[str, Any], field: str) -> str:
        value = row.get(field)
        if value is not None and field in ('mean_after_fee_edge', 'best_after_fee_edge'):
            with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
                return f'{Decimal(value) * 100:+.4f}c'
        if value is not None and field.endswith('_pct'):
            return f'{Decimal(value):.2f}%'
        return text(value)

    fields = (
        ('model', 'Model'), ('asset', 'Asset'), ('horizon', 'Observation lead'),
        ('price_band', 'Executable price band'), ('forecast_n', 'Forecast N'),
        ('event_n', 'Event N'), ('dependency_group_n', 'Dependency group N'),
        ('brier', 'Brier'), ('log_loss', 'Log loss'),
        ('executable_side_n', 'Executable sides'), ('fee_supported_side_n', 'Fee-known sides'),
        ('gross_positive_pct', 'Gross positive %'),
        ('after_fee_positive_pct', 'After-fee positive %'),
        ('after_fee_gt_5c_pct', 'After-fee >5c %'),
        ('mean_after_fee_edge', 'Mean after-fee edge'),
        ('best_after_fee_edge', 'Best after-fee edge'), ('status', 'Status'),
    )
    head = ''.join(f'<th>{escape(label)}</th>' for _, label in fields)
    body = ''.join('<tr>' + ''.join(f'<td>{cell(row, key)}</td>' for key, _ in fields)
                   + '</tr>' for row in report['leaderboard'])
    if not body:
        body = f'<tr><td colspan="{len(fields)}">No checked forecast assessments available.</td></tr>'
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Alpha Lab</title><style>body{font:16px system-ui;margin:2rem;color:#172535;'
        'background:#f5f7fa}p{line-height:1.5}.table{overflow:auto}table{border-collapse:collapse;'
        'background:white}td,th{padding:.7rem;border:1px solid #ccd5df;text-align:left}'
        '.metrics{display:flex;flex-wrap:wrap;gap:1rem}.metrics div{background:white;padding:1rem}'
        '</style></head><body><h1>Alpha Lab</h1>'
        '<nav><a href="/positive-ev">Positive EV</a> · '
        '<a href="/paper-live">Shadow lifecycle</a> · '
        '<a href="/api/alpha-lab">Research JSON</a></nav>'
        '<p>Captured research economics. After-fee edge excludes execution impact and '
        'calibrated uncertainty. This page cannot authorize or submit a trade.</p>'
        f'<p>Capture: {text(report["assessed_at"])}. Freshness: {text(report["freshness"])}.</p>'
        '<h2>Predictive quality</h2><p>Official outcomes are not joined in this view. '
        'Brier, log loss, ECE, accuracy and hypothetical returns remain Unknown. '
        'No segment is holdout validated.</p><h2>Trading value</h2>'
        '<p>Gross percentages use executable sides; after-fee percentages use fee-known '
        'sides. Missing values are excluded, never counted as zero. Forecast N counts '
        'distinct contracts per model within each segment; the same forecast can appear '
        'in multiple side-price bands. Counts do not measure independent observations. '
        'Observation lead is not the market settlement interval.</p>'
        f'<div class="table"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
        + render_alpha_analysis(current) + '</body></html>'
    )


def create_router() -> APIRouter:
    router = APIRouter()

    def current() -> dict[str, Any]:
        from kalshi_predictor.overnight_paper.dashboard import snapshot

        path = os.environ.get('OVERNIGHT_PAPER_DB')
        return dict(snapshot(Path(path) if path else None).get('current_research', {}))

    @router.get('/alpha-lab', response_class=HTMLResponse)
    def alpha_lab() -> HTMLResponse:
        return HTMLResponse(render_lab(current()))

    @router.get('/api/alpha-lab')
    def alpha_lab_api() -> dict[str, Any]:
        return lab_snapshot(current())

    return router
