import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, Inexact, localcontext

from kalshi_predictor.crypto.cf_process_inputs import digest
from kalshi_predictor.crypto.cf_selection_level import (
    PROFILE,
    PURPOSE,
    VERSION,
    prepare_cf_selection_level,
)
from kalshi_predictor.crypto.control_central_selection import (
    POLICY,
    SCHEMA,
    ControlCentralSelectionInputs,
    select_control_and_central,
)
from kalshi_predictor.crypto.cost_evidence import OriginalBook
from kalshi_predictor.crypto.current_event_selection import (
    CurrentEventDiscovery,
    event_discovery_url,
)

NOW = datetime(2026, 9, 13, 7, 35, 9, tzinfo=UTC)
URL = 'https://external-api.kalshi.com/trade-api/v2/cfbenchmarks/values?id=BRTI'
CONTEXT = dict(
    expected_asset='BTC',
     expected_event_ticker='KXBTC-26SEP1304',
     expected_source_commit='d' * 40
)

def enc(value):
    return json.dumps(value, separators=(',', ':'), allow_nan=False).encode()

def inputs(count=3599):
    end = int((NOW - timedelta(seconds=2)).timestamp() * 1000)
    body = {
        'data': {
            'serverTime': (
                NOW - timedelta(
                    seconds=1
                )
            ).isoformat(
            ),
             'payload': [
                {
                    'time': end - 1000 * (
                        count - i - 1
                    ),
                     'value': '77116.71'
                } for i in range(
                    count
                )
            ]
        }
    }
    plan = dict(
        version=VERSION,
         profile=PROFILE,
         purpose=PURPOSE,
         asset='BTC',
         index_id='BRTI',
         event_ticker=CONTEXT[
            'expected_event_ticker'
        ],
         source_commit='d' * 40,
         declared_at=(
            NOW - timedelta(
                seconds=10
            )
        ).isoformat(
        ),
         not_before=(
            NOW - timedelta(
                seconds=9
            )
        ).isoformat(
        ),
         not_after=(
            NOW + timedelta(
                seconds=120
            )
        ).isoformat(
        ),
         max_cf_gets=1,
         retries=0,
         paper_eligible=False,
         execution_authority=False
    )
    rec = dict(
        schema='cf-level-selection-receipt-v1',
         profile=PROFILE,
         purpose=PURPOSE,
         index_id='BRTI',
         method='GET',
         url=URL,
         http_status=200,
         original_complete=True,
         requested_at=(
            NOW - timedelta(
                seconds=3
            )
        ).isoformat(
        ),
         received_at=(
            NOW - timedelta(
                milliseconds=500
            )
        ).isoformat(
        ),
         recorded_at=NOW.isoformat(
        )
    )
    return (body, plan, rec)

def prepare(body, plan, rec, *, as_of=NOW, context=None):
    body_raw = body if isinstance(body, bytes) else enc(body)
    plan_raw = plan if isinstance(plan, bytes) else enc(plan)
    receipt = enc(dict(rec, source_sha256=digest(body_raw), protocol_sha256=digest(plan_raw)))
    return prepare_cf_selection_level(
        body=body_raw,
         receipt=receipt,
         protocol_original=plan_raw,
         protocol_sha256=digest(
            plan_raw
        ),
         as_of=as_of,
         **CONTEXT if context is None else context
    )

