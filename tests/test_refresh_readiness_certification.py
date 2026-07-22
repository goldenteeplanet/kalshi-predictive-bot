import json
import time
from pathlib import Path

from kalshi_predictor.ui.refresh_certification import certify_refresh_readiness
from kalshi_predictor.ui.refresh_readiness import build_refresh_readiness_dashboard

ROOT = Path(__file__).parents[1]


def test_refresh_readiness_certification_passes_read_only_contract() -> None:
    result = certify_refresh_readiness(ROOT)
    baseline = json.loads(
        (ROOT / "tests/fixtures/refresh_readiness_visual_baseline.json").read_text(
            encoding="utf-8"
        )
    )
    assert result["decision"] == "PASS"
    assert all(result["checks"].values())
    assert result["read_only"] is True
    assert result["template_sha256"] == baseline["template_sha256"]


def test_missing_artifact_dashboard_meets_performance_budget(tmp_path: Path) -> None:
    started = time.perf_counter()
    dashboard = build_refresh_readiness_dashboard(
        refresh_path=tmp_path / "missing.json",
        history_path=tmp_path / "missing.jsonl",
        manifest_path=tmp_path / "manifest.json",
        control_plane_root=tmp_path / "control_plane",
        cloud_status_path=tmp_path / "cloud.json",
    )
    elapsed = time.perf_counter() - started
    assert dashboard["source"]["state"] == "NO_SOURCE_DATA"
    assert elapsed < 0.25


def test_cloud_collector_is_read_only_and_checksummed() -> None:
    script = (ROOT / "scripts/cloud/collect-refresh-readiness-status.sh").read_text(
        encoding="utf-8"
    )
    assert "systemctl is-active" in script
    assert "hashlib.sha256" in script
    assert "systemctl start" not in script
    assert "systemctl stop" not in script
    assert "paper-order" not in script.lower()
