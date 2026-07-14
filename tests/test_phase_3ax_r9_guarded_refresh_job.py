import json
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func, select
from typer.testing import CliRunner

from kalshi_predictor import phase3ax_r9
from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.schema import PaperOrder
from kalshi_predictor.phase3ax_r9 import write_phase3ax_r9_guarded_refresh_job_report
from kalshi_predictor.phase3bc_r6 import Phase3BCR6UnattendedStart


def test_phase3ax_r9_refuses_duplicate_r5_start(tmp_path, monkeypatch) -> None:
    reports_dir = tmp_path / "reports"
    r5_status = _r5_status(running=True, pid=5151)
    _patch_r9_dependencies(monkeypatch, reports_dir=reports_dir, status_payloads=[r5_status])

    def fail_start(**_kwargs):
        raise AssertionError("R9 must not start a duplicate R5 watcher")

    monkeypatch.setattr(phase3ax_r9, "start_phase3bc_r5_unattended_watch", fail_start)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        before_orders = session.scalar(select(func.count(PaperOrder.id)))
        artifacts = write_phase3ax_r9_guarded_refresh_job_report(
            session,
            output_dir=reports_dir / "phase3ax_r9",
            reports_dir=reports_dir,
            r5_output_dir=reports_dir / "phase3bc_r5",
            settings=Settings(opportunity_min_time_to_close_minutes=1),
            registered_commands={"phase3ax-r9-guarded-refresh-job", "phase3ax-gap-analysis"},
            start_if_needed=False,
        )
        after_orders = session.scalar(select(func.count(PaperOrder.id)))

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["status"] == "ALREADY_RUNNING_NO_DUPLICATE_STARTED"
    assert payload["summary"]["duplicate_refused"] is True
    assert payload["summary"]["r5_pid"] == 5151
    assert payload["start_action"]["start_attempted"] is False
    assert payload["summary"]["dashboard_truth_refreshed"] is True
    assert payload["summary"]["gap_analysis_refreshed"] is True
    assert before_orders == after_orders == 0


