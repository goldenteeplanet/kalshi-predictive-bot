from types import SimpleNamespace

from typer.testing import CliRunner

import kalshi_predictor.cli as cli_module
from kalshi_predictor.cli import app


def test_cli_help() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "collect-once" in result.output
    assert "report-calibration" in result.output
    assert "paper-run" in result.output
    assert "paper-summary" in result.output
    assert "paper-pnl" in result.output
    assert "paper-reset" in result.output
    assert "ingest-external" in result.output
    assert "backtest" in result.output
    assert "compare-strategies" in result.output
    assert "find-opportunities" in result.output
    assert "leaderboard" in result.output
    assert "market-rankings" in result.output
    assert "ingest-crypto" in result.output
    assert "build-crypto-features" in result.output
    assert "link-crypto-markets" in result.output
    assert "crypto-report" in result.output
    assert "crypto-backtest" in result.output
    assert "ingest-weather" in result.output
    assert "build-weather-features" in result.output
    assert "link-weather-markets" in result.output
    assert "weather-report" in result.output
    assert "weather-backtest" in result.output
    assert "tournament" in result.output
    assert "model-diagnostics" in result.output
    assert "model-weights" in result.output
    assert "autopilot-status" in result.output
    assert "autopilot-once" in result.output
    assert "autopilot-run" in result.output
    assert "autopilot-report" in result.output
    assert "overnight-status" in result.output
    assert "overnight-once" in result.output
    assert "overnight-run" in result.output
    assert "overnight-report" in result.output
    assert "tonight-check" in result.output
    assert "tonight-run" in result.output
    assert "tonight-report" in result.output
    assert "link-remediate" in result.output
    assert "settlement-watch" in result.output
    assert "phase3y-report" in result.output
    assert "ingest-forum-consensus" in result.output
    assert "portfolio-summary" in result.output
    assert "daily-briefing" in result.output
    assert "analytics-report" in result.output
    assert "best-payouts" in result.output
    assert "research-opportunity" in result.output
    assert "ask-research" in result.output
    assert "research-report" in result.output
    assert "signal-report" in result.output
    assert "signals-report" in result.output
    assert "signals-status" in result.output
    assert "forecast-signals" in result.output
    assert "signal-leaderboard" in result.output
    assert "signal-explorer" in result.output
    assert "signal-performance" in result.output
    assert "ingest-news" in result.output
    assert "link-news-markets" in result.output
    assert "build-news-features" in result.output
    assert "news-report" in result.output
    assert "news-opportunities" in result.output
    assert "news-backtest" in result.output
    assert "ingest-sports" in result.output
    assert "link-sports-markets" in result.output
    assert "build-sports-features" in result.output
    assert "sports-report" in result.output
    assert "sports-opportunities" in result.output
    assert "sports-backtest" in result.output
    assert "build-microstructure-features" in result.output
    assert "microstructure-report" in result.output
    assert "microstructure-opportunities" in result.output
    assert "microstructure-backtest" in result.output
    assert "build-meta-features" in result.output
    assert "build-meta-training" in result.output
    assert "meta-evaluate" in result.output
    assert "meta-report" in result.output
    assert "meta-opportunities" in result.output
    assert "scheduler-plan" in result.output
    assert "ui-summary" in result.output
    assert "explain-opportunity" in result.output
    assert "phase-status" in result.output
    assert "command-audit" in result.output
    assert "feature-discovery-status" in result.output
    assert "feature-discovery-run" in result.output
    assert "feature-discovery-report" in result.output
    assert "feature-experiment-export" in result.output
    assert "synthetic-markets-status" in result.output
    assert "synthetic-markets-run" in result.output
    assert "synthetic-markets-report" in result.output
    assert "rl-status" in result.output
    assert "rl-dataset" in result.output
    assert "rl-train" in result.output
    assert "rl-evaluate" in result.output
    assert "rl-shadow-report" in result.output
    assert "rl-drift-report" in result.output
    assert "institutional-dashboard-status" in result.output
    assert "institutional-dashboard-report" in result.output
    assert "institutional-dashboard-export" in result.output
    assert "personal-trader-status" in result.output
    assert "personal-trader-brief" in result.output
    assert "personal-trader-audit" in result.output


