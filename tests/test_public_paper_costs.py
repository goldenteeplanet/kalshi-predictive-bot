from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from kalshi_predictor.crypto.account_fee_evidence import FeeAuthorityOriginal
from kalshi_predictor.crypto.cost_evidence import OriginalBook
from kalshi_predictor.crypto.cost_record import build_cost_record, replay_cost_record
from kalshi_predictor.crypto.public_paper_costs import (
    SCHEDULE_URL,
    general_taker_cent_fee,
    public_paper_fee,
    snapshot_one_contract_impact,
)

AT = datetime(2026, 9, 12, 23, 35, tzinfo=UTC)
TICKER = 'KXSOLE-26SEP1317-T64'


def authority():
    root = Path(__file__).parent/'fixtures/public_paper_fees'
    return tuple(FeeAuthorityOriginal(url, (root/file).read_bytes(), AT) for file, url in (
        ('fee-schedule-reviewed-facts.json', SCHEDULE_URL),
        ('KXSOLE-series.json', 'https://external-api.kalshi.com/trade-api/v2/series/KXSOLE'),
        ('KXSOLE-fee-changes.json', 'https://external-api.kalshi.com/trade-api/v2/series/'
         'fee_changes?series_ticker=KXSOLE&show_historical=true'),
    ))


def book(no_levels=b'[["0.61","10"]]', at=AT):
    return OriginalBook(
        f'https://external-api.kalshi.com/trade-api/v2/markets/{TICKER}/orderbook?depth=10',
        b'{"orderbook_fp":{"yes_dollars":[["0.35","10"]],"no_dollars":'+no_levels+b'}}', at,
    )


@pytest.mark.parametrize(('price','fee'), [
    ('.01','.01'),('.05','.01'),('.10','.01'),('.25','.02'),('.39','.02'),
    ('.50','.02'),('.54','.02'),('.75','.02'),('.95','.01'),('.99','.01'),
])
def test_requested_cent_model_and_published_examples(price, fee):
    with localcontext() as context:
        context.prec = 4
        assert general_taker_cent_fee(Decimal(price)) == Decimal(fee)


@pytest.mark.parametrize('price', [0.39, Decimal('NaN'), Decimal('Infinity'), Decimal('0')])
def test_no_float_or_nonfinite_fee(price):
    with pytest.raises(ValueError):
        general_taker_cent_fee(price)


def test_public_model_needs_no_account_class_but_preserves_rounding_conflict():
    row = public_paper_fee(series='KXSOLE', price=Decimal('.39'), originals=authority(),
                           assessed_at=AT)
    assert row['value'] == '0.02'
    assert row['applicability'] == 'GENERAL_PUBLIC_FEE_APPLIES'
    assert row['status'] == 'ESTIMATED_WITH_SUPPORT'
    assert row['rounding_status'] == 'FEE_CONFLICT_FORMULA_VERSUS_EXAMPLES'
    assert row['exact_account_fee_certified'] is False
    assert not row['execution_authority']


def test_unknown_series_tampered_original_or_stale_review_cannot_use_policy():
    originals = authority()
    bad = originals[:1]+(FeeAuthorityOriginal(originals[1].url, b'{}', AT),)+originals[2:]
    for series, docs, at in [('KXOTHER', originals, AT), ('KXSOLE', bad, AT),
                             ('KXSOLE', originals, AT+timedelta(days=2))]:
        row = public_paper_fee(series=series, price=Decimal('.39'), originals=docs, assessed_at=at)
        assert row['value'] is None and row['status'] == 'UNKNOWN'
    subcent = public_paper_fee(series='KXSOLE', price=Decimal('.398'), originals=originals,
                              assessed_at=AT)
    assert subcent['value'] is None


def test_snapshot_impact_excludes_spread_and_future_quote_movement():
    row = snapshot_one_contract_impact(ticker=TICKER, side='YES', price=Decimal('.39'),
                                      originals=(book(), book(b'[["0.60","10"]]',
                                                             AT+timedelta(seconds=1))),
                                      decision_at=AT)
    assert row['value'] == '0.00' and row['fill_price'] == '0.39'
    assert row['adverse_selection_cost'] is None and row['latency_cost'] is None
    assert row['fill_status'] == 'BOOK_FILL_PRICE_KNOWN'


def test_depth_vwap_and_insufficient_depth():
    row = snapshot_one_contract_impact(ticker=TICKER, side='YES', price=Decimal('.39'),
                                      originals=(book(b'[["0.61","0.5"],["0.59","1"]]'),),
                                      decision_at=AT)
    assert Decimal(row['value']) == Decimal('.01')
    missing = snapshot_one_contract_impact(ticker=TICKER, side='YES', price=Decimal('.39'),
                                          originals=(book(b'[["0.61","0.5"]]'),), decision_at=AT)
    assert missing['value'] is None


def test_only_executable_side_is_needed_for_snapshot_fill():
    original = book()
    single = OriginalBook(original.url, original.payload.replace(
        b'"yes_dollars":[["0.35","10"]]', b'"yes_dollars":[]'), AT)
    row = snapshot_one_contract_impact(ticker=TICKER, side='YES', price=Decimal('.39'),
                                      originals=(single,), decision_at=AT)
    assert row['fill_status'] == 'BOOK_FILL_PRICE_KNOWN'
    assert Decimal(row['value']) == 0


def test_public_cost_record_replays_originals_and_keeps_uncertainty_unknown():
    decision = dict(ticker=TICKER, event_id='KXSOLE-26SEP1317', series='KXSOLE',
                    side='BUY_YES', selected_probability='.60', executable_price='.39',
                    model_version='research-model', segment='UNDECLARED',
                    decision_at=AT.isoformat())
    record = build_cost_record(decision=decision, selected_probability=Decimal('.60'),
                               executable_price=Decimal('.39'), side='YES', books=(book(),),
                               public_paper_fee_originals=authority(), public_paper_assessed_at=AT)
    row = replay_cost_record(record, expected_decision=decision)
    assert row['exchange_fee']['value'] == '0.02'
    assert Decimal(row['execution_price_impact']['value']) == 0
    assert row['uncertainty']['value'] is None and row['full_net_ev'] is None
    assert 'RULE_UNCERTIFIED' in row['blockers']
    assert 'FEE_ACCOUNT_APPLICABILITY_NOT_REVIEWED' not in row['blockers']
    assert row['stress_deducted'] is False and row['execution_authority'] is False
    record['assessment']['exchange_fee']['value'] = '0'
    with pytest.raises(ValueError, match='RECOMPUTATION_MISMATCH'):
        replay_cost_record(record, expected_decision=decision)
