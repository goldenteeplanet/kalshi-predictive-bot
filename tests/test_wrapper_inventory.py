from pathlib import Path

from kalshi_predictor import (
    phase3ag_crypto,
    phase3ar,
    phase3bc,
    phase3bc_r3,
    phase3bc_r4,
    phase3bc_r5,
    phase3bc_r5_alignment,
    phase3bc_r6,
    phase3bc_r7,
    phase3bc_r16,
    phase3bc_r17,
)
from kalshi_predictor import (
    phase3bb_r45_weather_freshness_to_ranking_impact as weather_r45,
)
from kalshi_predictor import (
    phase3bb_r50_weather_post_link_ranking_fast_lane_recheck as weather_r50,
)
from kalshi_predictor import (
    phase3bb_r51_weather_ranking_path_repair as weather_r51,
)
from kalshi_predictor.current_research_common import (
    crypto_candidate_sort_key,
    decode_list,
    format_cents,
    int_from_float_or_none,
    int_or_none,
    latest_crypto_v2_forecast,
    latest_market_snapshot,
    latest_risk_decisions_by_ticker,
    markdown_cell,
    read_json,
    read_json_required,
)
from kalshi_predictor.phase3bb_weather_common import (
    check,
    first_line,
    legacy_check,
    tail,
    write_legacy_probe_csv,
    write_probe_csv,
    write_rows_csv,
    write_sorted_rows_csv,
)
from kalshi_predictor.system_lanes import CURRENT_RESEARCH, GH2_SOAK, command_owner
from kalshi_predictor.wrapper_inventory import build_wrapper_inventory, write_wrapper_inventory


def test_weather_chain_is_exclusively_current_research() -> None:
    payload = build_wrapper_inventory()

    assert payload["status"] == "READY"
    assert payload["violations"] == []
    assert len(payload["phase3bb_weather_chain"]) == 20
    assert {row["lane"] for row in payload["phase3bb_weather_chain"]} == {CURRENT_RESEARCH}
    assert all(row["writer_callable"] != "none" for row in payload["phase3bb_weather_chain"])
    assert command_owner("wrapper-inventory") == GH2_SOAK


def test_inventory_writes_canonical_evidence(tmp_path: Path) -> None:
    payload = write_wrapper_inventory(
        json_path=tmp_path / "wrapper_inventory.json",
        markdown_path=tmp_path / "wrapper_inventory.md",
    )

    assert payload["status"] == "READY"
    assert (tmp_path / "wrapper_inventory.json").is_file()
    report = (tmp_path / "wrapper_inventory.md").read_text(encoding="utf-8")
    assert "No Phase 3BB weather command was removed or aliased" in report
    assert "phase3bb_weather_common" in report
    assert "current_research_common" in report


def test_crypto_compatibility_aliases_use_canonical_exact_helpers() -> None:
    assert phase3ag_crypto._latest_snapshot is latest_market_snapshot
    assert phase3ar._latest_snapshot is latest_market_snapshot
    assert phase3ag_crypto._latest_forecast is latest_crypto_v2_forecast
    assert phase3ar._latest_forecast is latest_crypto_v2_forecast
    assert phase3bc_r3._read_json is read_json
    assert phase3bc_r4._read_json is read_json
    assert phase3bc_r5._read_json is read_json
    assert phase3bc_r16._read_json is read_json_required
    assert phase3bc_r17._read_json is read_json_required
    assert phase3bc_r5._latest_forecast is latest_crypto_v2_forecast
    assert phase3bc_r7._latest_forecast is latest_crypto_v2_forecast
    assert phase3bc_r4._latest_risk_decisions_by_ticker is latest_risk_decisions_by_ticker
    assert phase3bc_r5._latest_risk_decisions_by_ticker is latest_risk_decisions_by_ticker
    assert phase3bc_r4._diagnostic_sort_key is crypto_candidate_sort_key
    assert phase3bc_r5._candidate_sort_key is crypto_candidate_sort_key
    assert phase3bc_r4._decode_list is decode_list
    assert phase3bc_r5._decode_list is decode_list
    assert phase3bc_r4._cents is format_cents
    assert phase3bc_r5._cents is format_cents
    assert phase3bc._cell is markdown_cell
    assert phase3bc_r4._cell is markdown_cell
    assert phase3bc_r16._cell is markdown_cell
    assert phase3bc_r17._cell is markdown_cell
    assert phase3ar._history_int is int_from_float_or_none
    assert phase3bc_r6._int_or_none is int_from_float_or_none
    assert weather_r45._int_or_none is int_or_none
    assert phase3bc_r5_alignment._int_value is int_or_none
    assert weather_r50._check is legacy_check
    assert weather_r51._check is legacy_check
    assert weather_r50._tail is tail
    assert weather_r51._tail is tail
    assert weather_r50._write_probe_csv is write_legacy_probe_csv
    assert weather_r51._write_probe_csv is write_legacy_probe_csv
    assert weather_r50._write_rows_csv is write_sorted_rows_csv
    assert weather_r51._write_rows_csv is write_sorted_rows_csv


def test_common_weather_helpers_preserve_output_contracts(tmp_path: Path) -> None:
    assert check("ready", 1, "ok") == {"check": "ready", "passed": True, "detail": "ok"}
    assert first_line("\n  first \nsecond") == "first"

    rows_path = tmp_path / "rows.csv"
    write_rows_csv(rows_path, [{"a": 1, "b": 2}, {"b": 3, "c": 4}])
    assert rows_path.read_text(encoding="utf-8").splitlines() == ["a,b,c", "1,2,", ",3,4"]

    probes_path = tmp_path / "probes.csv"
    write_probe_csv(probes_path, [{"name": "probe", "ok": True}])
    header = probes_path.read_text(encoding="utf-8").splitlines()[0]
    assert header == ("name,ok,exit_code,duration_seconds,timed_out,stdout_excerpt,stderr_excerpt")
