from pathlib import Path

from fastapi.testclient import TestClient

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.ui.accessibility_certification import build_accessibility_certification
from kalshi_predictor.ui.app import create_app


ROOT = Path(__file__).resolve().parents[1]


def test_accessibility_and_responsive_certification_passes() -> None:
    report = build_accessibility_certification(ROOT)
    assert report["status"] == "PASSED", [name for name, passed in report["checks"].items() if not passed]
    assert all(report["checks"].values())
    assert report["minimum_contrast_ratio"] >= 4.5
    assert report["viewports"] == [320, 375, 700, 1100, 1440]
    assert report["cloud_access"] is False
    assert report["deployment_performed"] is False
    assert report["runtime_controls"] is False


def test_progress_page_exposes_accessible_polling_controls(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KALSHI_PROGRESS_SNAPSHOT_PATH", str((ROOT / "tests/fixtures/ui_obs1/progress_snapshot.json").resolve()))
    engine = init_db(f"sqlite:///{tmp_path / 'ui.db'}")
    client = TestClient(create_app(session_factory=get_session_factory(engine), settings=Settings()))
    page = client.get("/system/progress")
    assert page.status_code == 200
    assert 'aria-live="polite"' in page.text
    assert 'data-progress-poll-toggle' in page.text
    assert 'aria-pressed="false"' in page.text
    assert 'aria-busy="false"' in page.text
    assert 'id="progress-page-title"' in page.text


def test_status_contrast_pairs_meet_wcag_normal_text_target() -> None:
    report = build_accessibility_certification(ROOT)
    assert all(ratio >= 4.5 for ratio in report["contrast_ratios"].values())
