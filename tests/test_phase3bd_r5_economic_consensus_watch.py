from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.economic.actuals import TRADING_ECONOMICS_ENV_NAMES
from kalshi_predictor.economic.consensus_watch import (
    release_window_state,
    run_phase3bd_r5_consensus_feed_watch,
    write_phase3bd_r5_consensus_feed_watch_report,
)
from kalshi_predictor.economic.repository import insert_economic_event


def test_phase3bd_r5_blocks_without_verified_consensus_source(tmp_path, monkeypatch) -> None:
    _clear_consensus_env(monkeypatch)
    session_factory = _session_factory(tmp_path)
    now = _dt("2026-07-01T12:00:00+00:00")
    with session_factory() as session:
        insert_economic_event(
            session,
            event_key="cpi",
            source="fixture",
            event_time=now + timedelta(minutes=30),
            category="cpi",
            title="CPI release",
            raw_json={"source_url": "https://example.test/calendar"},
        )
        payload = run_phase3bd_r5_consensus_feed_watch(session, now=now)

    assert payload["summary"]["status"] == "BLOCKED_BY_MISSING_CONSENSUS_SOURCE"
    assert payload["summary"]["source_configured"] is False
    assert payload["summary"]["in_release_window"] is True
    assert payload["summary"]["r4_ran"] is False
    assert payload["summary"]["live_demo_execution"] == "blocked"
    assert payload["summary"]["order_submission_cancel_replace"] == "blocked"


def test_phase3bd_r5_waits_outside_release_window_with_source(tmp_path, monkeypatch) -> None:
    _clear_consensus_env(monkeypatch)
    session_factory = _session_factory(tmp_path)
    now = _dt("2026-07-01T12:00:00+00:00")
    with session_factory() as session:
        insert_economic_event(
            session,
            event_key="jobs",
            source="fixture",
            event_time=now + timedelta(days=4),
            category="jobs",
            title="Jobs release",
            raw_json={"source_url": "https://example.test/calendar"},
        )
        payload = run_phase3bd_r5_consensus_feed_watch(
            session,
            input_file=tmp_path / "verified.csv",
            now=now,
            r4_runner=_raising_r4_runner,
        )

    assert payload["summary"]["status"] == "WAITING_FOR_RELEASE_WINDOW"
    assert payload["summary"]["source_mode"] == "VERIFIED_INPUT_FILE"
    assert payload["summary"]["r4_ran"] is False
    assert payload["summary"]["minutes_until_next_release"] == 5760


def test_phase3bd_r5_runs_r4_inside_release_window(tmp_path, monkeypatch) -> None:
    _clear_consensus_env(monkeypatch)
    session_factory = _session_factory(tmp_path)
    now = _dt("2026-07-01T12:00:00+00:00")
    with session_factory() as session:
        insert_economic_event(
            session,
            event_key="fed",
            source="fixture",
            event_time=now - timedelta(minutes=15),
            category="fed",
            title="Fed rate decision",
            raw_json={"source_url": "https://example.test/fed"},
        )
        payload = run_phase3bd_r5_consensus_feed_watch(
            session,
            input_file=tmp_path / "verified.csv",
            now=now,
            r4_runner=_fake_r4_runner,
        )

    assert payload["summary"]["status"] == "R4_ACTIVE_WITH_VERIFIED_CONSENSUS"
    assert payload["summary"]["in_release_window"] is True
    assert payload["summary"]["r4_ran"] is True
    assert payload["summary"]["consensus_value_observations"] == 3
    assert payload["summary"]["actual_and_consensus_observations"] == 2
    assert payload["summary"]["rankings_inserted"] == 1


def test_phase3bd_r5_force_refresh_runs_outside_release_window(tmp_path, monkeypatch) -> None:
    _clear_consensus_env(monkeypatch)
    session_factory = _session_factory(tmp_path)
    now = _dt("2026-07-01T12:00:00+00:00")
    with session_factory() as session:
        insert_economic_event(
            session,
            event_key="gdp",
            source="fixture",
            event_time=now + timedelta(days=10),
            category="gdp",
            title="GDP release",
            raw_json={"source_url": "https://example.test/gdp"},
        )
        artifacts = write_phase3bd_r5_consensus_feed_watch_report(
            session=session,
            output_dir=tmp_path / "reports",
            input_file=tmp_path / "verified.json",
            now=now,
            force_refresh=True,
            r4_runner=_fake_r4_runner,
        )

    assert artifacts.payload["summary"]["status"] == "R4_ACTIVE_WITH_VERIFIED_CONSENSUS"
    assert artifacts.payload["summary"]["force_refresh"] is True
    assert artifacts.payload["summary"]["r4_ran"] is True
    assert artifacts.json_path.exists()
    assert artifacts.markdown_path.exists()
    history = artifacts.history_path.read_text(encoding="utf-8")
    assert "R4_ACTIVE_WITH_VERIFIED_CONSENSUS" in history


def test_release_window_state_reports_next_and_previous_events(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = _dt("2026-07-01T12:00:00+00:00")
    with session_factory() as session:
        insert_economic_event(
            session,
            event_key="cpi",
            source="fixture",
            event_time=now - timedelta(minutes=10),
            category="cpi",
            title="CPI release",
            raw_json={"source_url": "https://example.test/cpi"},
        )
        insert_economic_event(
            session,
            event_key="jobs",
            source="fixture",
            event_time=now + timedelta(minutes=20),
            category="jobs",
            title="Jobs release",
            raw_json={"source_url": "https://example.test/jobs"},
        )
        state = release_window_state(session, now=now)

    assert state["in_release_window"] is True
    assert state["next_release_event_key"] == "jobs"
    assert state["minutes_until_next_release"] == 20
    assert state["last_release_event_key"] == "cpi"
    assert state["minutes_since_last_release"] == 10
    assert len(state["release_window_events"]) == 2


def _fake_r4_runner(*args, **kwargs):
    del args, kwargs
    return {
        "phase": "3BD-R4",
        "generated_at": "2026-07-01T12:00:00+00:00",
        "summary": {
            "status": "ACTIVE_WITH_VERIFIED_CONSENSUS",
            "sources_attempted": 1,
            "sources_succeeded": 1,
            "consensus_value_observations": 3,
            "actual_and_consensus_observations": 2,
            "features_inserted": 2,
            "forecasts_inserted": 1,
            "rankings_inserted": 1,
            "opportunities_detected": 1,
        },
        "sources": [],
        "opportunity_report": "reports/opportunities_economic_v1.md",
        "recommended_next_action": "fixture",
    }


def _raising_r4_runner(*args, **kwargs):
    del args, kwargs
    raise AssertionError("R4 should not run outside the release window")


def _session_factory(tmp_path: Path):
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bd_r5.db'}")
    return get_session_factory(engine)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(ZoneInfo("UTC"))


def _clear_consensus_env(monkeypatch) -> None:
    for name in TRADING_ECONOMICS_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
