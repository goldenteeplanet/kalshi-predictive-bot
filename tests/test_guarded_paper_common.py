import ast
from pathlib import Path

from kalshi_predictor import phase3bb_r33_cloud_paper_only_operations_readiness as readiness
from kalshi_predictor.guarded_paper_common import int_or_zero
from kalshi_predictor.learning import cycle, diagnostics
from kalshi_predictor.paper import ledger
from kalshi_predictor.position_sizing import service
from kalshi_predictor.wrapper_inventory import build_wrapper_inventory


def test_integer_parser_compatibility_aliases_use_guarded_common() -> None:
    assert cycle._int is int_or_zero
    assert diagnostics._int is int_or_zero
    assert readiness._int_value is int_or_zero
    assert [int_or_zero(value) for value in (None, "", "4", "invalid")] == [0, 0, 4, 0]


def test_guarded_detector_keeps_stateful_and_cross_lane_helpers_isolated() -> None:
    payload = build_wrapper_inventory()

    assert payload["guarded_paper_eligible_duplicate_helpers"] == []
    isolated_members = {
        f"{member['module']}::{member['function']}"
        for group in payload["guarded_paper_isolated_duplicate_helpers"]
        for member in group["members"]
    }
    assert "paper/ledger.py::_pending_position" in isolated_members
    assert "position_sizing/service.py::_pending_position" in isolated_members
    assert ledger._pending_position is not service._pending_position

    cross_lane = payload["guarded_paper_cross_lane_duplicates"]
    assert cross_lane[0]["disposition"] == "not_merged_cross_lane"
    assert {member["module"] for member in cross_lane[0]["members"]} == {
        "paper_trading_gap.py",
        "phase3bb_r3_activation.py",
    }


def test_guarded_common_has_only_guarded_paper_importers() -> None:
    root = Path(__file__).parents[1] / "src" / "kalshi_predictor"
    importers = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if path.name != "guarded_paper_common.py"
        and any(
            isinstance(node, ast.ImportFrom)
            and node.module == "kalshi_predictor.guarded_paper_common"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    }
    assert importers == {
        "learning/cycle.py",
        "learning/diagnostics.py",
        "phase3bb_r33_cloud_paper_only_operations_readiness.py",
    }
