from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from kalshi_predictor.config import Settings
from kalshi_predictor.consensus.repository import insert_forum_consensus_signal
from kalshi_predictor.consensus.scoring import assess_forum_consensus
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.schema import ModelIterationMetric, OvernightCycle, OvernightRun
from kalshi_predictor.overnight.cycle import OvernightJobs
from kalshi_predictor.overnight.health import run_health_checks
from kalshi_predictor.overnight.runner import run_overnight_once, run_overnight_scheduler
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.utils.time import utc_now


def test_overnight_once_creates_run_cycle_rows(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        result = run_overnight_once(session, settings=_settings(), jobs=_jobs())
        session.commit()

        run = session.scalar(select(OvernightRun))
        cycle = session.scalar(select(OvernightCycle))

    assert result.status == "COMPLETED"
    assert run is not None
    assert cycle is not None
    assert cycle.markets_collected == 4
    assert cycle.paper_orders_created == 2


def test_overnight_run_stops_at_max_cycles(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    result = run_overnight_scheduler(
        session_factory,
        settings=_settings(overnight_enabled=True, overnight_max_cycles=2),
        jobs=_jobs(),
        sleeper=lambda _seconds: None,
    )

    assert result.status == "COMPLETED"
    assert len(result.cycles) == 2
    assert result.stop_reason == "Reached OVERNIGHT_MAX_CYCLES=2."


def test_health_check_fails_gracefully_when_reports_dir_unavailable(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    blocked_path = tmp_path / "reports-file"
    blocked_path.write_text("not a directory", encoding="utf-8")
    with session_factory() as session:
        result = run_health_checks(
            session,
            settings=_settings(overnight_require_market_data=False),
            reports_dir=blocked_path,
        )

    assert result["ok"] is False
    assert any(check["name"] == "Reports directory writable" for check in result["errors"])


def test_errors_stored_and_loop_continues_when_stop_on_error_false(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)

    def failing_crypto(session, settings):
        del session, settings
        raise RuntimeError("crypto source down")

    with session_factory() as session:
        result = run_overnight_once(
            session,
            settings=_settings(overnight_stop_on_error=False),
            jobs=_jobs(ingest_crypto=failing_crypto),
        )
        session.commit()

        cycle = session.scalar(select(OvernightCycle))

    assert result.status == "COMPLETED_WITH_ERRORS"
    assert result.paper_orders_created == 2
    assert cycle is not None
    assert "crypto source down" in (cycle.errors_json or "")


def test_no_execution_occurs_by_default(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    settings = _settings()
    with session_factory() as session:
        result = run_overnight_once(session, settings=settings, jobs=_jobs())

    assert settings.overnight_run_demo is False
    assert (
        result.summary["demo_execution"]
        == "OVERNIGHT_RUN_DEMO=false; no demo orders are submitted."
    )


def test_model_iteration_metrics_inserted(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        run_overnight_once(session, settings=_settings(), jobs=_jobs())
        session.commit()

        metric = session.scalar(select(ModelIterationMetric))

    assert metric is not None
    assert metric.model_name == "ensemble_v2"
    assert "Cycle completed" in metric.notes


def test_forum_consensus_flags_longshot_winner_contingent() -> None:
    signal = SimpleNamespace(
        observed_at=utc_now(),
        participant_count=28,
        winner_count=9,
        average_win_rate="0.64",
        longshot_price="0.18",
        consensus_score=None,
    )

    assessment = assess_forum_consensus(signal, settings=_settings())

    assert assessment.qualifies is True
    assert assessment.label == "Longshot Watch"
    assert "historically winning participants" in assessment.summary


def test_ingested_forum_consensus_persists(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        signal = insert_forum_consensus_signal(
            session,
            {
                "ticker": "LONGSHOT-TEST",
                "observed_at": utc_now().isoformat(),
                "source": "manual_forum_note",
                "side": "YES",
                "participant_count": 20,
                "winner_count": 6,
                "average_win_rate": "0.61",
                "longshot_price": "0.19",
            },
            settings=_settings(),
        )
        session.commit()

    assert signal.id is not None
    assert signal.side == "BUY_YES"
    assert signal.consensus_score is not None


def test_ui_overnight_page_smoke(tmp_path) -> None:
    client = TestClient(
        create_app(
            session_factory=_session_factory(tmp_path),
            settings=_settings(),
        )
    )

    response = client.get("/overnight")

    assert response.status_code == 200
    assert "Overnight" in response.text
    assert "PAPER / DEMO ONLY" in response.text
    assert "Run one paper cycle" in response.text


def test_dashboard_still_renders_with_overnight_card(tmp_path) -> None:
    client = TestClient(
        create_app(
            session_factory=_session_factory(tmp_path),
            settings=_settings(),
        )
    )

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "Decision Cockpit" in response.text
    assert "Overnight Paper Learning" in response.text


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'overnight.db'}")
    return get_session_factory(engine)


def _settings(**overrides) -> Settings:
    values = {
        "overnight_model": "ensemble_v2",
        "overnight_run_paper": True,
        "overnight_run_demo": False,
        "overnight_require_market_data": False,
        "overnight_interval_minutes": 0,
        "overnight_max_cycles": 2,
        "forum_consensus_min_winners": 5,
        "forum_consensus_min_win_rate": Decimal("0.55"),
        "forum_consensus_longshot_max_price": Decimal("0.25"),
        "forum_consensus_max_age_hours": 24,
    }
    values.update(overrides)
    return Settings(**values)


def _jobs(**overrides) -> OvernightJobs:
    jobs = OvernightJobs(
        health=lambda session, settings: {
            "ok": True,
            "checks": [],
            "errors": [],
            "warnings": [],
        },
        collect_markets=lambda session, settings: {
            "markets_seen": 4,
            "snapshots_inserted": 4,
            "forecasts_inserted": 1,
        },
        ingest_crypto=_ok,
        build_crypto_features=_ok,
        link_crypto_markets=_ok,
        ingest_weather=_ok,
        build_weather_features=_ok,
        link_weather_markets=_ok,
        forecast_all=lambda session, settings: {"forecasts_inserted": 3},
        update_model_weights=_ok,
        forecast_target_model=lambda session, settings: {"forecasts_inserted": 2},
        find_opportunities=lambda session, settings: {"opportunities_detected": 3},
        paper_run=lambda session, settings: {"orders_created": 2, "fills_created": 2},
        paper_pnl=lambda session, settings: {"pnl_rows_inserted": 0, "total_pnl": "0"},
        sync_settlements=lambda session, settings: {"settlements_synced": 1},
        backtest=_ok,
        reports=lambda session, settings: {"reports_generated": 4},
    )
    for key, value in overrides.items():
        setattr(jobs, key, value)
    return jobs


def _ok(session, settings) -> dict:
    del session, settings
    return {"status": "ok"}
