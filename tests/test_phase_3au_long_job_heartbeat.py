from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3au import (
    LongJobHeartbeat,
    load_latest_long_job_status,
    write_phase3au_report,
)
from kalshi_predictor.phase3y import run_link_remediation


def test_phase3au_heartbeat_writes_status_and_checkpoint(tmp_path) -> None:
    heartbeat = LongJobHeartbeat(
        "link-remediate",
        output_dir=Path(tmp_path),
        checkpoint_every=2,
    )

    heartbeat.emit(
        stage="SPORTS_LINK",
        processed=2,
        total=10,
        current_item="KXTEST",
        message="PROGRESS",
    )
    status = load_latest_long_job_status(output_dir=Path(tmp_path), stale_after_seconds=99999)

    assert heartbeat.heartbeat_path.exists()
    assert heartbeat.checkpoint_path.exists()
    assert status["status"] == "ACTIVE"
    assert status["heartbeat"]["stage"] == "SPORTS_LINK"
    assert status["heartbeat"]["processed"] == 2


def test_phase3au_report_renders(tmp_path) -> None:
    LongJobHeartbeat("link-remediate", output_dir=Path(tmp_path)).emit(
        stage="COMPLETE",
        processed=10,
        total=10,
        message="done",
        force_checkpoint=True,
    )

    path = write_phase3au_report(output_dir=Path(tmp_path), stale_after_seconds=99999)

    text = path.read_text(encoding="utf-8")
    assert "Phase 3AU Long Job Heartbeat" in text
    assert "COMPLETE" in text


def test_link_remediation_writes_phase3au_heartbeat(tmp_path) -> None:
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3au.db'}")
    session_factory = get_session_factory(engine)
    heartbeat_dir = Path(tmp_path) / "phase3au"

    with session_factory() as session:
        result = run_link_remediation(
            session,
            settings=Settings(),
            limit=1,
            heartbeat_dir=heartbeat_dir,
            progress_every=1,
            checkpoint_every=1,
        )

    status = load_latest_long_job_status(output_dir=heartbeat_dir, stale_after_seconds=99999)
    assert result.heartbeat_path == str(heartbeat_dir / "link_remediate_heartbeat.json")
    assert status["heartbeat"]["stage"] == "COMPLETE"
    assert status["heartbeat"]["extra"]["total_links"] == result.total_links


def test_phase3au_cli_help() -> None:
    runner = CliRunner()
    for command in ("phase3au-status", "phase3au-report"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output

    link_help = runner.invoke(app, ["link-remediate", "--help"])
    assert link_help.exit_code == 0
    assert "stop-after-minutes" in link_help.output
    assert "checkpoint-every" in link_help.output
