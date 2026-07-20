import json
from pathlib import Path

import pytest

from kalshi_predictor.ui.progress import build_progress_dashboard
from kalshi_predictor.ui.workstream_registry import load_workstream_registry, normalize_workstream_registry


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/ui_obs1/progress_snapshot.json"
REQUIRED_IDS = {"pmb", "prov", "nyc_weather", "gh_liquidity", "readiness", "backup", "scheduler"}


def test_registry_contains_every_required_lane_once() -> None:
    registry = load_workstream_registry()
    rows = registry["workstreams"]
    assert {row["id"] for row in rows} == REQUIRED_IDS
    assert len(rows) == len(REQUIRED_IDS)
    assert all(row["phase_prefixes"] for row in rows)


def test_snapshot_normalizes_to_complete_registry() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    normalized = normalize_workstream_registry(payload)
    assert normalized["coverage"] == {"required": 7, "reported": 7, "missing": 0, "complete": True}
    assert {row["id"] for row in normalized["workstreams"]} == REQUIRED_IDS
    backup = next(row for row in normalized["workstreams"] if row["id"] == "backup")
    scheduler = next(row for row in normalized["workstreams"] if row["id"] == "scheduler")
    assert backup["state"] == "PASSED"
    assert scheduler["state"] == "RUNNING"


def test_missing_and_invalid_status_fail_closed() -> None:
    normalized = normalize_workstream_registry({"workstreams":[{"id":"pmb","state":"MAGIC"}]})
    pmb = next(row for row in normalized["workstreams"] if row["id"] == "pmb")
    prov = next(row for row in normalized["workstreams"] if row["id"] == "prov")
    assert pmb["state"] == "BLOCKED"
    assert prov["state"] == "WAITING"
    assert prov["reported"] is False
    assert normalized["coverage"]["complete"] is False


def test_duplicate_registry_ids_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"workstreams":[{"id":"x","name":"X","phase_prefixes":["X-"]},{"id":"x","name":"Y","phase_prefixes":["Y-"]}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="IDS_INVALID"):
        load_workstream_registry(path)


def test_progress_dashboard_exposes_registry_coverage() -> None:
    progress = build_progress_dashboard(FIXTURE)
    assert progress["workstream_registry"]["schema_version"] == 1
    assert progress["workstream_registry"]["coverage"]["complete"] is True
    assert len(progress["workstreams"]) == 7
