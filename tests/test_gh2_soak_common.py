import ast
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from kalshi_predictor import phase_gh2
from kalshi_predictor.gh2_soak_common import as_utc, json_safe
from kalshi_predictor.live_readiness import contracts as readiness_contracts
from kalshi_predictor.roadmap import runtime_reports
from kalshi_predictor.system_certification import contracts as certification_contracts
from kalshi_predictor.wrapper_inventory import build_wrapper_inventory


def test_gh2_pure_helper_aliases_preserve_contracts() -> None:
    assert phase_gh2._aware is as_utc
    assert runtime_reports._as_utc is as_utc
    assert readiness_contracts._json_safe is json_safe
    assert certification_contracts._json_safe is json_safe

    naive = datetime(2026, 8, 22, 1, 0)
    offset = datetime(2026, 8, 21, 20, 0, tzinfo=timezone(-timedelta(hours=5)))
    assert as_utc(naive) == naive.replace(tzinfo=UTC)
    assert as_utc(offset) == naive.replace(tzinfo=UTC)
    assert json_safe({1: (Decimal("1.20"), naive)}) == {"1": ["1.20", naive.isoformat()]}


def test_gh2_detector_has_no_remaining_eligible_duplicates() -> None:
    payload = build_wrapper_inventory()

    assert payload["status"] == "READY"
    assert payload["gh2_soak_eligible_duplicate_helpers"] == []
    policy = payload["gh2_soak_isolation_policy"]
    assert {"scheduler", "timeout", "invariant", "stage", "timestamp", "counter"} <= set(
        policy["protected_tokens"]
    )
    assert phase_gh2._write_json.__module__ == "kalshi_predictor.phase_gh2"


def test_gh2_common_has_only_operational_soak_importers() -> None:
    root = Path(__file__).parents[1] / "src" / "kalshi_predictor"
    importers = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if path.name != "gh2_soak_common.py"
        and any(
            isinstance(node, ast.ImportFrom) and node.module == "kalshi_predictor.gh2_soak_common"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    }
    assert importers == {
        "live_readiness/contracts.py",
        "phase_gh2.py",
        "roadmap/runtime_reports.py",
        "system_certification/contracts.py",
    }
