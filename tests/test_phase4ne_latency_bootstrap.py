from __future__ import annotations

from scripts.local.phase4ne_latency_bootstrap import COMPONENTS, bootstrap_latency_replay
from tests.test_phase4nd_microstructure_replay import _events, _order


def _observations(count=30):
    rows = []
    for index in range(count):
        burst = index % 7 == 0
        rows.append(
            {
                "observation_id": f"o-{index}",
                "regime": "burst" if burst else "normal",
                "decision_to_send_ms": 20 + (100 if burst else index % 5),
                "send_to_ack_ms": 30 + (150 if burst else index % 7),
                "market_data_age_ms": 10 + (200 if burst else index % 3),
                "processing_pause_ms": 5 + (50 if burst else 0),
                "scheduler_jitter_ms": 3 + (30 if burst else index % 2),
                "reconnect_delay_ms": 200 if burst else 0,
                "status": "ACK",
            }
        )
    return rows


def _run(rows=None, **kwargs):
    values = {
        "outcome": "yes",
        "sample_count": 100,
        "block_size": 3,
        "seed": 42,
        "minimum_observations": 20,
        "minimum_p99_net_pnl": "-1",
    }
    values.update(kwargs)
    return bootstrap_latency_replay(
        rows or _observations(), _events(), _order(max_book_age_ms=100), **values
    )


def test_block_bootstrap_is_seed_stable_and_reports_required_percentiles() -> None:
    first = _run()
    assert first == _run()
    assert first["joint_component_sampling"] is True
    assert first["readiness"] == "PASS"
    assert set(first["latency_percentiles_ms"]) == {"p50", "p90", "p95", "p99", "worst"}
    values = first["latency_percentiles_ms"]
    assert values["p50"] <= values["p90"] <= values["p95"] <= values["p99"] <= values["worst"]


def test_joint_rows_preserve_correlated_burst_components() -> None:
    result = _run()
    sources = {row["observation_id"]: row for row in _observations()}
    for path in result["paths"]:
        source = sources[path["source_observation_id"]]
        assert path["components"] == {key: source[key] for key in COMPONENTS}


def test_sparse_tail_support_refuses_readiness() -> None:
    result = _run(_observations(5), minimum_observations=20)
    assert result["readiness"] == "REFUSE"
    assert "TAIL_SUPPORT_INADEQUATE" in result["claim_errors"]


def test_outliers_and_multimodal_reconnect_regimes_reach_tail() -> None:
    result = _run()
    assert result["latency_percentiles_ms"]["worst"] > result["latency_percentiles_ms"]["p50"]
    assert any(path["regime"] == "burst" for path in result["paths"])
    assert result["distributions"]["stale_book_count"] > 0


def test_clock_skew_and_negative_latency_refuse() -> None:
    rows = _observations()
    rows[0]["scheduler_jitter_ms"] = -1
    result = _run(rows)
    assert result["verdict"] == "REFUSE"
    assert "CLOCK_SKEW_OR_NEGATIVE_LATENCY" in " ".join(result["errors"])


def test_timeouts_and_missing_acknowledgments_are_censored_not_dropped() -> None:
    rows = _observations()
    rows[0]["status"] = "TIMEOUT"
    rows[1]["status"] = "MISSING_ACK"
    result = _run(rows)
    assert result["censored_observation_count"] == 2
    assert any(path["censored"] for path in result["paths"])
    assert result["latency_percentiles_ms"]["worst"] >= 5000


def test_p99_economics_limit_can_fail_readiness() -> None:
    result = _run(minimum_p99_net_pnl="100")
    assert result["readiness"] == "REFUSE"
    assert "P99_EXECUTION_ECONOMICS_LIMIT_BREACHED" in result["claim_errors"]


def test_bootstrap_uncertainty_changes_with_seed_but_is_repeatable() -> None:
    first = _run(seed=1)
    second = _run(seed=2)
    assert first["bootstrap_sha256"] != second["bootstrap_sha256"]
    assert first == _run(seed=1)


def test_replay_distributions_include_fill_pnl_adverse_rejections_and_closure() -> None:
    distributions = _run()["distributions"]
    assert set(distributions) == {
        "fill_rate",
        "net_pnl",
        "adverse_selection",
        "stale_book_count",
        "rejection_count",
        "market_closure_count",
    }
    assert distributions["rejection_count"] >= distributions["stale_book_count"]


def test_bootstrap_has_no_order_or_execution_capability() -> None:
    result = _run()
    assert result["submitted_order"] is False
    safety = result["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
