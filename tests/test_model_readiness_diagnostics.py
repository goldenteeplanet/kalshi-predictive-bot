from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.crypto.assets import DEFAULT_CRYPTO_SYMBOLS
from kalshi_predictor.crypto.repository import insert_crypto_features, insert_crypto_market_link
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot
from kalshi_predictor.economic.repository import (
    insert_economic_feature,
    insert_economic_market_link,
)
from kalshi_predictor.forecasting.status import (
    EXPECTED_MODEL_NAMES,
    STATUS_NEEDS_DATA,
    STATUS_READY_NO_FORECASTS,
    STATUS_READY_NO_MATCHING_MARKETS,
    generate_model_readiness_report,
    model_status_rows,
)
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.utils.time import utc_now


def test_expected_model_readiness_rows_are_present(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        rows = model_status_rows(session)

    names = {row["model_name"] for row in rows}
    assert set(EXPECTED_MODEL_NAMES).issubset(names)
    assert _row(rows, "crypto_v2")["status"] == STATUS_NEEDS_DATA
    assert _row(rows, "weather_v2")["status"] == STATUS_NEEDS_DATA
    assert _row(rows, "economic_v1")["status"] == STATUS_NEEDS_DATA


def test_crypto_readiness_identifies_missing_inputs(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        crypto = _row(model_status_rows(session), "crypto_v2")

    assert "crypto market link" in crypto["missing_data"]
    assert "crypto features" in crypto["missing_data"]
    assert "market snapshot" in crypto["missing_data"]
    command = f"kalshi-bot ingest-crypto --symbols {DEFAULT_CRYPTO_SYMBOLS} --source coinbase"
    assert command in crypto["next_commands"]


def test_crypto_readiness_distinguishes_no_matching_markets(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        _seed_snapshot(session, ticker="UNRELATED-READY", title="Will an unrelated market resolve?")
        insert_crypto_features(
            session,
            symbol="BTC",
            source="test",
            generated_at=utc_now(),
            window_minutes=1440,
            features={
                "price": "100000",
                "momentum_score": "0.25",
                "trend_direction": "UP",
            },
        )

        crypto = _row(model_status_rows(session), "crypto_v2")

    assert crypto["status"] == STATUS_READY_NO_MATCHING_MARKETS
    assert crypto["status_label"] == "Ready, no matching markets"
    assert crypto["missing_data"] == ["crypto market link"]


def test_crypto_ready_no_forecasts_when_inputs_exist(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="BTC-READY", title="Will Bitcoin rise today?")
        insert_crypto_market_link(
            session,
            ticker=snapshot.ticker,
            symbol="BTC",
            confidence=Decimal("1.0"),
            reason="test link",
        )
        insert_crypto_features(
            session,
            symbol="BTC",
            source="test",
            generated_at=utc_now(),
            window_minutes=1440,
            features={
                "price": "100000",
                "momentum_score": "0.25",
                "trend_direction": "UP",
            },
        )

        crypto = _row(model_status_rows(session), "crypto_v2")

    assert crypto["status"] == STATUS_READY_NO_FORECASTS
    assert crypto["missing_data_label"] == "none"
    assert "kalshi-bot forecast --model crypto_v2" in crypto["next_commands"]


def test_economic_sample_path_and_commands_are_visible(tmp_path) -> None:
    assert Path("examples/economic_sample.json").exists()
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="CPI-READY", title="Will CPI exceed 3 percent?")
        insert_economic_market_link(
            session,
            ticker=snapshot.ticker,
            event_key="cpi",
            category="inflation",
            confidence=Decimal("0.9"),
            reason="test link",
        )
        insert_economic_feature(
            session,
            event_key="cpi",
            generated_at=utc_now(),
            category="inflation",
            surprise_score=Decimal("0.1"),
            direction="UP",
            confidence_score=Decimal("0.8"),
        )

        economic = _row(model_status_rows(session), "economic_v1")

    assert economic["status"] == STATUS_READY_NO_FORECASTS
    assert (
        "kalshi-bot ingest-economic --input-file examples/economic_sample.json"
        in economic["next_commands"]
    )


def test_model_readiness_cli_and_report_smoke(tmp_path) -> None:
    get_settings.cache_clear()
    runner = CliRunner()
    db_url = f"sqlite:///{Path(tmp_path) / 'readiness_cli.db'}"

    status_result = runner.invoke(app, ["model-readiness"], env={"KALSHI_DB_URL": db_url})
    output_path = Path(tmp_path) / "model_readiness.md"
    report_result = runner.invoke(
        app,
        ["model-readiness-report", "--output", str(output_path)],
        env={"KALSHI_DB_URL": db_url},
    )

    get_settings.cache_clear()
    assert status_result.exit_code == 0
    assert "model=crypto_v2" in status_result.output
    assert "status=NEEDS_DATA" in status_result.output
    assert (
        f"kalshi-bot ingest-crypto --symbols {DEFAULT_CRYPTO_SYMBOLS} --source coinbase"
        in status_result.output
    )
    assert report_result.exit_code == 0
    assert output_path.exists()
    assert "# Model Readiness Report" in output_path.read_text(encoding="utf-8")


def test_generate_model_readiness_report_renders(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output_path = Path(tmp_path) / "report.md"

    with session_factory() as session:
        generated = generate_model_readiness_report(session, output_path=output_path)

    text = generated.read_text(encoding="utf-8")
    assert "## Model Readiness" in text
    assert "crypto_v2" in text
    assert f"kalshi-bot ingest-crypto --symbols {DEFAULT_CRYPTO_SYMBOLS} --source coinbase" in text


def test_ui_model_readiness_page_and_dashboard_card_render(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    client = TestClient(create_app(session_factory=session_factory))

    readiness = client.get("/models/readiness")
    dashboard = client.get("/dashboard")

    assert readiness.status_code == 200
    assert "Model Readiness" in readiness.text
    assert "crypto_v2" in readiness.text
    assert (
        f"kalshi-bot ingest-crypto --symbols {DEFAULT_CRYPTO_SYMBOLS} --source coinbase"
        in readiness.text
    )
    assert dashboard.status_code == 200
    assert "Some models are inactive." in dashboard.text
    assert "Latest Forecast" in dashboard.text
    assert (
        f"kalshi-bot ingest-crypto --symbols {DEFAULT_CRYPTO_SYMBOLS} --source coinbase"
        in dashboard.text
    )


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'model_readiness.db'}")
    return get_session_factory(engine)


def _row(rows: list[dict], model_name: str) -> dict:
    return next(row for row in rows if row["model_name"] == model_name)


def _seed_snapshot(session, *, ticker: str, title: str):
    captured_at = utc_now()
    snapshot = insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "title": title,
            "yes_bid_dollars": "0.45",
            "yes_ask_dollars": "0.55",
            "last_price_dollars": "0.50",
            "close_time": (captured_at + timedelta(hours=4)).isoformat(),
        },
        {"orderbook": {"yes": [[45, 10]], "no": [[45, 10]]}},
        captured_at,
    )
    session.flush()
    return snapshot
