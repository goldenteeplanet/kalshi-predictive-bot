from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.data.schema import Base
from kalshi_predictor.system_lanes import (
    CURRENT_RESEARCH,
    GH2_SOAK,
    GUARDED_PAPER,
    HISTORICAL_REPLAY,
    build_lane_contract,
    command_owner,
    table_owner,
)


def test_every_table_has_exactly_one_canonical_lane() -> None:
    payload = build_lane_contract()
    table_resources = [item for item in payload["resources"] if item["kind"] == "table"]

    assert payload["status"] == "READY"
    assert payload["collisions"] == []
    assert {item["name"] for item in table_resources} == set(Base.metadata.tables)
    assert all(item["lane"] in payload["lanes"] for item in table_resources)


def test_high_risk_resources_have_expected_owners() -> None:
    assert table_owner("market_rankings") == CURRENT_RESEARCH
    assert table_owner("forecasts") == CURRENT_RESEARCH
    assert table_owner("settlements") == HISTORICAL_REPLAY
    assert table_owner("backtest_runs") == HISTORICAL_REPLAY
    assert table_owner("paper_orders") == GUARDED_PAPER
    assert table_owner("advanced_risk_decisions") == GUARDED_PAPER
    assert table_owner("runtime_provenance_events") == GH2_SOAK

    assert command_owner("phase3bc-r3-active-crypto-refresh") == CURRENT_RESEARCH
    assert command_owner("crypto-backtest") == HISTORICAL_REPLAY
    assert command_owner("sync-settlements") == HISTORICAL_REPLAY
    assert command_owner("phase3aa-realize") == HISTORICAL_REPLAY
    assert command_owner("feature-discovery-run") == HISTORICAL_REPLAY
    assert command_owner("synthetic-markets-run") == HISTORICAL_REPLAY
    assert command_owner("rl-train") == HISTORICAL_REPLAY
    assert command_owner("weather-one-contract-paper-activation") == GUARDED_PAPER
    assert command_owner("gh2-single-writer-decision-refresh") == GH2_SOAK


def test_all_registered_commands_have_one_owner() -> None:
    payload = build_lane_contract()
    commands = [item for item in payload["resources"] if item["kind"] == "command"]

    assert len(commands) > 100
    assert len({item["name"] for item in commands}) == len(commands)
    assert all(item["lane"] in payload["lanes"] for item in commands)


def test_system_lanes_cli_writes_collision_free_contract(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["system-lanes", "--output-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Status: READY" in result.output
    assert "Ownership collisions: 0" in result.output
    assert (tmp_path / "canonical_lanes.json").is_file()
    assert (tmp_path / "canonical_lanes.md").is_file()