def fixture(rows=None, cursor=''):
    event = CONTEXT['expected_event_ticker']
    if rows is None:
        rows = [
            dict(
                ticker=event + '-B' + str(
                    i
                ),
                 event_ticker=event,
                 market_type='binary',
                 status='active',
                 close_time=(
                    NOW + timedelta(
                        minutes=30
                    )
                ).isoformat(
                ),
                 strike_type='between',
                 floor_strike=str(
                    i
                ),
                 cap_strike=str(
                    i + 1
                ),
                 yes_bid_dollars='0.01',
                 yes_ask_dollars='0.99'
            ) for i in (
                77118,
                 77117,
                 77116,
                 77115
            )
        ]
    original = OriginalBook(
        event_discovery_url(
            'KXBTC',
             event
        ),
         enc(
            dict(
                markets=rows,
                 cursor=cursor
            )
        ),
         NOW - timedelta(
            seconds=4
        )
    )
    receipt = enc(
        dict(
            url=original.url,
             method='GET',
             http_status=200,
             original_complete=True,
             source_sha256=original.sha256,
             requested_at=(
                NOW - timedelta(
                    seconds=5
                )
            ).isoformat(
            ),
             received_at=original.received_at.isoformat(
            )
        )
    )
    plan = enc(
        dict(
            schema=SCHEMA,
             asset='BTC',
             event_ticker=event,
             source_commit='d' * 40,
             declared_at=(
                NOW - timedelta(
                    seconds=12
                )
            ).isoformat(
            ),
             not_before=(
                NOW - timedelta(
                    seconds=11
                )
            ).isoformat(
            ),
             not_after=(
                NOW + timedelta(
                    seconds=120
                )
            ).isoformat(
            ),
             policy=POLICY,
             max_contracts=3,
             purpose=PURPOSE,
             paper_eligible=False,
             execution_authority=False
        )
    )
    return ControlCentralSelectionInputs(
        CurrentEventDiscovery(
            original,
             receipt,
             event
        ),
         prepare(
            *inputs(
                1
            )
        ),
         plan,
         digest(
            plan
        ),
         'd' * 40,
         NOW
    )

def select(value, at=NOW):
    return select_control_and_central(value, asset='BTC', assessed_at=at)

