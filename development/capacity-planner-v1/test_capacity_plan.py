"""New tests for the new planner; NOT RUN at resource-throttled authoring."""
import unittest
from capacity_plan import Charge, PAGE_PHASE_BYTES, MAX_BYTES, plan


class CapacityPlanTests(unittest.TestCase):
    def call(self, **changes):
        args = dict(filesystem='volume-1', epoch='epoch-new',
                    available_bytes=PAGE_PHASE_BYTES, observed_at='2026-09-14T13:00:00Z',
                    decision_at='2026-09-14T13:00:01Z', max_age_seconds=2,
                    protected_margin_bytes=0, pages=1, phases=1, charges=())
        args.update(changes)
        return plan(**args)

    def test_exact_fit_is_not_authority(self):
        r = self.call()
        self.assertEqual(r['status'], 'POSSIBLE_ARITHMETIC')
        self.assertEqual(r['remaining_bytes'], 0)
        for key in ('reservation_created', 'writer_authority', 'input_authenticated', 'allocation_enforced'):
            self.assertIs(r[key], False)

    def test_one_byte_short(self):
        self.assertEqual(self.call(available_bytes=PAGE_PHASE_BYTES-1)['shortfall_bytes'], 1)

    def test_retained_full_phase_case(self):
        # This is historical arithmetic, not a fresh admission observation.
        r = self.call(available_bytes=7395848192, pages=15, phases=2)
        self.assertEqual(r['requested_bytes'], 16106127360)
        self.assertEqual(r['shortfall_bytes'], 8710279168)

    def test_all_states_and_other_epoch_stay_charged(self):
        states = ('RESERVED', 'RUNNING', 'COMPLETED_CHARGED', 'AMBIGUOUS')
        rows = tuple(Charge(str(i), 'volume-1', 'epoch-old', s, 10) for i, s in enumerate(states))
        self.assertEqual(self.call(charges=rows)['shortfall_bytes'], 40)

    def test_duplicate_and_foreign_filesystem(self):
        row = Charge('id', 'volume-1', 'epoch-old', 'RUNNING', 10)
        for rows in ((row, row), (Charge('id', 'other-volume', 'old', 'RUNNING', 10),)):
            with self.assertRaises(ValueError): self.call(charges=rows)

    def test_stale_future_and_naive_refused(self):
        for t in ('2026-09-14T12:59:58Z', '2026-09-14T13:00:02Z', '2026-09-14T13:00:00'):
            with self.assertRaises(ValueError): self.call(observed_at=t)

    def test_numeric_and_collection_bounds(self):
        for changes in ({'available_bytes': True}, {'pages': 0}, {'pages': 121},
                        {'charges': []}, {'protected_margin_bytes': -1},
                        {'available_bytes': MAX_BYTES+1}, {'max_age_seconds': 0}):
            with self.assertRaises(ValueError): self.call(**changes)

    def test_overflow_and_no_release(self):
        with self.assertRaises(ValueError):
            self.call(charges=(Charge('r', 'volume-1', 'old', 'RUNNING', MAX_BYTES),))
        with self.assertRaises(ValueError):
            self.call(charges=(Charge('r', 'volume-1', 'old', 'RELEASED_WITH_PROOF', 1),))


if __name__ == '__main__':
    unittest.main()
