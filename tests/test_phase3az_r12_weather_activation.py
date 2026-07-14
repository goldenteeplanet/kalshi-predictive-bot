from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.schema import (
    Forecast,
    Market,
    MarketLeg,
    MarketSnapshot,
    WeatherMarketLink,
)
from kalshi_predictor.phase3az_weather import (
    write_phase3az_r12_weather_activation_preview_report,
    write_phase3az_r12_weather_missing_link_apply_report,
    write_phase3az_r13_weather_handoff_status_report,
)
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.repository import (
    insert_weather_features,
    insert_weather_market_link,
)


def test_phase3az_r12_weather_preview_finds_safe_stale_relink(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    ticker = "KXTEMPNYCH-26JUL1609-T90"
    fresh_target = now + timedelta(hours=6)
    stale_target = now - timedelta(days=9)
    with session_factory() as session:
        _seed_weather_market(
            session,
            ticker=ticker,
            title="Will New York temperature be above 90 degrees?",
            target_time=fresh_target,
        )
        insert_weather_market_link(
            session,
            ticker=ticker,
            location_key="new_york",
            weather_metric="TEMPERATURE",
            target_operator="ABOVE",
            target_value="90",
            target_time=stale_target,
            confidence="1.0",
            reason="stale fixture",
            detected_at=stale_target,
        )
        insert_weather_features(
            session,
            location_key="new_york",
            source="test",
            generated_at=now - timedelta(hours=1),
            target_time=fresh_target,
            features={
                "temperature_f": "92",
                "precipitation_probability": "0",
                "expected_precipitation_inches": "0",
                "wind_speed_mph": "4",
                "wind_gust_mph": "6",
                "weather_confidence_score": "0.9",
                "forecast_age_hours": "1",
            },
        )

        artifacts = write_phase3az_r12_weather_activation_preview_report(
            session,
            output_dir=tmp_path / "reports",
            limit=100,
            fresh_window_hours=24,
            match_tolerance_hours=3,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["rows_safe_to_relink"] == 1
    assert payload["summary"]["stale_target_time_links"] == 1
    row = payload["candidate_rows"][0]
    assert row["ticker"] == ticker
    assert row["blocker"] == "SAFE_TO_RELINK"
    assert row["safe_to_relink"] is True
    assert ticker in artifacts.safe_to_relink_csv_path.read_text(encoding="utf-8")


def test_phase3az_r12_weather_preview_blocks_without_fresh_window(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    ticker = "KXTEMPNYCH-26JUL1609-T91"
    with session_factory() as session:
        _seed_weather_market(
            session,
            ticker=ticker,
            title="Will New York temperature be above 91 degrees?",
            target_time=now + timedelta(hours=6),
        )
        insert_weather_market_link(
            session,
            ticker=ticker,
            location_key="new_york",
            weather_metric="TEMPERATURE",
            target_operator="ABOVE",
            target_value="91",
            target_time=now - timedelta(days=9),
            confidence="1.0",
            reason="stale fixture",
            detected_at=now - timedelta(days=9),
        )

        artifacts = write_phase3az_r12_weather_activation_preview_report(
            session,
            output_dir=tmp_path / "reports",
            limit=100,
            fresh_window_hours=24,
            match_tolerance_hours=3,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["rows_safe_to_relink"] == 0
    assert payload["summary"]["stale_target_time_links"] == 1
    assert payload["candidate_rows"][0]["blocker"] == "NO_FRESH_FORECAST_WINDOW"
    assert artifacts.safe_to_relink_csv_path.read_text(encoding="utf-8").count("\n") == 1


def test_phase3az_r12_weather_preview_finds_safe_missing_link(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    ticker = "KXTEMPNYCH-26JUL1609-T92"
    fresh_target = now + timedelta(hours=6)
    with session_factory() as session:
        _seed_weather_market(
            session,
            ticker=ticker,
            title="Will New York temperature be above 92 degrees?",
            target_time=fresh_target,
        )
        insert_weather_features(
            session,
            location_key="new_york",
            source="test",
            generated_at=now - timedelta(hours=1),
            target_time=fresh_target,
            features={
                "temperature_f": "93",
                "precipitation_probability": "0",
                "expected_precipitation_inches": "0",
                "wind_speed_mph": "4",
                "wind_gust_mph": "6",
                "weather_confidence_score": "0.9",
                "forecast_age_hours": "1",
            },
        )

        artifacts = write_phase3az_r12_weather_activation_preview_report(
            session,
            output_dir=tmp_path / "reports",
            limit=100,
            fresh_window_hours=24,
            match_tolerance_hours=3,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["rows_safe_to_link"] == 1
    assert payload["summary"]["missing_weather_links_safe_to_link"] == 1
    row = payload["candidate_rows"][0]
    assert row["blocker"] == "SAFE_TO_LINK"
    assert row["safe_to_link"] is True
    assert ticker in artifacts.safe_to_link_csv_path.read_text(encoding="utf-8")


def test_phase3az_r12_weather_missing_link_apply_writes_only_safe_rows(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    ticker = "KXTEMPNYCH-26JUL1609-T93"
    fresh_target = now + timedelta(hours=6)
    with session_factory() as session:
        _seed_weather_market(
            session,
            ticker=ticker,
            title="Will New York temperature be above 93 degrees?",
            target_time=fresh_target,
        )
        insert_weather_features(
            session,
            location_key="new_york",
            source="test",
            generated_at=now - timedelta(hours=1),
            target_time=fresh_target,
            features={
                "temperature_f": "94",
                "precipitation_probability": "0",
                "expected_precipitation_inches": "0",
                "wind_speed_mph": "4",
                "wind_gust_mph": "6",
                "weather_confidence_score": "0.9",
                "forecast_age_hours": "1",
            },
        )
        dry_run_artifacts = write_phase3az_r12_weather_missing_link_apply_report(
            session,
            output_dir=tmp_path / "reports_dry",
            limit=100,
            fresh_window_hours=24,
            match_tolerance_hours=3,
        )
        dry_payload = json.loads(dry_run_artifacts.json_path.read_text(encoding="utf-8"))
        assert dry_payload["summary"]["would_write_link_rows"] == 1
        assert session.scalar(select(func.count()).select_from(WeatherMarketLink)) == 0

        apply_artifacts = write_phase3az_r12_weather_missing_link_apply_report(
            session,
            output_dir=tmp_path / "reports_apply",
            limit=100,
            fresh_window_hours=24,
            match_tolerance_hours=3,
            dry_run=False,
            apply=True,
            backup_first=True,
        )
        apply_payload = json.loads(apply_artifacts.json_path.read_text(encoding="utf-8"))

        assert apply_payload["status"] == "APPLIED"
        assert apply_payload["summary"]["link_rows_written"] == 1
        assert apply_payload["backup_path"]
        assert session.scalar(select(func.count()).select_from(WeatherMarketLink)) == 1
        link = session.scalar(select(WeatherMarketLink).where(WeatherMarketLink.ticker == ticker))
        assert link is not None
        assert link.location_key == "new_york"
        assert link.weather_metric == "TEMPERATURE"


def test_phase3az_r12_weather_preview_cli_help() -> None:
    result = CliRunner().invoke(app, ["phase3az-r12-weather-activation-preview", "--help"])

    assert result.exit_code == 0
    assert "phase3az-r12-weather-activation-preview" in result.output


def test_phase3az_r12_weather_missing_link_apply_cli_help() -> None:
    result = CliRunner().invoke(app, ["phase3az-r12-weather-missing-link-apply", "--help"])

    assert result.exit_code == 0
    assert "phase3az-r12-weather-missing-link-apply" in result.output


def test_phase3az_r13_weather_handoff_detects_ranking_gap(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "kalshi_predictor.phase3az_weather.db_writer_monitor",
        lambda **kwargs: {"safe_to_start_write": True, "current_writer": None},
    )
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    ticker = "KXTEMPNYCH-26JUL1609-T94"
    target_time = now + timedelta(hours=2)
    with session_factory() as session:
        _seed_weather_market(
            session,
            ticker=ticker,
            title="Will New York temperature be above 94 degrees?",
            target_time=target_time,
        )
        insert_weather_market_link(
            session,
            ticker=ticker,
            location_key="new_york",
            weather_metric="TEMPERATURE",
            target_operator="ABOVE",
            target_value="94",
            target_time=target_time,
            confidence="1.0",
            reason="current fixture",
            detected_at=now,
        )
        session.add(
            MarketSnapshot(
                ticker=ticker,
                captured_at=now,
                status="active",
                yes_bid_dollars="0.40",
                yes_ask_dollars="0.45",
                no_bid_dollars="0.55",
                no_ask_dollars="0.60",
                best_yes_bid="0.40",
                best_yes_ask="0.45",
                best_no_bid="0.55",
                best_no_ask="0.60",
                spread="0.05",
                last_price_dollars="0.42",
                volume_fp="10",
                volume_24h_fp="10",
                open_interest_fp="10",
                raw_market_json="{}",
                raw_orderbook_json="{}",
            )
        )
        session.add(
            Forecast(
                ticker=ticker,
                forecasted_at=now,
                model_name="weather_v2",
                yes_probability="0.46",
                market_mid_probability="0.425",
                best_yes_bid="0.40",
                best_yes_ask="0.45",
                feature_json="{}",
                notes="fixture",
            )
        )
        session.commit()

        artifacts = write_phase3az_r13_weather_handoff_status_report(
            session,
            output_dir=tmp_path / "reports_r13",
            reports_dir=tmp_path,
            current_window_lookback_hours=3,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["current_weather_links"] == 1
    assert payload["summary"]["links_with_current_weather_forecasts"] == 1
    assert payload["summary"]["ranking_gap_rows"] == 1
    assert payload["next_action"]["stage"] == "INSERT_WEATHER_OPPORTUNITY_RANKINGS"
    assert "find-opportunities --model-name weather_v2" in artifacts.next_actions_path.read_text(
        encoding="utf-8"
    )


def test_phase3az_r13_weather_handoff_cli_help() -> None:
    result = CliRunner().invoke(app, ["phase3az-r13-weather-handoff-status", "--help"])

    assert result.exit_code == 0
    assert "phase3az-r13-weather-handoff-status" in result.output


def _session_factory(tmp_path: Path):
    engine = init_db(f"sqlite:///{tmp_path / 'phase3az_r12_weather.db'}")
    return get_session_factory(engine)


def _seed_weather_market(
    session,
    *,
    ticker: str,
    title: str,
    target_time,
) -> None:
    now = utc_now()
    market = Market(
        ticker=ticker,
        event_ticker=ticker.rsplit("-", 1)[0],
        series_ticker="KXTEMPNYCH",
        title=title,
        subtitle="",
        market_type="binary",
        status="open",
        result=None,
        open_time=now - timedelta(hours=1),
        close_time=target_time,
        expected_expiration_time=target_time,
        expiration_time=target_time,
        settlement_ts=None,
        settlement_value_dollars=None,
        volume_fp=None,
        open_interest_fp=None,
        liquidity_dollars=None,
        rules_primary="",
        rules_secondary="",
        raw_json="{}",
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(market)
    session.add(
        MarketLeg(
            ticker=ticker,
            leg_index=0,
            parsed_at=now,
            side="YES",
            category="weather",
            market_type="binary",
            entity_name="New York",
            operator="ABOVE",
            threshold_value="90",
            unit="F",
            confidence="1.0",
            raw_text=title,
            reason="weather fixture",
            raw_json="{}",
        )
    )
    session.flush()
