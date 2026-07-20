import copy
import json
from pathlib import Path

from kalshi_predictor.benchmarking.offline_export_join import (
    build_offline_exact_export_join_preview,
    join_exact_runtime_exports,
    write_offline_exact_export_join_preview,
)


FIXTURES = Path(__file__).parent / "fixtures/pmb34b/offline_runtime_exports.json"


def _crypto():
    return copy.deepcopy(json.loads(FIXTURES.read_text())[0])


def test_pmb34b_joins_all_categories_and_feeds_pmb34a():
    report = build_offline_exact_export_join_preview(FIXTURES)
    assert report["summary"]["joined"] == 3
    assert report["summary"]["required_categories_pass"] is True
    assert {row["category"] for row in report["rows"] if row["joined"]} == {
        "crypto", "weather", "sports"
    }
    crypto = next(row for row in report["rows"] if row["category"] == "crypto")
    assert crypto["mapping_provenance"]["forecast_bias"]["candidate_forecast_id"] == 301
    assert crypto["shadow_preview"]["runtime_effect"] == "NONE_DISABLED_SHADOW"
    assert report["summary"]["pmb35_deployment_unblocked"] is False


def test_pmb34b_rejects_stale_mismatched_missing_and_ambiguous_sources():
    stale = _crypto()
    stale["forecasts"][0]["generated_at"] = "2026-07-18T03:00:00Z"
    assert any(code.startswith("SOURCE_STALE:candidate_forecast") for code in join_exact_runtime_exports(stale)["diagnostics"])

    mismatch = _crypto()
    mismatch["books"][0]["ticker"] = "WRONG"
    assert "IDENTITY_TICKER_MISMATCH:current_book" in join_exact_runtime_exports(mismatch)["diagnostics"]

    missing = _crypto()
    missing["forecasts"] = missing["forecasts"][:1]
    assert "JOIN_MISSING:reference_forecast:291" in join_exact_runtime_exports(missing)["diagnostics"]

    ambiguous = _crypto()
    ambiguous["books"].append(copy.deepcopy(ambiguous["books"][0]))
    assert "JOIN_AMBIGUOUS:current_book:401" in join_exact_runtime_exports(ambiguous)["diagnostics"]


def test_pmb34b_rejects_future_and_target_time_mismatch():
    future = _crypto()
    future["books"][0]["captured_at"] = "2026-07-18T05:01:00Z"
    assert "SOURCE_FROM_FUTURE:current_book" in join_exact_runtime_exports(future)["diagnostics"]

    mismatch = _crypto()
    mismatch["forecasts"][1]["target_time"] = "2026-07-18T06:00:00Z"
    assert "IDENTITY_TARGET_TIME_MISMATCH:reference_forecast" in join_exact_runtime_exports(mismatch)["diagnostics"]


def test_pmb34b_is_deterministic_local_and_disabled(tmp_path):
    first = json.loads(write_offline_exact_export_join_preview(FIXTURES, tmp_path / "a").read_text())
    second = json.loads(write_offline_exact_export_join_preview(FIXTURES, tmp_path / "b").read_text())
    assert first == second
    assert first["database_writes"] == 0
    assert first["cloud_access"] is False
    assert first["execution_enabled"] is False
    assert first["runtime_policy_changed"] is False
