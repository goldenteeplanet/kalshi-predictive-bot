import unittest
from datetime import UTC, datetime, timedelta

from kalshi_predictor.crypto.prospective_dependency_graph import dependency_graph


def row(n, hours=0):
    start = datetime(2026, 9, 14, tzinfo=UTC) + timedelta(hours=hours)
    return dict(decision_id=f"{n:064x}", ticker=f"KXBTC-E{n}-B1", event=f"KXBTC-E{n}",
                asset="BTC", settlement_event=f"settlement-{n}", source_start=start.isoformat(),
                source_end=(start+timedelta(minutes=60)).isoformat(),
                recorded_at=(start+timedelta(minutes=61)).isoformat(),
                target_at=(start+timedelta(minutes=66)).isoformat(),
                input_hashes=(f"{n+1000:064x}",), role="DEVELOPMENT")


class DependencyTests(unittest.TestCase):
    def test_touching_same_asset_window_is_shared(self):
        result = dependency_graph((row(1), row(2, 1)))
        self.assertEqual(result["dependency_group_n"], 1)
        self.assertIn("SAME_ASSET_OVERLAPPING_CF_WINDOW", result["relationships"][0]["reasons"])

    def test_same_asset_alone_not_independence_or_automatic_union(self):
        result = dependency_graph((row(1), row(2, 3)))
        self.assertEqual(result["dependency_group_n"], 2)
        self.assertEqual(len(result["same_asset_potential_dependence"]["BTC"]), 2)
        self.assertIsNone(result["effective_independent_n"])

    def test_each_explicit_shared_identity_joins(self):
        for key in ("ticker", "event", "settlement_event", "input_hashes"):
            a, b = row(1), row(2, 3)
            b[key] = a[key]
            self.assertEqual(dependency_graph((a, b))["dependency_group_n"], 1)

    def test_simultaneous_cross_asset_target_joins(self):
        a, b = row(1), row(2)
        b["asset"] = "ETH"
        result = dependency_graph((a, b))
        self.assertEqual(result["dependency_group_n"], 1)
        self.assertEqual(result["relationships"][0]["reasons"], ["SAME_TARGET_TIME"])

    def test_transitive_cross_role_component_and_order_invariance(self):
        a, b, c = row(1), row(2, 3), row(3, 6)
        b["event"] = a["event"]
        c["input_hashes"] = b["input_hashes"]
        c["role"] = "FUTURE_HOLDOUT"
        forward = dependency_graph((a, b, c))
        self.assertEqual(forward, dependency_graph((c, a, b)))
        self.assertTrue(forward["components"][0]["cross_role_excluded"])
        self.assertEqual(len(forward["components"][0]["members"]), 3)

    def test_duplicate_outcome_extra_field_and_bad_clocks_rejected(self):
        with self.assertRaises(ValueError):
            dependency_graph((row(1), row(1)))
        for key, value in [("outcome", 1), ("target_at", "2026-09-13T00:00:00Z"),
                           ("source_start", "2026-09-14T00:00:00"), ("asset", "OTHER")]:
            item = row(1)
            item[key] = value
            with self.assertRaises(ValueError):
                dependency_graph((item,))

    def test_bound_and_hash_validation(self):
        with self.assertRaises(ValueError):
            dependency_graph(tuple(row(n) for n in range(513)))
        item = row(1)
        item["input_hashes"] = ("bad",)
        with self.assertRaises(ValueError):
            dependency_graph((item,))


if __name__ == "__main__":
    unittest.main()