def test_phase_2_6_cli_command_help() -> None:
    runner = CliRunner()
    for command in ("find-opportunities", "leaderboard", "market-rankings"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_phase_2_7_crypto_cli_command_help() -> None:
    runner = CliRunner()
    for command in (
        "ingest-crypto",
        "build-crypto-features",
        "link-crypto-markets",
        "crypto-report",
        "crypto-backtest",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_phase_2_8_weather_cli_command_help() -> None:
    runner = CliRunner()
    for command in (
        "ingest-weather",
        "build-weather-features",
        "link-weather-markets",
        "weather-report",
        "weather-backtest",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_ingest_weather_location_key_resolves_known_coordinates(monkeypatch) -> None:
    runner = CliRunner()
    captured = {}

    class FakeSession:
        def commit(self) -> None:
            captured["committed"] = True

    class FakeSessionContext:
        def __enter__(self) -> FakeSession:
            return FakeSession()

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    def fake_ingest_weather_location(
        session,
        *,
        location_key: str,
        latitude: float,
        longitude: float,
    ):
        captured.update(
            {
                "session": session,
                "location_key": location_key,
                "latitude": latitude,
                "longitude": longitude,
            }
        )
        return SimpleNamespace(
            forecasts_inserted=1,
            observations_inserted=0,
            source="test",
            errors=[],
        )

    monkeypatch.setattr(cli_module, "init_db", lambda: object())
    monkeypatch.setattr(cli_module, "get_session_factory", lambda _engine: FakeSessionContext)
    monkeypatch.setattr(
        cli_module,
        "ingest_weather_location",
        fake_ingest_weather_location,
    )

    result = runner.invoke(app, ["ingest-weather", "--location-key", "kansas_city"])

    assert result.exit_code == 0
    assert captured["location_key"] == "kansas_city"
    assert captured["latitude"] == 39.0997
    assert captured["longitude"] == -94.5786
    assert captured["committed"] is True


def test_phase_2_9_tournament_cli_command_help() -> None:
    runner = CliRunner()
    for command in ("tournament", "model-diagnostics", "model-weights"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_phase_2_9_forecast_help_mentions_ensemble_v2() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["forecast", "--help"])

    assert result.exit_code == 0
    assert "ensemble_v2" in result.output
    assert "meta_model_v1" in result.output
    assert "meta_ensemble_v1" in result.output
    assert "sports_v1" in result.output


def test_phase_3b_autopilot_cli_command_help() -> None:
    runner = CliRunner()
    for command in (
        "autopilot-status",
        "autopilot-once",
        "autopilot-run",
        "autopilot-report",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_phase_3c_explain_opportunity_cli_command_help() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["explain-opportunity", "--help"])

    assert result.exit_code == 0
    assert "--ticker" in result.output


def test_phase_3c_5_overnight_cli_command_help() -> None:
    runner = CliRunner()
    for command in (
        "overnight-status",
        "overnight-once",
        "overnight-run",
        "overnight-report",
        "tonight-check",
        "tonight-run",
        "tonight-report",
        "ingest-forum-consensus",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_phase_3d_workstation_cli_command_help() -> None:
    runner = CliRunner()
    for command in ("portfolio-summary", "daily-briefing", "analytics-report"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_phase_3e_intelligence_cli_command_help() -> None:
    runner = CliRunner()
    for command in ("best-payouts", "ui-summary"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_phase_3f_research_cli_command_help() -> None:
    runner = CliRunner()
    for command in ("research-opportunity", "ask-research", "research-report"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_phase_3g_signal_cli_command_help() -> None:
    runner = CliRunner()
    for command in (
        "signal-report",
        "signals-report",
        "signals-status",
        "forecast-signals",
        "signal-leaderboard",
        "signal-explorer",
        "signal-performance",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_phase_3h_news_cli_command_help() -> None:
    runner = CliRunner()
    for command in (
        "ingest-news",
        "link-news-markets",
        "build-news-features",
        "news-report",
        "news-opportunities",
        "news-backtest",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_phase_3j_sports_cli_command_help() -> None:
    runner = CliRunner()
    for command in (
        "ingest-sports",
        "link-sports-markets",
        "build-sports-features",
        "sports-report",
        "sports-opportunities",
        "sports-backtest",
        "scheduler-plan",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_phase_3k_microstructure_cli_command_help() -> None:
    runner = CliRunner()
    for command in (
        "build-microstructure-features",
        "microstructure-report",
        "microstructure-opportunities",
        "microstructure-backtest",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_phase_3l_meta_cli_command_help() -> None:
    runner = CliRunner()
    for command in (
        "build-meta-features",
        "build-meta-training",
        "meta-evaluate",
        "meta-report",
        "meta-opportunities",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_phase_3r_synthetic_markets_cli_command_help() -> None:
    runner = CliRunner()
    for command in (
        "synthetic-markets-status",
        "synthetic-markets-run",
        "synthetic-markets-report",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_phase_3s_reinforcement_learning_cli_command_help() -> None:
    runner = CliRunner()
    for command in (
        "rl-status",
        "rl-dataset",
        "rl-train",
        "rl-evaluate",
        "rl-shadow-report",
        "rl-drift-report",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_phase_3t_institutional_dashboard_cli_command_help() -> None:
    runner = CliRunner()
    for command in (
        "institutional-dashboard-status",
        "institutional-dashboard-report",
        "institutional-dashboard-export",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_phase_3u_personal_trader_cli_command_help() -> None:
    runner = CliRunner()
    for command in (
        "personal-trader-status",
        "personal-trader-brief",
        "personal-trader-audit",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output
