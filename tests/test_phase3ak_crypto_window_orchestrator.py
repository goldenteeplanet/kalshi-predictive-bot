from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.crypto.repository import insert_crypto_features, insert_crypto_market_link
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import encode_json, insert_forecast, insert_market_snapshot
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    MarketLeg,
    MarketRanking,
    PositionSizingDecisionLog,
)
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.paper.models import BUY_YES
from kalshi_predictor.phase3ak import (
    Phase3AKArtifactSet,
    build_crypto_watch_status,
    build_crypto_window_sync,
    build_market_data_refresh_status,
    write_phase_3ak_report,
)
from kalshi_predictor.professional_ux.service import (
    _apply_phase3ak_top_strip_context,
    build_default_shell_context,
    load_shell_status_context,
)
from kalshi_predictor.utils.time import utc_now


def test_phase3ak_active_crypto_market_creates_active_window(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_window(session, ticker="KXBTC-ACTIVE")

        payload = build_crypto_window_sync(session, settings=_settings(tmp_path))

    assert payload["summary"]["active_crypto_markets"] == 1
    assert payload["summary"]["active_windows"] == 1
    assert payload["rows"][0]["window_state"] == "ACTIVE"


def test_phase3ak_expired_crypto_market_is_excluded_from_active_windows(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_window(
            session,
            ticker="KXBTC-EXPIRED",
            close_delta=timedelta(minutes=-5),
        )

        payload = build_crypto_window_sync(session, settings=_settings(tmp_path))

    assert payload["summary"]["expired_windows"] == 1
    assert payload["summary"]["active_windows"] == 0
    assert payload["primary_blocker"] == "EXPIRED_WINDOWS_ONLY"


def test_phase3ak_stale_quotes_block_as_quote_stale(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_window(
            session,
            ticker="KXBTC-STALE",
            snapshot_delta=timedelta(minutes=-90),
        )

        payload = build_crypto_window_sync(session, settings=_settings(tmp_path))

    assert payload["summary"]["stale_windows"] == 1
    assert payload["summary"]["active_windows"] == 1
    assert payload["summary"]["fresh_quote_count"] == 0
    assert payload["summary"]["stale_quote_count"] == 1
    assert payload["primary_blocker"] == "QUOTE_STALE"
    assert payload["rows"][0]["readiness_reason"] == "QUOTE_STALE"


def test_phase3ak_stale_current_windows_outrank_expired_history(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_window(
            session,
            ticker="KXBTC-OLD",
            close_delta=timedelta(minutes=-5),
        )
        _seed_crypto_window(
            session,
            ticker="KXBTC-CURRENT-STALE",
            snapshot_delta=timedelta(minutes=-90),
        )

        payload = build_crypto_window_sync(session, settings=_settings(tmp_path))

    assert payload["summary"]["expired_windows"] == 1
    assert payload["summary"]["stale_windows"] == 1
    assert payload["summary"]["active_windows"] == 1
    assert payload["primary_blocker"] == "QUOTE_STALE"
    assert payload["diagnosis"]["active_crypto_markets_exist_but_only_expired_windows_attached"] is False


def test_phase3ak_no_active_markets_reports_precise_blocker(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        payload = build_crypto_window_sync(session, settings=_settings(tmp_path))

    assert payload["summary"]["active_crypto_markets"] == 0
    assert payload["primary_blocker"] == "NO_ACTIVE_CRYPTO_MARKETS"


def test_phase3ak_positive_raw_ev_wide_spread_is_precise(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_window(
            session,
            ticker="KXBTC-WIDE",
            probability="0.70",
            best_price="0.50",
            spread="0.25",
        )

        payload = build_crypto_window_sync(session, settings=_settings(tmp_path))

    assert payload["rows"][0]["raw_expected_value"] == "0.20"
    assert payload["rows"][0]["readiness_reason"] == "SPREAD_TOO_WIDE"


def test_phase3ak_summary_uses_positive_ev_blocker_not_default_risk(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_window(
            session,
            ticker="KXBTC-WIDE",
            probability="0.90",
            best_price="0.50",
            spread="0.25",
        )
        _seed_crypto_window(
            session,
            ticker="KXBTC-NO-EV",
            probability="0.40",
            best_price="0.50",
            spread="0.02",
        )

        payload = build_crypto_window_sync(session, settings=_settings(tmp_path))

    assert payload["summary"]["liquidity_pass"] > 0
    assert payload["summary"]["spread_pass"] > 0
    assert payload["summary"]["positive_executable_ev"] > 0
    assert payload["summary"]["paper_ready_opportunities"] == 0
    assert payload["summary"]["positive_ev_blocker_counts"]["SPREAD_TOO_WIDE"] == 1
    assert payload["primary_blocker"] == "SPREAD_TOO_WIDE"


def test_phase3ak_zero_liquidity_reports_liquidity_too_low(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_window(
            session,
            ticker="KXBTC-ILLIQUID",
            probability="0.90",
            best_price="0.10",
            spread="0.02",
            liquidity_score="0",
        )

        payload = build_crypto_window_sync(session, settings=_settings(tmp_path))

    assert payload["rows"][0]["readiness_reason"] == "LIQUIDITY_TOO_LOW"
    assert payload["primary_blocker"] == "LIQUIDITY_TOO_LOW"


def test_phase3ak_missing_executable_depth_blocks_paper_ready(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_window(
            session,
            ticker="KXBTC-NO-DEPTH",
            probability="0.90",
            best_price="0.50",
            spread="0.02",
            liquidity_score="90",
            orderbook_json={
                "orderbook_fp": {
                    "yes_dollars": [["0.48", "20"]],
                    "no_dollars": [],
                }
            },
        )

        payload = build_crypto_window_sync(session, settings=_settings(tmp_path))

    row = payload["rows"][0]
    assert row["raw_expected_value"] == "0.40"
    assert row["book_usable"] is False
    assert row["book_has_executable_depth"] is False
    assert row["readiness_reason"] == "LIQUIDITY_TOO_LOW"
    assert payload["summary"]["liquidity_pass"] == 0
    assert payload["summary"]["paper_ready_opportunities"] == 0


def test_phase3ak_phase3s_low_score_reports_skip(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_window(
            session,
            ticker="KXBTC-LOW-SCORE",
            probability="0.90",
            best_price="0.50",
            spread="0.02",
            liquidity_score="90",
            opportunity_score="20",
        )

        payload = build_crypto_window_sync(session, settings=_settings(tmp_path))

    row = payload["rows"][0]
    assert row["book_usable"] is True
    assert row["phase3s_score_pass"] is False
    assert row["readiness_reason"] == "PHASE_3S_SKIP"
    assert payload["primary_blocker"] == "PHASE_3S_SKIP"
    assert payload["summary"]["positive_ev_blocker_counts"]["PHASE_3S_SKIP"] == 1
    assert payload["readiness_funnel"]["phase3s_proceed"] == 0


def test_phase3ak_clean_book_without_sizing_reports_phase3m_zero_size(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_window(
            session,
            ticker="KXBTC-NO-SIZE",
            probability="0.90",
            best_price="0.50",
            spread="0.02",
            liquidity_score="90",
        )

        payload = build_crypto_window_sync(session, settings=_settings(tmp_path))

    row = payload["rows"][0]
    assert row["book_usable"] is True
    assert row["phase3s_score_pass"] is True
    assert row["phase3m_nonzero_size"] is False
    assert row["readiness_reason"] == "PHASE_3M_ZERO_SIZE"
    assert payload["primary_blocker"] == "PHASE_3M_ZERO_SIZE"
    assert payload["readiness_funnel"]["phase3m_nonzero_size"] == 0
    assert payload["readiness_funnel"]["paper_ready_opportunities"] == 0


def test_phase3ak_sizing_without_risk_reports_phase3n_block(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_window(
            session,
            ticker="KXBTC-NO-RISK",
            probability="0.90",
            best_price="0.50",
            spread="0.02",
            liquidity_score="90",
            phase3m_contracts=2,
        )

        payload = build_crypto_window_sync(session, settings=_settings(tmp_path))

    row = payload["rows"][0]
    assert row["phase3m_nonzero_size"] is True
    assert row["phase3n_approved"] is False
    assert row["readiness_reason"] == "PHASE_3N_RISK_BLOCK"
    assert payload["primary_blocker"] == "PHASE_3N_RISK_BLOCK"
    assert payload["readiness_funnel"]["phase3m_nonzero_size"] == 1
    assert payload["readiness_funnel"]["phase3n_approved"] == 0
    assert payload["readiness_funnel"]["paper_ready_opportunities"] == 0


def test_phase3ak_sizing_and_risk_approval_reports_paper_ready(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_window(
            session,
            ticker="KXBTC-PAPER-READY",
            probability="0.90",
            best_price="0.50",
            spread="0.02",
            liquidity_score="90",
            phase3m_contracts=2,
            phase3n_action="ALLOW",
            phase3n_contracts=2,
        )

        payload = build_crypto_window_sync(session, settings=_settings(tmp_path))

    row = payload["rows"][0]
    assert row["phase3m_proposed_contracts"] == 2
    assert row["phase3n_approved_contracts"] == 2
    assert row["readiness_reason"] == "PAPER_READY"
    assert payload["primary_blocker"] == "PAPER_READY"
    assert payload["readiness_funnel"]["phase3m_nonzero_size"] == 1
    assert payload["readiness_funnel"]["phase3n_approved"] == 1
    assert payload["readiness_funnel"]["paper_ready_opportunities"] == 1


def test_phase3ak_repeated_sync_is_idempotent(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_window(session, ticker="KXBTC-IDEMPOTENT")

        first = build_crypto_window_sync(session, settings=_settings(tmp_path))
        second = build_crypto_window_sync(session, settings=_settings(tmp_path))

    assert [row["window_key"] for row in first["rows"]] == [
        row["window_key"] for row in second["rows"]
    ]
    assert first["idempotency"]["duplicate_window_rows"] == 0
    assert second["idempotency"]["duplicate_window_rows"] == 0


def test_phase3ak_stale_watcher_heartbeat_marks_runner_stale(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    status_path = Path(tmp_path) / "status.json"
    report_path = Path(tmp_path) / "report.json"
    old = utc_now() - timedelta(minutes=40)
    status_path.write_text(
        json.dumps(
            {
                "guard": {
                    "status": "RUNNING",
                    "running": True,
                    "latest_generated_at": old.isoformat(),
                }
            }
        ),
        encoding="utf-8",
    )
    report_path.write_text(json.dumps({"generated_at": old.isoformat(), "summary": {}}), encoding="utf-8")
    with session_factory() as session:
        payload = build_crypto_watch_status(
            session,
            watch_status_path=status_path,
            watch_report_path=report_path,
            settings=_settings(tmp_path),
        )

    assert payload["runner_state"] == "RUNNER_STALE"
    assert payload["primary_blocker"] == "WINDOW_SYNC_STALE"


def test_phase3ak_overdue_cycle_distinguishes_running_watcher(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    status_path = Path(tmp_path) / "status.json"
    report_path = Path(tmp_path) / "report.json"
    old = utc_now() - timedelta(minutes=25)
    status_path.write_text(
        json.dumps(
            {
                "generated_at": utc_now().isoformat(),
                "guard": {
                    "status": "RUNNING",
                    "running": True,
                    "pid": 5151,
                    "latest_generated_at": old.isoformat(),
                    "stale_report": True,
                    "seconds_until_timeout": 600,
                },
                "process": {"pid_running": True},
            }
        ),
        encoding="utf-8",
    )
    report_path.write_text(json.dumps({"generated_at": old.isoformat(), "summary": {}}), encoding="utf-8")
    with session_factory() as session:
        payload = build_crypto_watch_status(
            session,
            watch_status_path=status_path,
            watch_report_path=report_path,
            settings=_settings(tmp_path),
        )

    assert payload["runner_state"] == "RUNNING_CYCLE_OVERDUE"
    assert payload["watch_state"] == "RUNNING_CYCLE_OVERDUE"
    assert payload["primary_blocker"] == "WINDOW_SYNC_STALE"
    assert payload["runner_heartbeat"]["cycle_overdue_seconds"] > 0
    assert "in-flight cycle" in payload["next_action"]


def test_phase3ak_watch_status_exposes_top_level_counts(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_window(
            session,
            ticker="KXBTC-CURRENT-STALE",
            snapshot_delta=timedelta(minutes=-90),
        )
        payload = build_crypto_watch_status(
            session,
            watch_status_path=Path(tmp_path) / "missing-status.json",
            watch_report_path=Path(tmp_path) / "missing-report.json",
            settings=_settings(tmp_path),
        )

    assert payload["current_active_window_rows"] == 1
    assert payload["stale_quote_rows"] == 1
    assert payload["paper_ready_candidates"] == 0
    assert payload["positive_ev_rows"] == 0
    assert payload["primary_gap"] == "WINDOW_SYNC_STALE"


def test_phase3ak_market_data_refresh_blocks_active_writer(tmp_path, monkeypatch) -> None:
    session_factory = _session_factory(tmp_path)

    def fake_monitor(*, settings=None):
        return {
            "status": "WRITER_ACTIVE",
            "safe_to_start_write": False,
            "current_writer_pid": 123,
            "current_writer_command": "kalshi-bot phase3bc-r5-crypto-freshness-watch",
            "recommended_next_action": "wait",
            "long_job_status": {},
        }

    monkeypatch.setattr("kalshi_predictor.phase3ak.db_writer_monitor", fake_monitor)
    monkeypatch.setattr("kalshi_predictor.phase3ak._active_writer_process", lambda: None)
    with session_factory() as session:
        payload = build_market_data_refresh_status(session, settings=_settings(tmp_path))

    assert payload["state"] == "BLOCKED_BY_ACTIVE_WRITER"
    assert payload["refresh_started"] is False
    assert payload["active_writer"]["writer_name"] == "crypto_watcher"


def test_phase3ak_market_data_refresh_propagates_rate_limit_state(tmp_path, monkeypatch) -> None:
    session_factory = _session_factory(tmp_path)
    output_dir = Path(tmp_path) / "phase_3ak"

    def fake_monitor(*, settings=None):
        return {
            "status": "CLEAR",
            "safe_to_start_write": True,
            "current_writer_pid": None,
            "current_writer_command": None,
            "recommended_next_action": "safe",
            "long_job_status": {},
        }

    def fake_refresh(*args, **kwargs):
        assert kwargs["market_limit"] == 150
        assert kwargs["market_max_pages"] == 1
        assert kwargs["crypto_market_scan_limit"] == 2500
        assert kwargs["crypto_link_limit"] == 500
        path = kwargs["output_dir"] / "phase3bc_r3_active_crypto_refresh.json"
        markdown_path = kwargs["output_dir"] / "phase3bc_r3_active_crypto_refresh.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "summary": {
                        "kalshi_api_status": "RATE_LIMITED_KALSHI_API",
                        "data_complete": False,
                    },
                    "rate_limit": {
                        "status": "RATE_LIMITED_RETRY_EXHAUSTED",
                        "blocker": "RATE_LIMITED_KALSHI_API",
                        "rate_limited": True,
                        "retry_count": 3,
                        "total_sleep_seconds": 7.0,
                        "rows_fetched_before_limit": 4,
                        "data_completeness": "partial",
                        "endpoints": [],
                    },
                }
            ),
            encoding="utf-8",
        )
        markdown_path.write_text("# fake refresh\n", encoding="utf-8")
        return Phase3AKArtifactSet(kwargs["output_dir"], path, markdown_path)

    monkeypatch.setattr("kalshi_predictor.phase3ak.db_writer_monitor", fake_monitor)
    monkeypatch.setattr("kalshi_predictor.phase3ak._active_writer_process", lambda: None)
    monkeypatch.setattr(
        "kalshi_predictor.phase3ak.write_phase3bc_r3_active_crypto_refresh_report",
        fake_refresh,
    )
    with session_factory() as session:
        payload = build_market_data_refresh_status(
            session,
            output_dir=output_dir,
            settings=_settings(tmp_path),
        )

    assert payload["state"] == "RATE_LIMITED_KALSHI_API"
    assert payload["refresh_started"] is True
    assert payload["refresh_completed"] is True
    assert payload["refresh_summary"]["data_complete"] is False
    assert payload["refresh_summary"]["rate_limit"]["blocker"] == "RATE_LIMITED_KALSHI_API"
    assert "bounded market-data-refresh" in payload["next_action"]


def test_phase3ak_top_strip_uses_data_watermark_not_render_time(tmp_path) -> None:
    top_strip_path = Path(tmp_path) / "top_strip_status.json"
    watermark = utc_now() - timedelta(hours=2)
    top_strip_path.write_text(
        json.dumps(
            {
                "market_data_state": "STALE",
                "data_watermark": watermark.isoformat(),
                "state": "BLOCKED_BY_ACTIVE_WRITER",
                "blocked_reason": "BLOCKED_BY_ACTIVE_WRITER",
                "active_writer_name": "crypto_watcher",
                "active_writer_pid": 123,
            }
        ),
        encoding="utf-8",
    )
    context = build_default_shell_context(_settings(tmp_path))

    _apply_phase3ak_top_strip_context(context, path=top_strip_path)

    assert context["market_freshness"]["code"] == "stale"
    assert context["market_freshness"]["age_seconds"] >= 7_000
    assert "crypto_watcher" in context["market_freshness"]["description"]


def test_phase3ak_top_strip_applies_when_shell_snapshot_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    top_strip_dir = Path("reports/phase_3ak")
    top_strip_dir.mkdir(parents=True)
    watermark = utc_now() - timedelta(minutes=3)
    (top_strip_dir / "top_strip_status.json").write_text(
        json.dumps(
            {
                "market_data_state": "FRESH",
                "data_watermark": watermark.isoformat(),
                "state": "REFRESH_COMPLETED",
            }
        ),
        encoding="utf-8",
    )

    context = load_shell_status_context(
        snapshot_path=Path("missing_shell_status_snapshot.json"),
        settings=_settings(tmp_path),
    )

    assert context["market_freshness"]["code"] == "fresh"
    assert context["market_freshness"]["age_seconds"] < 300
    assert context["phase3ak_top_strip_status"]["market_data_state"] == "FRESH"


def test_phase3ak_cli_commands_are_registered() -> None:
    runner = CliRunner()
    for command in (
        "crypto-window-sync",
        "crypto-watch-status",
        "market-data-refresh",
        "phase-3ak-report",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert command in result.output


def test_phase3ak_report_uses_existing_artifacts_without_rebuilding(tmp_path, monkeypatch) -> None:
    output_dir = Path(tmp_path) / "phase_3ak"
    output_dir.mkdir()
    (output_dir / "crypto_window_sync.json").write_text(
        json.dumps(
            {
                "generated_at": utc_now().isoformat(),
                "summary": {"active_crypto_markets": 1, "active_windows": 1},
                "readiness_funnel": {"paper_ready_opportunities": 0},
                "next_action": "cached window",
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "crypto_watch_status.json").write_text(
        json.dumps(
            {
                "generated_at": utc_now().isoformat(),
                "runner_state": "RUNNING",
                "primary_blocker": "LIQUIDITY_TOO_LOW",
                "watch_state": "WAITING_FOR_EXECUTION_QUALITY",
                "next_action": "cached watch",
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "market_data_refresh_status.json").write_text(
        json.dumps(
            {
                "state": "BLOCKED_BY_ACTIVE_WRITER",
                "generated_at": utc_now().isoformat(),
                "data_watermark": {
                    "state": "FRESH",
                    "latest_market_snapshot_at": utc_now().isoformat(),
                    "age_minutes": "1",
                    "freshness_threshold_minutes": "15",
                },
                "active_writer": {"active_writer": True, "writer_name": "crypto_watcher", "pid": 123},
                "next_action": "cached market",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "kalshi_predictor.phase3ak.write_crypto_window_sync_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not rebuild window artifact")),
    )
    monkeypatch.setattr(
        "kalshi_predictor.phase3ak.write_crypto_watch_status_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not rebuild watch artifact")),
    )
    monkeypatch.setattr(
        "kalshi_predictor.phase3ak.build_market_data_refresh_status",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not rebuild market artifact")),
    )
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        artifacts = write_phase_3ak_report(
            session,
            output=Path(tmp_path) / "phase_3ak_report.md",
            output_dir=output_dir,
            settings=_settings(tmp_path),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["artifact_sources"]["crypto_window_sync"] == "existing_artifact"
    assert payload["artifact_sources"]["crypto_watch_status"] == "existing_artifact"
    assert payload["artifact_sources"]["market_data_refresh_status"] == "existing_artifact"
    assert payload["watch_status"]["primary_blocker"] == "LIQUIDITY_TOO_LOW"


def test_phase3ak_report_rebuilds_stale_market_status_only(tmp_path, monkeypatch) -> None:
    output_dir = Path(tmp_path) / "phase_3ak"
    output_dir.mkdir()
    (output_dir / "crypto_window_sync.json").write_text(
        json.dumps(
            {
                "generated_at": utc_now().isoformat(),
                "summary": {},
                "readiness_funnel": {},
                "next_action": "cached window",
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "crypto_watch_status.json").write_text(
        json.dumps(
            {
                "generated_at": utc_now().isoformat(),
                "runner_state": "RUNNING",
                "primary_blocker": "LIQUIDITY_TOO_LOW",
                "watch_state": "WAITING_FOR_EXECUTION_QUALITY",
                "next_action": "cached watch",
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "market_data_refresh_status.json").write_text(
        json.dumps(
            {
                "state": "BLOCKED_BY_ACTIVE_WRITER",
                "generated_at": (utc_now() - timedelta(minutes=10)).isoformat(),
                "data_watermark": {"state": "STALE"},
                "active_writer": {"active_writer": True},
            }
        ),
        encoding="utf-8",
    )

    def fake_market_status(*args, **kwargs):
        return {
            "state": "READY_TO_REFRESH",
            "data_watermark": {
                "state": "FRESH",
                "latest_market_snapshot_at": utc_now().isoformat(),
                "age_minutes": "0",
                "freshness_threshold_minutes": "15",
            },
            "active_writer": {"active_writer": False},
            "next_action": "rebuilt market",
        }

    monkeypatch.setattr("kalshi_predictor.phase3ak.build_market_data_refresh_status", fake_market_status)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        artifacts = write_phase_3ak_report(
            session,
            output=Path(tmp_path) / "phase_3ak_report.md",
            output_dir=output_dir,
            settings=_settings(tmp_path),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["artifact_sources"]["market_data_refresh_status"] == "status_only_rebuilt_stale_or_missing_artifact"
    assert payload["market_data_state"] == "STATUS_ONLY_REFRESH_NOT_STARTED"


def test_phase3ak_report_rebuilds_stale_window_and_watch_artifacts(
    tmp_path,
    monkeypatch,
) -> None:
    output_dir = Path(tmp_path) / "phase_3ak"
    output_dir.mkdir()
    stale = utc_now() - timedelta(minutes=10)
    (output_dir / "crypto_window_sync.json").write_text(
        json.dumps(
            {
                "generated_at": stale.isoformat(),
                "summary": {"active_windows": 1},
                "readiness_funnel": {"paper_ready_opportunities": 0},
                "next_action": "stale window",
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "crypto_watch_status.json").write_text(
        json.dumps(
            {
                "generated_at": stale.isoformat(),
                "runner_state": "RUNNER_STALE",
                "primary_blocker": "WINDOW_SYNC_STALE",
                "watch_state": "RUNNER_STALE",
                "next_action": "stale watch",
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "market_data_refresh_status.json").write_text(
        json.dumps(
            {
                "state": "BLOCKED_BY_ACTIVE_WRITER",
                "generated_at": utc_now().isoformat(),
                "data_watermark": {
                    "state": "FRESH",
                    "latest_market_snapshot_at": utc_now().isoformat(),
                    "age_minutes": "1",
                    "freshness_threshold_minutes": "15",
                },
                "active_writer": {"active_writer": True, "writer_name": "crypto_watcher", "pid": 123},
                "next_action": "cached market",
            }
        ),
        encoding="utf-8",
    )

    def fake_window_report(*args, **kwargs):
        path = kwargs["output_dir"] / "crypto_window_sync.json"
        rows_path = kwargs["output_dir"] / "crypto_windows.json"
        markdown_path = kwargs["output_dir"] / "crypto_window_sync.md"
        path.write_text(
            json.dumps(
                {
                    "generated_at": utc_now().isoformat(),
                    "summary": {"active_windows": 2},
                    "readiness_funnel": {"paper_ready_opportunities": 0},
                    "next_action": "rebuilt window",
                }
            ),
            encoding="utf-8",
        )
        rows_path.write_text("[]", encoding="utf-8")
        markdown_path.write_text("# rebuilt window\n", encoding="utf-8")
        return Phase3AKArtifactSet(kwargs["output_dir"], path, markdown_path, rows_path)

    def fake_watch_report(*args, **kwargs):
        path = kwargs["output_dir"] / "crypto_watch_status.json"
        markdown_path = kwargs["output_dir"] / "crypto_watch_status.md"
        path.write_text(
            json.dumps(
                {
                    "generated_at": utc_now().isoformat(),
                    "runner_state": "RUNNING",
                    "primary_blocker": "LIQUIDITY_TOO_LOW",
                    "watch_state": "WAITING_FOR_EXECUTION_QUALITY",
                    "next_action": "rebuilt watch",
                }
            ),
            encoding="utf-8",
        )
        markdown_path.write_text("# rebuilt watch\n", encoding="utf-8")
        return Phase3AKArtifactSet(kwargs["output_dir"], path, markdown_path)

    monkeypatch.setattr(
        "kalshi_predictor.phase3ak.write_crypto_window_sync_report",
        fake_window_report,
    )
    monkeypatch.setattr(
        "kalshi_predictor.phase3ak.write_crypto_watch_status_report",
        fake_watch_report,
    )
    monkeypatch.setattr(
        "kalshi_predictor.phase3ak.build_market_data_refresh_status",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not rebuild market artifact")),
    )
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        artifacts = write_phase_3ak_report(
            session,
            output=Path(tmp_path) / "phase_3ak_report.md",
            output_dir=output_dir,
            settings=_settings(tmp_path),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["artifact_sources"]["crypto_window_sync"] == "rebuilt_stale_artifact"
    assert payload["artifact_sources"]["crypto_watch_status"] == "rebuilt_stale_artifact"
    assert payload["artifact_sources"]["market_data_refresh_status"] == "existing_artifact"
    assert payload["window_summary"]["active_windows"] == 2
    assert payload["watch_status"]["primary_blocker"] == "LIQUIDITY_TOO_LOW"


def _seed_crypto_window(
    session,
    *,
    ticker: str,
    close_delta: timedelta = timedelta(hours=1),
    snapshot_delta: timedelta = timedelta(minutes=-1),
    probability: str = "0.40",
    best_price: str = "0.50",
    spread: str = "0.02",
    liquidity_score: str = "80",
    opportunity_score: str = "80",
    orderbook_json: dict | None = None,
    phase3m_contracts: int | None = None,
    phase3n_action: str | None = None,
    phase3n_contracts: int | None = None,
) -> None:
    now = utc_now()
    close_time = now + close_delta
    captured_at = now + snapshot_delta
    market_json = {
        "ticker": ticker,
        "status": "open",
        "title": "Will Bitcoin be above $100,000?",
        "series_ticker": "KXBTC",
        "event_ticker": f"{ticker}-EVENT",
        "close_time": close_time.isoformat(),
        "yes_bid_dollars": "0.48",
        "yes_ask_dollars": "0.50",
        "no_bid_dollars": "0.49",
        "no_ask_dollars": "0.51",
    }
    best_price_decimal = Decimal(best_price)
    spread_decimal = Decimal(spread)
    yes_bid = max(Decimal("0.01"), best_price_decimal - spread_decimal)
    no_bid = max(Decimal("0.01"), Decimal("1") - best_price_decimal)
    resolved_orderbook_json = orderbook_json or {
        "orderbook_fp": {
            "yes_dollars": [[str(yes_bid), "20"]],
            "no_dollars": [[str(no_bid), "20"]],
        }
    }
    insert_market_snapshot(
        session,
        market_json,
        resolved_orderbook_json,
        captured_at=captured_at,
    )
    session.add(
        MarketLeg(
            ticker=ticker,
            leg_index=0,
            parsed_at=now,
            side="yes",
            category="crypto",
            market_type="target_price",
            entity_name="Bitcoin",
            operator="above",
            threshold_value="100000",
            unit="usd",
            confidence="1.0",
            raw_text="Bitcoin above $100,000",
            reason="test",
            raw_json=encode_json({"symbol": "BTC"}),
        )
    )
    insert_crypto_market_link(
        session,
        ticker=ticker,
        symbol="BTC",
        confidence="1.0",
        reason="test",
        raw_json={"ticker": ticker},
        detected_at=now,
    )
    insert_crypto_features(
        session,
        symbol="BTC",
        source="fixture",
        generated_at=now,
        window_minutes=15,
        features={"price": "100000", "trend_direction": "UP"},
    )
    forecast = insert_forecast(
        session,
        ForecastOutput(
            ticker=ticker,
            forecasted_at=now,
            model_name="crypto_v2",
            yes_probability=Decimal(probability),
            market_mid_probability=Decimal("0.50"),
            best_yes_bid=Decimal("0.48"),
            best_yes_ask=Decimal(best_price),
            feature_json={},
            notes="fixture",
        ),
    )
    session.add(
        MarketRanking(
            ticker=ticker,
            ranked_at=now,
            title="Will Bitcoin be above $100,000?",
            status="open",
            series_ticker="KXBTC",
            event_ticker=f"{ticker}-EVENT",
            volume="100",
            open_interest="100",
            liquidity="1000",
            spread=spread,
            midpoint="0.50",
            time_to_close_minutes="60",
            forecast_model="crypto_v2",
            forecast_probability=probability,
            best_side=BUY_YES,
            best_price=best_price,
            estimated_edge=str(Decimal(probability) - Decimal(best_price)),
            liquidity_score=liquidity_score,
            spread_score="80",
            time_score="80",
            model_confidence_score="80",
            opportunity_score=opportunity_score,
            reason="fixture",
            raw_json=encode_json({"forecast_id": forecast.id}),
        )
    )
    sizing_id: int | None = None
    if phase3m_contracts is not None:
        sizing = PositionSizingDecisionLog(
            decision_timestamp=now,
            created_at=now,
            version="test",
            mode="PAPER",
            strategy_id="test",
            instrument=ticker,
            ticker=ticker,
            model_name="crypto_v2",
            trade_intent_id=f"intent-{ticker}",
            order_correlation_id=f"corr-{ticker}",
            paper_order_id=None,
            tier="standard" if phase3m_contracts > 0 else "zero",
            composite_score="1.0",
            proposed_contracts=phase3m_contracts,
            live_candidate_contracts=0,
            executed_contracts=0,
            factor_scores_json="{}",
            factor_weights_json="{}",
            adjusted_historical_accuracy="0.60",
            historical_sample_size=200,
            drawdown_utilization="0",
            caps_json="{}",
            limiting_factors_json="[]",
            reason_codes_json="[]",
            fallback_used=0,
            raw_json="{}",
        )
        session.add(sizing)
        session.flush()
        sizing_id = sizing.id
    if phase3n_action is not None:
        proposed_contracts = phase3m_contracts or 0
        approved_contracts = phase3n_contracts if phase3n_contracts is not None else proposed_contracts
        session.add(
            AdvancedRiskDecisionLog(
                decision_timestamp=now,
                created_at=now,
                version="test",
                mode="PAPER",
                action=phase3n_action,
                strategy_id="test",
                model_id="crypto_v2",
                category_id="crypto",
                instrument_id=ticker,
                correlation_group_id=ticker,
                ticker=ticker,
                trade_intent_id=f"intent-{ticker}",
                order_correlation_id=f"corr-{ticker}",
                position_sizing_decision_id=sizing_id,
                paper_order_id=None,
                reservation_id=None,
                phase_3m_tier="standard" if proposed_contracts > 0 else "zero",
                phase_3m_proposed_contracts=proposed_contracts,
                live_candidate_contracts=0,
                executed_contracts=approved_contracts,
                risk_per_contract="1.0",
                planned_trade_risk=str(approved_contracts),
                raw_caps_json="{}",
                bucketed_caps_json="{}",
                limiting_factors_json="[]",
                hard_blocks_json="[]",
                reason_codes_json="[]",
                fallback_used=0,
                raw_json="{}",
            )
        )
    session.commit()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        kalshi_db_url=f"sqlite:///{Path(tmp_path) / 'phase3ak.db'}",
        opportunity_min_edge=Decimal("0.03"),
        opportunity_min_score=Decimal("60"),
        opportunity_max_spread=Decimal("0.10"),
    )


def _session_factory(tmp_path: Path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ak.db'}")
    return get_session_factory(engine)
