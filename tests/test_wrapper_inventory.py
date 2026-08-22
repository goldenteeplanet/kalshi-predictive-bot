from pathlib import Path

from kalshi_predictor.phase3bb_weather_common import (
    check,
    first_line,
    write_probe_csv,
    write_rows_csv,
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
