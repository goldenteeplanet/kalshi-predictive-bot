from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3bb_r12_cloud_bootstrap import (
    CloudBootstrapTarget,
    RemoteProbe,
    RemoteProbeResult,
)
from kalshi_predictor.phase3bb_r52_weather_ev_fair_value_diagnostic import (
    build_phase3bb_r52_weather_ev_fair_value_diagnostic,
    write_phase3bb_r52_weather_ev_fair_value_diagnostic_report,
)


def test_phase3bb_r52_explains_non_positive_weather_ev(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r52_weather_ev_fair_value_diagnostic_report(
            session,
            output_dir=reports_dir / "phase3bb_r52",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(_ev_state(rows=_negative_rows())),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["decision"]["status"] == "WEATHER_EV_NOT_POSITIVE_EXPLAINED"
    assert payload["summary"]["ranked_weather_rows"] == 10
    assert payload["summary"]["positive_ev_rows"] == 0
    assert payload["summary"]["non_positive_ev_rows"] == 10
    assert payload["summary"]["best_edge_to_positive"] == "0.0200"
    assert payload["safety_flags"]["ssh_write_capable_commands_executed"] == 0
    assert payload["safety_flags"]["creates_paper_trades"] is False
    assert artifacts.rows_csv_path.exists()
    assert "phase3bb-r40-cloud-scheduler-runtime-monitor" in artifacts.next_actions_path.read_text(
        encoding="utf-8"
    )


def test_phase3bb_r52_routes_positive_ev_to_paper_gate(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r52_weather_ev_fair_value_diagnostic(
            session,
            output_dir=reports_dir / "phase3bb_r52",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(_ev_state(rows=_positive_rows())),
        )

    assert payload["decision"]["status"] == "WEATHER_POSITIVE_EV_FOUND_REFRESH_PAPER_GATE"
    assert payload["summary"]["positive_ev_rows"] == 1
    assert "phase3bb-r8-unified-paper-gate" in payload["decision"]["operator_next_command"]
    assert payload["paper_trade_creation"] is False
    assert payload["order_submission_cancel_replace"] is False


def test_phase3bb_r52_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r52-weather-ev-fair-value-diagnostic", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r52-weather-ev-fair-value-diagnostic" in result.output
    assert "current-window" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r52.db'}")
    return get_session_factory(engine)


def _write_context(reports_dir: Path) -> None:
    r11_dir = reports_dir / "phase3bb_r11"
    r11_dir.mkdir(parents=True, exist_ok=True)
    (r11_dir / "codex_cloud_context.json").write_text(
        json.dumps(
            {
                "ssh_profile": {
                    "host": "203.0.113.10",
                    "user": "root",
                    "identity_file": "~/.ssh/id_ed25519_do",
                },
                "remote_paths": {
                    "app_path": "/opt/kalshi-predictive-bot",
                    "env_path": "/etc/kalshi-bot/kalshi-bot.env",
                    "db_path": "/var/lib/kalshi-bot/kalshi_phase1.db",
                    "reports_path": "/opt/kalshi-predictive-bot/reports",
                },
            }
        ),
        encoding="utf-8",
    )


def _fake_probe_runner(state: dict[str, object]):
    outputs = {
        "remote_time_utc": ("2026-07-13T23:00:00Z\n", True, 0, ""),
        "db_writer_monitor": (
            json.dumps({"status": "CLEAR", "safe_to_start_write": True, "current_writer_pid": None}),
            True,
            0,
            "",
        ),
        "remote_thresholds": (
            json.dumps(
                {
                    "opportunity_min_edge": "0.03",
                    "opportunity_min_score": "60",
                    "opportunity_max_spread": "0.10",
                    "opportunity_min_liquidity": "10",
                    "opportunity_min_time_to_close_minutes": "10",
                }
            ),
            True,
            0,
            "",
        ),
        "command_registry": ("COMMAND_REGISTRY_OK\n", True, 0, ""),
        "weather_ev_state": (json.dumps(state), True, 0, ""),
    }

    def _runner(probe: RemoteProbe, target: CloudBootstrapTarget) -> RemoteProbeResult:
        stdout, ok, exit_code, stderr = outputs.get(probe.name, ("", True, 0, ""))
        return RemoteProbeResult(
            name=probe.name,
            command=probe.command,
            ok=ok,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=0.01,
        )

    return _runner


def _ev_state(*, rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "ok": True,
        "rows": rows,
        "summary": {
            "linked_weather_rows": len(rows),
            "ranked_weather_rows": len(rows),
            "positive_ev_rows": sum(1 for row in rows if str(row["estimated_edge"]).startswith("0.")),
        },
    }


def _negative_rows() -> list[dict[str, object]]:
    return [
        {
            "ticker": f"KXTEMPNYCH-26JUL1320-T{73 + index}.99",
            "market_title": "Will the temp in New York City be above a threshold?",
            "target_time": "2026-07-14T00:00:00+00:00",
            "target_window_state": "LIVE_OR_FUTURE",
            "forecast_probability": "0.4000",
            "yes_fair_value": "0.4000",
            "no_fair_value": "0.6000",
            "market_mid_probability": "0.4500",
            "best_yes_ask": "0.4300",
            "best_no_ask": "0.6200",
            "yes_edge": "-0.0300",
            "no_edge": "-0.0200",
            "best_side": "BUY_NO",
            "best_price": "0.6200",
            "estimated_edge": "-0.0200",
            "edge_to_positive": "0.0200",
            "opportunity_score": "20",
            "spread": "0.0400",
            "liquidity": "100",
            "liquidity_score": "80",
            "spread_score": "60",
            "first_ev_blocker": "FAIR_VALUE_BELOW_EXECUTABLE_PRICE",
            "explanation": "The best executable ask is above model fair value.",
        }
        for index in range(10)
    ]


def _positive_rows() -> list[dict[str, object]]:
    row = _negative_rows()[0]
    row.update(
        {
            "ticker": "KXTEMPNYCH-26JUL1320-T75.99",
            "best_yes_ask": "0.4100",
            "yes_edge": "0.0400",
            "best_side": "BUY_YES",
            "best_price": "0.4100",
            "estimated_edge": "0.0400",
            "edge_to_positive": "0.0000",
            "first_ev_blocker": "EV_POSITIVE_OR_FILTERED_LATER",
        }
    )
    return [row]
