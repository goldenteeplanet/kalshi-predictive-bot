from __future__ import annotations

from kalshi_predictor.benchmarking.shadow_census import build_shadow_census


def _cycle(index: int, **updates):
    cycle = {
        "cycle_id": f"cycle-{index}",
        "policy_enabled": False,
        "runtime_policy_changed": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "summary": {"all_attribution_complete": True},
        "rows": [
            {
                "category": "crypto",
                "baseline": {"eligible": True, "requested_capital": "10"},
                "shadow": {"eligible": True, "allocated_capital": "9.50", "blocker": None},
            },
            {
                "category": "weather",
                "baseline": {"eligible": True, "requested_capital": "10"},
                "shadow": {
                    "eligible": False,
                    "allocated_capital": "0",
                    "blocker": "STRESS_BUFFER_EXCEEDED",
                },
            },
        ],
    }
    cycle.update(updates)
    return cycle


def test_three_disabled_shadow_cycles_pass() -> None:
    report = build_shadow_census([_cycle(1), _cycle(2), _cycle(3)])
    assert report["status"] == "PASSED"
    assert report["counts"]["shadow_rejections"] == 3
    assert report["capital"]["shadow_allocated"] == "28.50"


def test_fewer_than_three_cycles_fails_closed() -> None:
    report = build_shadow_census([_cycle(1), _cycle(2)])
    assert report["gates"]["minimum_distinct_cycles"] is False


def test_duplicate_cycles_fail_closed() -> None:
    report = build_shadow_census([_cycle(1), _cycle(1), _cycle(2)])
    assert report["gates"]["minimum_distinct_cycles"] is False


def test_activation_or_execution_fails_closed() -> None:
    report = build_shadow_census(
        [_cycle(1), _cycle(2), _cycle(3, policy_enabled=True, execution_enabled=True)]
    )
    assert report["status"] == "FAILED"
    assert report["gates"]["policy_disabled"] is False
    assert report["gates"]["execution_disabled"] is False


def test_incomplete_attribution_fails_closed() -> None:
    bad = _cycle(3)
    bad["summary"]["all_attribution_complete"] = False
    report = build_shadow_census([_cycle(1), _cycle(2), bad])
    assert report["gates"]["complete_attribution"] is False


def test_report_is_deterministic() -> None:
    cycles = [_cycle(1), _cycle(2), _cycle(3)]
    assert build_shadow_census(cycles) == build_shadow_census(cycles)