class SelectionTests(unittest.TestCase):

    def test_sol_actual_slot04_range_shape(self):
        value = fixture()
        event = 'KXSOLE-26SEP1311'
        rows = json.loads(value.discovery.original.payload)['markets'][:2]
        for i, row in enumerate(rows):
            row.update(
                ticker=event + '-B' + str(
                    i
                ),
                 event_ticker=event,
                 floor_strike='99.25' if i == 0 else '100.25',
                 cap_strike='99.4999' if i == 0 else '100.4999'
            )
        original = replace(
            value.discovery.original,
             url=event_discovery_url(
                'KXSOLE',
                 event
            ),
             payload=enc(
                dict(
                    markets=rows,
                     cursor=''
                )
            )
        )
        receipt = json.loads(value.discovery.receipt)
        receipt.update(url=original.url, source_sha256=original.sha256)
        body, lp, rec = inputs(1)
        body['data']['payload'][0]['value'] = '100.24'
        lp.update(asset='SOL', index_id='SOLUSD_RTI', event_ticker=event)
        rec.update(index_id='SOLUSD_RTI', url=URL.replace('BRTI', 'SOLUSD_RTI'))
        level = prepare(
            body,
             lp,
             rec,
             context=dict(
                CONTEXT,
                 expected_asset='SOL',
                 expected_event_ticker=event
            )
        )
        plan = json.loads(value.protocol_original)
        plan.update(asset='SOL', event_ticker=event)
        raw = enc(plan)
        result = select_control_and_central(
            replace(
                value,
                 discovery=CurrentEventDiscovery(
                    original,
                     enc(
                        receipt
                    ),
                     event
                ),
                 level=level,
                 protocol_original=raw,
                 protocol_sha256=digest(
                    raw
                )
            ),
             asset='SOL',
             assessed_at=NOW
        )
        self.assertEqual(result['selection_manifest']['roles']['CENTRAL_1'], event + '-B1')

    def test_control_plus_two_distinct_central(self):
        value = fixture()
        rows = json.loads(value.discovery.original.payload)['markets']
        rows[0].update(yes_bid_dollars='0.49', yes_ask_dollars='0.51')
        result = select(fixture(rows))['selection_manifest']
        self.assertEqual(len(result['selected']), 3)
        self.assertTrue(result['roles']['EVENT_METADATA_CONTROL'].endswith('77118'))
        self.assertTrue(result['roles']['CENTRAL_1'].endswith('77116'))
        self.assertFalse(result['paper_eligible'])
        self.assertIsNone(result['dependencies']['independent_n'])

    def test_deduplicate_without_replacement(self):
        value = fixture()
        rows = json.loads(value.discovery.original.payload)['markets']
        rows[2].update(yes_bid_dollars='0.49', yes_ask_dollars='0.51')
        result = select(fixture(rows))['selection_manifest']
        self.assertEqual(len(result['selected']), 2)
        self.assertEqual(result['roles']['EVENT_METADATA_CONTROL'], result['roles']['CENTRAL_1'])

    def test_order_invariant_and_no_book_input(self):
        value = fixture()
        rows = json.loads(value.discovery.original.payload)['markets']
        a = select(value)['selection_manifest']
        b = select(fixture(list(reversed(rows))))['selection_manifest']
        self.assertEqual(a['roles'], b['roles'])
        self.assertEqual(a['scores'], b['scores'])
        self.assertFalse(a['liquidity_verified'])

    def test_tie_ticker_and_geometry_exclusion(self):
        rows = json.loads(fixture().discovery.original.payload)['markets']
        for r in rows:
            r.update(floor_strike='77116', cap_strike='77117')
        rows[0]['floor_strike'] = 'NaN'
        m = select(fixture(rows))['selection_manifest']
        self.assertEqual(len(m['exclusions']), 1)
        self.assertTrue(m['roles']['CENTRAL_1'].endswith('77115'))

    def test_invalid_metadata_cannot_win_control_but_remains_central(self):
        rows = json.loads(fixture().discovery.original.payload)['markets']
        for r in rows:
            r['yes_bid_dollars'] = 'NaN'
        m = select(fixture(rows))['selection_manifest']
        self.assertIsNone(m['roles']['EVENT_METADATA_CONTROL'])
        self.assertEqual(len(m['selected']), 2)

    def test_protocol_rejections(self):
        for key, value in [
            (
                'max_contracts',
                 True
            ),
             (
                'max_contracts',
                 4
            ),
             (
                'asset',
                 'SOL'
            ),
             (
                'source_commit',
                 'e' * 40
            ),
             (
                'policy',
                 'old'
            ),
             (
                'unknown',
                 1
            )
        ]:
            with self.subTest(key=key, value=value):
                v = fixture()
                plan = json.loads(v.protocol_original)
                plan[key] = value
                raw = enc(plan)
                with self.assertRaises(ValueError):
                    select(replace(v, protocol_original=raw, protocol_sha256=digest(raw)))

    def test_original_and_derived_identity(self):
        v = fixture()
        for changed in [
            replace(
                v,
                 level=replace(
                    v.level,
                     level=Decimal(
                        1
                    )
                )
            ),
             replace(
                v,
                 expected_source_commit='e' * 40
            )
        ]:
            with self.assertRaises(ValueError):
                select(changed)
        with self.assertRaises(ValueError):
            select(fixture(cursor='next'))

    def test_duplicate_and_mixed_event(self):
        rows = json.loads(fixture().discovery.original.payload)['markets']
        with self.assertRaises(ValueError):
            select(fixture(rows + [rows[0]]))
        rows[0]['event_ticker'] = 'KXBTC-OTHER'
        with self.assertRaises(ValueError):
            select(fixture(rows))

    def test_clock_and_stale_rejections(self):
        v = fixture()
        with self.assertRaises(ValueError):
            select(v, NOW + timedelta(seconds=61))
        plan = json.loads(v.protocol_original)
        plan['declared_at'] = NOW.isoformat()
        raw = enc(plan)
        with self.assertRaises(ValueError):
            select(replace(v, protocol_original=raw, protocol_sha256=digest(raw)))

    def test_hostile_decimal_context(self):
        v = fixture()
        expected = select(v)['selection_manifest']['roles']
        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = True
            self.assertEqual(select(v)['selection_manifest']['roles'], expected)

    def test_empty_universe_and_singleton(self):
        self.assertEqual(select(fixture([]))['selected_markets'], 0)
        row = json.loads(fixture().discovery.original.payload)['markets'][:1]
        m = select(fixture(row))['selection_manifest']
        self.assertEqual(len(m['selected']), 1)
        self.assertIsNone(m['roles']['CENTRAL_2'])

    def test_event_row_cap(self):
        rows = json.loads(fixture().discovery.original.payload)['markets']
        many = [
            dict(
                rows[
                    0
                ],
                 ticker=CONTEXT[
                    'expected_event_ticker'
                ] + '-B' + str(
                    i
                )
            ) for i in range(
                201
            )
        ]
        with self.assertRaises(ValueError):
            select(fixture(many))
if __name__ == '__main__':
    unittest.main()