def test_phase3ax_r9_starts_r5_when_stopped_and_safe(tmp_path, monkeypatch) -> None:
    reports_dir = tmp_path / "reports"
    stopped_status = _r5_status(running=False, pid=None, guard_status="STOPPED_WITH_STALE_PID")
    running_status = _r5_status(running=True, pid=6262)
    _patch_r9_dependencies(
        monkeypatch,
        reports_dir=reports_dir,
        status_payloads=[stopped_status, running_status],
    )
    starts: list[dict[str, object]] = []

    def fake_start(**kwargs):
        starts.append(kwargs)
        return Phase3BCR6UnattendedStart(
            output_dir=kwargs["output_dir"],
            status="STARTED",
            pid=6262,
            started=True,
            pid_path=kwargs["output_dir"] / "phase3bc_r5_unattended_job.pid",
            metadata_path=kwargs["output_dir"] / "phase3bc_r5_unattended_job.json",
            stdout_path=kwargs["output_dir"] / "phase3bc_r5_unattended_stdout.log",
            stderr_path=kwargs["output_dir"] / "phase3bc_r5_unattended_stderr.log",
            command="kalshi-bot phase3bc-r5-crypto-freshness-watch",
            message="started",
        )

    monkeypatch.setattr(phase3ax_r9, "start_phase3bc_r5_unattended_watch", fake_start)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        artifacts = write_phase3ax_r9_guarded_refresh_job_report(
            session,
            output_dir=reports_dir / "phase3ax_r9",
            reports_dir=reports_dir,
            r5_output_dir=reports_dir / "phase3bc_r5",
            settings=Settings(opportunity_min_time_to_close_minutes=1),
            registered_commands={"phase3ax-r9-guarded-refresh-job", "phase3ax-gap-analysis"},
            cycles=4,
            interval_minutes=5,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert len(starts) == 1
    assert starts[0]["cycles"] == 4
    assert starts[0]["interval_minutes"] == 5
    assert payload["summary"]["status"] == "STARTED"
    assert payload["summary"]["r5_running"] is True
    assert payload["summary"]["r5_pid"] == 6262
    assert payload["start_action"]["result"]["started"] is True
    assert payload["next_codex_task"]["task_phase_name"] == (
        "Phase 3AX-R8 Dashboard Truth / Operator Workflow"
    )


def test_phase3ax_r9_refuses_unrelated_active_db_writer(tmp_path, monkeypatch) -> None:
    reports_dir = tmp_path / "reports"
    stopped_status = _r5_status(running=False, pid=None, guard_status="STOPPED_WITH_STALE_PID")
    _patch_r9_dependencies(
        monkeypatch,
        reports_dir=reports_dir,
        status_payloads=[stopped_status],
        writer_status={
            "safe_to_start_write": False,
            "current_writer_pid": 4444,
            "current_writer_command": "kalshi-bot build-sports-features",
        },
    )

    def fail_start(**_kwargs):
        raise AssertionError("R9 must not start R5 while another writer is active")

    monkeypatch.setattr(phase3ax_r9, "start_phase3bc_r5_unattended_watch", fail_start)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        artifacts = write_phase3ax_r9_guarded_refresh_job_report(
            session,
            output_dir=reports_dir / "phase3ax_r9",
            reports_dir=reports_dir,
            r5_output_dir=reports_dir / "phase3bc_r5",
            settings=Settings(opportunity_min_time_to_close_minutes=1),
            registered_commands={"phase3ax-r9-guarded-refresh-job", "phase3ax-gap-analysis"},
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["status"] == "NOT_STARTED_UNRELATED_DB_WRITER_ACTIVE"
    assert payload["summary"]["refused_reason"] == "UNRELATED_DB_WRITER_ACTIVE"
    assert payload["start_action"]["start_attempted"] is False


def test_phase3ax_r9_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3ax-r9-guarded-refresh-job", "--help"])
    assert result.exit_code == 0
    assert "phase3ax-r9-guarded-refresh-job" in result.output
    assert "--start-if-needed" in result.output
    assert "--status-only" in result.output


def test_phase3ax_r9_uses_refreshed_gap_analysis_next_task(tmp_path) -> None:
    gap_path = tmp_path / "reports" / "phase3ax" / "app_gap_analysis.json"
    gap_path.parent.mkdir(parents=True, exist_ok=True)
    gap_path.write_text(
        json.dumps(
            {
                "next_codex_task": {
                    "task_phase_name": (
                        "Phase 3AX-R7 Economic/News Parser Compatibility"
                    ),
                    "reason": "R8 is aligned.",
                    "problem_statement": "Repair economic/news compatibility.",
                    "acceptance_criteria": [],
                }
            }
        ),
        encoding="utf-8",
    )

    task = phase3ax_r9._next_codex_task(
        SimpleNamespace(app_gap_analysis_json_path=gap_path)
    )

    assert task["task_phase_name"] == "Phase 3AX-R7 Economic/News Parser Compatibility"


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ax_r9.db'}")
    return get_session_factory(engine)


def _r5_status(
    *,
    running: bool,
    pid: int | None,
    guard_status: str = "RUNNING",
) -> dict[str, object]:
    return {
        "generated_at": "2026-07-09T18:00:00+00:00",
        "process": {
            "status": "RUNNING" if running else "STOPPED",
            "phase3bc_r5_process_running": running,
            "phase3bc_r5_pids": [pid] if pid is not None else [],
        },
        "guard": {
            "status": guard_status,
            "pid": pid,
            "running": running,
            "should_stop": False,
        },
        "latest_watch_state": "WAITING_FOR_POSITIVE_EV",
        "latest_summary": {
            "watch_state": "WAITING_FOR_POSITIVE_EV",
            "primary_gap_after_refresh": "EV_NOT_POSITIVE",
            "positive_ev_rows": 0,
            "paper_ready_candidates": 0,
            "snapshot_stale_rows": 0,
            "forecast_stale_rows": 0,
        },
    }


def _patch_r9_dependencies(
    monkeypatch,
    *,
    reports_dir: Path,
    status_payloads: list[dict[str, object]],
    writer_status: dict[str, object] | None = None,
) -> None:
    status_queue = list(status_payloads)
    last_status: dict[str, object] = status_payloads[0]
    safe_writer = {
        "safe_to_start_write": True,
        "current_writer_pid": None,
        "current_writer_command": None,
    }

    monkeypatch.setattr(
        phase3ax_r9,
        "db_writer_monitor",
        lambda **_kwargs: dict(writer_status or safe_writer),
    )

    def fake_status_report(*, output_dir):
        nonlocal last_status
        payload = status_queue[0] if len(status_queue) == 1 else status_queue.pop(0)
        last_status = payload
        return _write_fake_artifact(output_dir / "phase3bc_r5_status.json", payload)

    def fake_guard_report(*, output_dir, stop_overrun=False):
        del stop_overrun
        payload = {"after": last_status}
        return _write_fake_artifact(output_dir / "phase3bc_r5_unattended_guard.json", payload)

    def fake_dashboard(*_args, output_dir, **_kwargs):
        return _write_fake_artifact(output_dir / "dashboard_truth.json", {"ok": True})

    def fake_gap(*_args, output_dir, **_kwargs):
        return _write_fake_artifact(output_dir / "app_gap_analysis.json", {"ok": True})

    monkeypatch.setattr(phase3ax_r9, "write_phase3bc_r5_status_report", fake_status_report)
    monkeypatch.setattr(
        phase3ax_r9,
        "write_phase3bc_r5_unattended_guard_report",
        fake_guard_report,
    )
    monkeypatch.setattr(phase3ax_r9, "write_phase3aw_dashboard_truth_report", fake_dashboard)
    monkeypatch.setattr(phase3ax_r9, "write_phase3ax_gap_analysis_report", fake_gap)
    reports_dir.mkdir(parents=True, exist_ok=True)


def _write_fake_artifact(path: Path, payload: dict[str, object]) -> SimpleNamespace:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return SimpleNamespace(
        json_path=path,
        markdown_path=path.with_suffix(".md"),
        dashboard_truth_path=path,
        app_gap_analysis_json_path=path,
    )
