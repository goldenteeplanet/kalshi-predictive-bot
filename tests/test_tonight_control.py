from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from kalshi_predictor.config import Settings
from kalshi_predictor.crypto.assets import DEFAULT_CRYPTO_SYMBOLS
from kalshi_predictor.crypto.repository import (
    insert_crypto_features,
    insert_crypto_market_link,
    insert_crypto_price,
)
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import PaperOrder
from kalshi_predictor.paper.models import ORDER_OPEN
from kalshi_predictor.tonight.control import (
    READY,
    WARNING,
    TonightJobs,
    build_tonight_check,
    generate_tonight_report,
    run_tonight,
)
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.repository import (
    insert_weather_features,
    insert_weather_forecast,
    insert_weather_market_link,
)


def test_tonight_check_returns_ready_when_required_systems_available(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ready_inputs(session)
        check = build_tonight_check(
            session,
            settings=Settings(),
            project_path=tmp_path,
            reports_dir=tmp_path / "reports",
            check_port=False,
        )

    assert check.status == READY
    assert check.summary["paper_trades_today"] >= 11
    assert check.summary["crypto"]["status"] == READY
    assert check.summary["weather"]["status"] == READY


def test_tonight_check_warns_on_onedrive_path(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ready_inputs(session)
        check = build_tonight_check(
            session,
            settings=Settings(),
            project_path=tmp_path / "OneDrive" / "repo",
            reports_dir=tmp_path / "reports",
            check_port=False,
        )

    assert check.status == WARNING
    assert any(item.name == "OneDrive path" and item.status == WARNING for item in check.items)


def test_tonight_run_does_not_call_demo_or_live_execution_by_default(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    seen_settings = []
    jobs = _fake_jobs(seen_settings=seen_settings)

    result = run_tonight(
        session_factory,
        settings=Settings(execution_enabled=True),
        jobs=jobs,
        max_cycles=1,
        interval_minutes=0,
        sleeper=lambda seconds: None,
    )

    assert result.cycles_completed == 1
    assert result.status == "COMPLETED"
    assert all(settings.execution_enabled is False for settings in seen_settings)
    assert all(settings.overnight_run_demo is False for settings in seen_settings)
    assert not any(
        "demo" in step.name.lower() or "live" in step.name.lower()
        for step in result.steps
    )


def test_tonight_run_continues_after_nonfatal_step_failure(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    jobs = _fake_jobs(fail_name="collect-once")

    result = run_tonight(
        session_factory,
        settings=Settings(),
        jobs=jobs,
        max_cycles=1,
        interval_minutes=0,
        sleeper=lambda seconds: None,
    )

    assert result.status == "COMPLETED_WITH_ERRORS"
    assert result.errors[0].name == "collect-once"
    assert any(step.name == "learning-once" and step.status == "OK" for step in result.steps)


def test_tonight_report_renders(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output = tmp_path / "tonight_report.md"
    with session_factory() as session:
        _seed_ready_inputs(session)
        path = generate_tonight_report(session, output_path=output, settings=Settings())

    text = path.read_text(encoding="utf-8")
    assert "Tonight Readiness Report" in text
    assert "Learning Progress" in text
    assert "Recommended Morning Action" in text


def test_ui_tonight_card_renders(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ready_inputs(session)
        session.commit()
    client = TestClient(create_app(session_factory=session_factory, settings=Settings()))

    dashboard = client.get("/dashboard")
    learning = client.get("/learning")

    assert dashboard.status_code == 200
    assert learning.status_code == 200
    assert "Tonight Mode" in dashboard.text
    assert "Tonight Mode" in learning.text
    assert "Paper-only confirmed" in dashboard.text


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'tonight.db'}")
    return get_session_factory(engine)


def _seed_ready_inputs(session) -> None:
    now = utc_now()
    for index in range(11):
        session.add(
            PaperOrder(
                ticker=f"TONIGHT-{index}",
                forecast_id=None,
                created_at=now,
                model_name="ensemble_v2",
                side="BUY_YES",
                probability="0.60",
                market_price="0.50",
                limit_price="0.50",
                edge="0.10",
                quantity=1,
                status=ORDER_OPEN,
                reason="tonight readiness seed",
                raw_decision_json=encode_json({}),
            )
        )
    insert_crypto_price(
        session,
        symbol="BTC",
        source="test",
        observed_at=now,
        price_usd="100",
    )
    insert_crypto_features(
        session,
        symbol="BTC",
        source="test",
        generated_at=now,
        window_minutes=1440,
        features={"price": "100", "momentum_score": "0.2", "trend_direction": "UP"},
    )
    insert_crypto_market_link(
        session,
        ticker="TONIGHT-BTC",
        symbol="BTC",
        confidence="1.0",
        reason="test",
    )
    insert_weather_forecast(
        session,
        location_key="kansas_city",
        source="test",
        forecast_generated_at=now,
        forecast_time=now + timedelta(hours=1),
        temperature_f="75",
        precipitation_probability="0.1",
    )
    insert_weather_features(
        session,
        location_key="kansas_city",
        source="test",
        generated_at=now,
        target_time=now + timedelta(hours=1),
        features={"temperature_f": "75", "weather_confidence_score": "80"},
    )
    insert_weather_market_link(
        session,
        ticker="TONIGHT-WX",
        location_key="kansas_city",
        weather_metric="temperature",
        target_operator="above",
        confidence="0.9",
        reason="test",
    )
    session.flush()


def _fake_jobs(*, fail_name: str | None = None, seen_settings: list[Settings] | None = None):
    def job_for(name: str):
        def job(session, settings):
            del session
            if seen_settings is not None:
                seen_settings.append(settings)
            if name == fail_name:
                raise RuntimeError("planned non-fatal failure")
            return {"step": name}

        return job

    return TonightJobs(
        collect_once=job_for("collect-once"),
        ingest_crypto=job_for(f"ingest-crypto {DEFAULT_CRYPTO_SYMBOLS}"),
        build_crypto_features=job_for("build-crypto-features"),
        link_crypto_markets=job_for("link-crypto-markets"),
        ingest_weather=job_for("ingest-weather kansas_city"),
        build_weather_features=job_for("build-weather-features"),
        link_weather_markets=job_for("link-weather-markets"),
        forecast_all=job_for("forecast --model all"),
        model_health=job_for("model-health"),
        signals_status=job_for("signals-status"),
        learning_once=job_for("learning-once"),
        paper_pnl=job_for("paper-pnl"),
        sync_settlements=job_for("sync-settlements"),
        model_confidence=job_for("model-confidence"),
        learning_report=job_for("learning-report"),
        signals_report=job_for("signals-report"),
        paper_summary=job_for("paper-summary"),
        leaderboard=job_for("leaderboard"),
        overnight_report=job_for("overnight-report"),
    )
