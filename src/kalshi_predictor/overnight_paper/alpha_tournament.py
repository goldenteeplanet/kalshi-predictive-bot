"""Descriptive frozen tournament joins, with no model fitting or promotion."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path
from typing import Any

from .alpha_analysis import _price_band


def _decimal(value: Any, low: str, high: str) -> Decimal:
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError('TOURNAMENT_DECIMAL_REQUIRED')
    result = Decimal(value)
    if not result.is_finite() or not Decimal(low) <= result <= Decimal(high):
        raise ValueError('TOURNAMENT_DECIMAL_BOUND')
    return result


def score_tournament(analysis: dict[str, Any], evaluation: dict[str, Any]) -> dict[str, Any]:
    """Join retained scores and fee replay by exact decision/model/endpoint.

    Caller must pin these input bytes to the reviewed capture/outcome pipeline.
    This view rechecks arithmetic, not official-provider provenance or causality.
    """
    if not isinstance(analysis, dict) or not isinstance(evaluation, dict):
        raise ValueError('TOURNAMENT_OBJECT_REQUIRED')
    rows, scores = analysis.get('rows'), evaluation.get('rows')
    if (not isinstance(rows, list) or not isinstance(scores, list)
            or not 0 < len(rows) <= 600 or not 0 < len(scores) <= 300):
        raise ValueError('TOURNAMENT_BOUNDED_INPUT_REQUIRED')
    if evaluation.get('execution_authority') is not False:
        raise ValueError('TOURNAMENT_RESEARCH_ONLY')
    by_key = {}
    with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
        for record in scores:
            key = (record['decision_id'], record['model'])
            if key in by_key:
                raise ValueError('TOURNAMENT_DUPLICATE_SCORE')
            score = record['score']
            p = _decimal(score['probability'], '0', '1')
            y = score['outcome']
            if type(y) is not int or y not in (0, 1):
                raise ValueError('TOURNAMENT_BINARY_OUTCOME')
            brier = _decimal(score['brier'], '0', '1')
            if abs(brier - (p-y)**2) > Decimal('1e-24'):
                raise ValueError('TOURNAMENT_BRIER_MISMATCH')
            loss = score['log_loss']
            # Unknown clipping rules cannot be silently substituted.
            if score.get('probability_clipped') is not False:
                raise ValueError('TOURNAMENT_UNSUPPORTED_CLIPPED_SCORE')
            assigned = p if y else 1-p
            if assigned == 0:
                if loss != 'POSITIVE_INFINITY':
                    raise ValueError('TOURNAMENT_EXACT_INFINITE_LOGLOSS_REQUIRED')
            else:
                if type(loss) not in (float, int) or not math.isfinite(loss) or loss < 0:
                    raise ValueError('TOURNAMENT_LOGLOSS_REQUIRED')
                expected = -float(assigned.ln())
                if not math.isclose(loss, expected, rel_tol=1e-12, abs_tol=1e-12):
                    raise ValueError('TOURNAMENT_LOGLOSS_MISMATCH')
            if not isinstance(record.get('event'), str) or not record['event']:
                raise ValueError('TOURNAMENT_EVENT_REQUIRED')
            by_key[key] = record
        grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
        seen = set()
        matched_scores = set()
        for row in rows:
            identity = (row['decision_id'], row['model'], row['side'])
            if identity in seen or row['side'] not in ('YES', 'NO'):
                raise ValueError('TOURNAMENT_DUPLICATE_OR_INVALID_SIDE')
            seen.add(identity)
            if row.get('paper_eligible') is not False or row.get('full_net_ev') is not None:
                raise ValueError('TOURNAMENT_NOT_ADMISSION')
            key = identity[:2]
            score_record = by_key.get(key)
            edge = row.get('gross_minus_supported_fee')
            if score_record is None:
                if edge is not None or row.get('gross') is not None:
                    raise ValueError('TOURNAMENT_MISSING_SCORE_FOR_PRICED_ROW')
                continue
            matched_scores.add(key)
            if any(row[name] != score_record[name] for name in ('ticker', 'rule_version')):
                raise ValueError('TOURNAMENT_SCORE_IDENTITY_MISMATCH')
            band = 'UNKNOWN'
            value = dict(score=score_record, side=row['side'], after=None,
                         gross=(_decimal(row['gross'], '-1', '1')
                                if row.get('gross') is not None else None),
                         hypothetical_result=None, hypothetical_return=None)
            if edge is not None:
                price = _decimal(row['fee']['price'], '0', '1')
                fee = _decimal(row['fee']['value'], '0', '1')
                gross = _decimal(row['gross'], '-1', '1')
                after = _decimal(edge, '-2', '1')
                p = Decimal(score_record['score']['probability'])
                # Retained v1 forecasts serialized a float before complementing
                # NO. Reproduce that explicit representation, not a loose epsilon.
                side_p = p if row['side'] == 'YES' else Decimal(str(1.0-float(p)))
                if abs(side_p-price-gross) > Decimal('1e-24') or gross-fee != after:
                    raise ValueError('TOURNAMENT_ECONOMICS_MISMATCH')
                y = score_record['score']['outcome']
                payout = y if row['side'] == 'YES' else 1-y
                result = Decimal(payout)-price-fee
                value.update(after=after, gross=gross, hypothetical_result=result,
                             hypothetical_return=result/(price+fee) if price+fee else None)
                band = _price_band(str(price))
            grouped[(row['model'], score_record['asset'], row['rule_version'], band)].append(value)
        if matched_scores != set(by_key):
            raise ValueError('TOURNAMENT_UNMATCHED_SCORE')
        leaderboard = []
        for (model, asset, endpoint, band), items in sorted(grouped.items()):
            unique = {v['score']['decision_id']: v['score'] for v in items}
            events = {v['event'] for v in unique.values()}
            gross_values = [v['gross'] for v in items if v['gross'] is not None]
            after_values = [v['after'] for v in items if v['after'] is not None]
            returns = [v['hypothetical_return'] for v in items
                       if v['hypothetical_return'] is not None]
            infinite_losses = sum(v['score']['log_loss'] == 'POSITIVE_INFINITY'
                                  for v in unique.values())
            leaderboard.append(dict(
                model=model, asset=asset, endpoint_hypothesis=endpoint, price_band=band,
                forecast_n=len(unique), event_n=len(events), dependency_group_n=None,
                effective_independent_n=None, status='INSUFFICIENT_DATA',
                brier=str(sum(Decimal(v['score']['brier']) for v in unique.values()) / len(unique)),
                log_loss=('POSITIVE_INFINITY' if infinite_losses else
                          sum(v['score']['log_loss'] for v in unique.values()) / len(unique)),
                infinite_log_loss_n=infinite_losses,
                accuracy=sum((Decimal(v['score']['probability']) >= Decimal('.5'))
                             == bool(v['score']['outcome']) for v in unique.values()) / len(unique),
                ece=None, executable_side_n=None,
                conditional_priced_side_n=len(gross_values),
                book_qualification_status='NOT_PRESENT_IN_RETAINED_INPUT',
                fee_supported_side_n=len(after_values),
                gross_thresholds_cents={str(c): sum(v > Decimal(c)/100 for v in gross_values)
                                        for c in (0, 1, 3, 5, 7, 10)},
                after_fee_thresholds_cents={str(c): sum(v > Decimal(c)/100 for v in after_values)
                                            for c in (0, 2, 5, 7, 10)},
                mean_after_fee_edge=(str(sum(after_values)/len(after_values))
                                     if after_values else None),
                best_after_fee_edge=str(max(after_values)) if after_values else None,
                mean_hypothetical_return=str(sum(returns)/len(returns)) if returns else None,
                paper_eligible=False, full_net_ev=None,
            ))
    return dict(schema='RETAINED_ALPHA_TOURNAMENT_V1', status='RETAINED_ARITHMETIC_CHECKED',
                leaderboard=leaderboard, model_forecasts=len(by_key), side_rows=len(rows),
                scored_side_rows=sum(len(v) for v in grouped.values()),
                unavailable_model_side_rows=len(rows)-sum(len(v) for v in grouped.values()),
                event_n=len({v['event'] for v in scores}), effective_independent_n=None,
                holdout_validated=False, paper_eligible=False, full_net_ev=None,
                scope='PINNED_RETAINED_REPORTS_NOT_PROVIDER_REPLAY_OR_CURRENT_QUOTES')


def read_tournament(root: Path, manifest_sha256: str) -> dict[str, Any]:
    """Load two fixed-name bounded inputs pinned by an operator-reviewed manifest."""
    try:
        if (len(manifest_sha256) != 64 or any(c not in '0123456789abcdef' for c in manifest_sha256)
                or any(p.is_symlink() for p in (root, *root.parents))):
            raise ValueError('TOURNAMENT_PIN_REQUIRED')

        def read(name: str, maximum: int) -> bytes:
            path = root/name
            if path.is_symlink() or not path.is_file():
                raise ValueError('TOURNAMENT_REGULAR_FILE_REQUIRED')
            with path.open('rb') as stream:
                raw = stream.read(maximum+1)
            if len(raw) > maximum:
                raise ValueError('TOURNAMENT_BYTE_BOUND')
            return raw

        raw = read('manifest.json', 16000)
        if hashlib.sha256(raw).hexdigest() != manifest_sha256:
            raise ValueError('TOURNAMENT_MANIFEST_PIN_MISMATCH')
        manifest = json.loads(raw)
        if not isinstance(manifest, dict):
            raise ValueError('TOURNAMENT_MANIFEST_OBJECT')
        if manifest['schema'] != 'ALPHA_TOURNAMENT_INPUTS_V1':
            raise ValueError('TOURNAMENT_MANIFEST_SCHEMA')
        inputs = {}
        for name in ('analysis.json', 'evaluation.json'):
            raw = read(name, 2_000_000)
            if hashlib.sha256(raw).hexdigest() != manifest['files'][name]:
                raise ValueError('TOURNAMENT_INPUT_PIN_MISMATCH')
            inputs[name] = json.loads(raw)
        result = score_tournament(inputs['analysis.json'], inputs['evaluation.json'])
        result.update(manifest_sha256=manifest_sha256, source_hashes=manifest['files'],
                      horizon=manifest['horizon'], cohort=manifest['cohort'])
        return result
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError):
        return dict(status='UNAVAILABLE_OR_INVALID', leaderboard=[], paper_eligible=False,
                    full_net_ev=None, holdout_validated=False)
