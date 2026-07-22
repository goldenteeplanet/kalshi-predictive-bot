import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from kalshi_predictor import cli as cli_module
from kalshi_predictor import phase3bc_r5, phase3bc_r6
from kalshi_predictor.cli import _phase3bc_r5_fast_path_command, app
from kalshi_predictor.config import get_settings
from kalshi_predictor.paper.models import BUY_YES
from kalshi_predictor.phase3bc_r5 import (
    build_phase3bc_r5_payload,
    select_phase3bc_r5_preflight_candidates,
)
from kalshi_predictor.phase3bc_r6 import (
    start_phase3bc_r5_unattended_watch,
    write_phase3bc_r5_status_report,
    write_phase3bc_r5_unattended_guard_report,
)
from kalshi_predictor.scheduler import scheduler_plan
from kalshi_predictor.ui import service as ui_service
from kalshi_predictor.ui.service import crypto_freshness_watch_status
from kalshi_predictor.utils.time import utc_now


def test_phase3bc_r5_can_reuse_existing_r3_report(monkeypatch, tmp_path: Path) -> None:
    r3_dir = tmp_path / "phase3bc_r3"
    r3_dir.mkdir()
    r3_json = r3_dir / "phase3bc_r3_active_crypto_refresh.json"
    r3_markdown = r3_dir / "phase3bc_r3_active_crypto_refresh.md"
    r3_json.write_text(json.dumps({"phase": "3BC-R3", "summary": {}}), encoding="utf-8")
    r3_markdown.write_text("# Existing R3 report\n", encoding="utf-8")

    def fail_if_r3_runs(*args, **kwargs):
        raise AssertionError("R3 refresh must be bypassed for the GH-2 bounded cycle")

    def fake_r7(*args, **kwargs):
        output_dir = kwargs["output_dir"]
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / "phase3bc_r7_crypto_ranking_coverage_repair.json"
        markdown_path = output_dir / "phase3bc_r7_crypto_ranking_coverage_repair.md"
        rows_path = output_dir / "phase3bc_r7_crypto_ranking_coverage_rows.json"
        json_path.write_text(json.dumps({"summary": {}}), encoding="utf-8")
        markdown_path.write_text("# R7\n", encoding="utf-8")
        rows_path.write_text("[]\n", encoding="utf-8")
        return SimpleNamespace(
            output_dir=output_dir,
            json_path=json_path,
            markdown_path=markdown_path,
            rows_path=rows_path,
        )

    def fake_r4(*args, **kwargs):
        output_dir = kwargs["output_dir"]
        phase3bc_output_dir = kwargs["phase3bc_output_dir"]
        output_dir.mkdir(parents=True, exist_ok=True)
        phase3bc_output_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / "phase3bc_r4_crypto_ev_risk_diagnostics.json"
        markdown_path = output_dir / "phase3bc_r4_crypto_ev_risk_diagnostics.md"
        phase3bc_json_path = phase3bc_output_dir / "phase3bc_crypto_clean_opportunity.json"
        phase3bc_rows_path = phase3bc_output_dir / "phase3bc_crypto_clean_rows.json"
        empty_summary = {
            "active_pure_crypto_rows": 0,
            "paper_ready_candidates": 0,
            "positive_ev_rows": 0,
            "clean_execution_rows": 0,
        }
        json_path.write_text(
            json.dumps({"summary": empty_summary, "rows": []}),
            encoding="utf-8",
        )
        markdown_path.write_text("# R4\n", encoding="utf-8")
        phase3bc_json_path.write_text(
            json.dumps({"summary": {}, "rows": []}),
            encoding="utf-8",
        )
        phase3bc_rows_path.write_text("[]\n", encoding="utf-8")
        return SimpleNamespace(
            output_dir=output_dir,
            json_path=json_path,
            markdown_path=markdown_path,
            phase3bc_json_path=phase3bc_json_path,
            phase3bc_rows_path=phase3bc_rows_path,
        )

    monkeypatch.setattr(
        phase3bc_r5,
        "write_phase3bc_r3_active_crypto_refresh_report",
        fail_if_r3_runs,
    )
    monkeypatch.setattr(
        phase3bc_r5,
        "write_phase3bc_r7_crypto_ranking_coverage_repair_report",
        fake_r7,
    )
    monkeypatch.setattr(
        phase3bc_r5,
        "write_phase3bc_r4_crypto_ev_risk_diagnostics_report",
        fake_r4,
    )
    monkeypatch.setattr(phase3bc_r5, "_latest_risk_decisions_by_ticker", lambda *args: {})
    monkeypatch.setattr(
        phase3bc_r5,
        "_write_post_refresh_dashboard_truth",
        lambda *args, **kwargs: {"status": "SKIPPED_TEST"},
    )

    artifacts = phase3bc_r5.write_phase3bc_r5_crypto_freshness_watch_report(
        SimpleNamespace(),
        output_dir=tmp_path / "phase3bc_r5",
        phase3bc_output_dir=tmp_path / "phase3bc",
        phase3bc_r3_output_dir=r3_dir,
        phase3bc_r4_output_dir=tmp_path / "phase3bc_r4",
        phase3bc_r7_output_dir=tmp_path / "phase3bc_r7",
        settings=get_settings(),
        risk_preflight=False,
        ranking_repair=False,
        exact_snapshot_refresh=False,
        skip_phase3bc_r3_refresh=True,
    )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["options"]["skip_phase3bc_r3_refresh"] is True
    assert artifacts.phase3bc_r3_json_path == r3_json


def test_phase3bc_r5_selector_requires_positive_ev_fresh_pure_ready() -> None:
    now = utc_now()
    fresh = now.isoformat()
    stale = (now - timedelta(minutes=16)).isoformat()

    candidates, blocked = select_phase3bc_r5_preflight_candidates(
        [
            _row("KXBTC-CLEAN", expected_value="0.12", latest_ranking_at=fresh),
            _row("KXBTC-NO-EV", expected_value="0", latest_ranking_at=fresh),
            _row(
                "KXBTC-STALE",
                expected_value="0.08",
                latest_snapshot_at=fresh,
                latest_forecast_at=fresh,
                latest_ranking_at=stale,
            ),
            _row(
                "KXBTC-SNAPSHOT-STALE",
                expected_value="0.08",
                latest_snapshot_at=stale,
                latest_forecast_at=fresh,
                latest_ranking_at=fresh,
            ),
            _row("KXBTC-RISK-CURRENT", expected_value="0.05", latest_ranking_at=fresh),
            _row(
                "KXBTC-MIXED",
                expected_value="0.20",
                latest_ranking_at=fresh,
                structure_status="MIXED_CATEGORY",
            ),
        ],
        risk_by_ticker={
            "KXBTC-RISK-CURRENT": {
                "id": 77,
                "decision_timestamp": now.isoformat(),
                "action": "BLOCK",
            }
        },
        freshness_minutes=15,
        now=now,
    )

    assert [row["ticker"] for row in candidates] == ["KXBTC-CLEAN"]
    blocked_by_ticker = {row["ticker"]: row["blocked_reason"] for row in blocked}
    assert blocked_by_ticker["KXBTC-NO-EV"] == "ev_not_positive"
    assert blocked_by_ticker["KXBTC-STALE"] == "ranking_before_forecast"
    assert blocked_by_ticker["KXBTC-SNAPSHOT-STALE"] == "snapshot_stale"
    assert blocked_by_ticker["KXBTC-RISK-CURRENT"] == "phase3n_risk_current"
    assert "KXBTC-MIXED" not in blocked_by_ticker


def test_phase3bc_r5_preflight_blocker_counts_explain_positive_ev_rows() -> None:
    now = utc_now()
    fresh = now.isoformat()
    stale = (now - timedelta(minutes=20)).isoformat()

    candidates, blocked = select_phase3bc_r5_preflight_candidates(
        [
            _row(
                "KXDOGE-LOW-SCORE-ZERO-LIQ",
                expected_value="0.04",
                latest_snapshot_at=stale,
                latest_forecast_at=fresh,
                latest_ranking_at=fresh,
                readiness_status="WATCH_LOW_SCORE",
                final_action="WATCH_ONLY",
                liquidity_score="0",
            ),
            _row(
                "KXETH-LOW-EDGE-RANKING-GAP",
                expected_value="0.02",
                latest_snapshot_at=fresh,
                latest_forecast_at=fresh,
                latest_ranking_at=stale,
                readiness_status="WATCH_LOW_EDGE",
                final_action="WATCH_ONLY",
            ),
        ],
        risk_by_ticker={},
        freshness_minutes=15,
        now=now,
    )

    assert candidates == []
    blocked_by_ticker = {row["ticker"]: row for row in blocked}
    assert blocked_by_ticker["KXDOGE-LOW-SCORE-ZERO-LIQ"]["preflight_blockers"] == [
        "LOW_SCORE",
        "LIQUIDITY_ZERO",
        "SNAPSHOT_STALE",
        "RISK_MISSING",
    ]
    assert blocked_by_ticker["KXETH-LOW-EDGE-RANKING-GAP"]["preflight_blockers"] == [
        "LOW_EDGE",
        "RANKING_GAP",
        "RISK_MISSING",
    ]

    payload = build_phase3bc_r5_payload(
        r3_payload={"summary": {}},
        r7_payload={"summary": {}},
        r4_payload={
            "summary": {
                "active_pure_crypto_rows": 2,
                "paper_ready_candidates": 0,
                "no_positive_ev_rows": 0,
                "missing_or_stale_ranking_rows": 1,
                "true_ranking_gap_after_repair": 1,
                "snapshot_stale_rows": 1,
                "forecast_stale_rows": 0,
                "positive_ev_rows": 2,
                "clean_execution_rows": 1,
                "risk_ready_rows": 0,
                "spread_or_liquidity_blocked_rows": 0,
                "primary_gap": "SNAPSHOT_STALE",
            }
        },
        phase3bc_payload={"summary": {}},
        candidates=candidates,
        blocked=blocked,
        preflight_results=[],
        risk_preflight=True,
        options={},
        reports={},
    )

    counts = payload["summary"]["preflight_blocker_counts"]
    assert counts["LOW_SCORE"] == 1
    assert counts["LOW_EDGE"] == 1
    assert counts["LIQUIDITY_ZERO"] == 1
    assert counts["SNAPSHOT_STALE"] == 1
    assert counts["RANKING_GAP"] == 1
    assert counts["RISK_MISSING"] == 2
    assert payload["summary"]["positive_ev_blocked_preflight_rows"] == 2
    assert payload["summary"]["positive_ev_no_executable_book_rows"] == 1
    assert payload["summary"]["positive_ev_liquidity_positive_rows"] == 1
    assert payload["summary"]["positive_ev_clean_book_rows"] == 1
    assert payload["summary"]["positive_ev_snapshot_stale_rows"] == 1
    assert payload["summary"]["positive_ev_clean_book_risk_missing_rows"] == 1
    assert payload["summary"]["liquidity_actionability_state"] == "CLEAN_BOOK_WAITING_FOR_RISK"
    assert payload["summary"]["watch_state"] == "REFRESH_RANKINGS"


def test_phase3bc_r5_liquidity_actionability_ignores_expired_positive_ev(
    monkeypatch,
) -> None:
    fixed_now = datetime(2026, 7, 1, 22, tzinfo=UTC)
    monkeypatch.setattr(phase3bc_r5, "utc_now", lambda: fixed_now)

    payload = build_phase3bc_r5_payload(
        r3_payload={"summary": {}},
        r7_payload={"summary": {}},
        r4_payload={
            "summary": {
                "active_pure_crypto_rows": 2,
                "paper_ready_candidates": 0,
                "no_positive_ev_rows": 0,
                "missing_or_stale_ranking_rows": 0,
                "true_ranking_gap_after_repair": 0,
                "snapshot_stale_rows": 0,
                "forecast_stale_rows": 0,
                "positive_ev_rows": 1,
                "clean_execution_rows": 0,
                "risk_ready_rows": 0,
                "spread_or_liquidity_blocked_rows": 1,
            }
        },
        phase3bc_payload={"summary": {}},
        candidates=[],
        blocked=[
            {
                **_row(
                    "KXDOGE-26JUL0106-T0.0050000",
                    expected_value="0.01",
                    latest_ranking_at=fixed_now.isoformat(),
                    readiness_status="WATCH_LOW_EDGE",
                    final_action="WATCH_ONLY",
                    liquidity_score="0",
                ),
                "blocked_reason": "WATCH_LOW_EDGE",
                "preflight_blockers": ["LIQUIDITY_ZERO"],
            },
            {
                **_row(
                    "KXETH-26JUL0217-T2309.99",
                    expected_value="0.02",
                    latest_ranking_at=fixed_now.isoformat(),
                    readiness_status="WATCH_LOW_SCORE",
                    final_action="WATCH_ONLY",
                    liquidity_score="0",
                ),
                "blocked_reason": "WATCH_LOW_SCORE",
                "preflight_blockers": ["LIQUIDITY_ZERO"],
            },
        ],
        preflight_results=[],
        risk_preflight=True,
        options={},
        reports={},
    )

    summary = payload["summary"]
    assert summary["positive_ev_rows"] == 1
    assert summary["positive_ev_current_actionability_rows"] == 1
    assert summary["positive_ev_expired_window_rows"] == 1
    assert summary["positive_ev_no_executable_book_rows"] == 1
    assert [row["ticker"] for row in payload["positive_ev_expired_window_examples"]] == [
        "KXDOGE-26JUL0106-T0.0050000"
    ]
    assert [row["ticker"] for row in payload["positive_ev_no_executable_book_examples"]] == [
        "KXETH-26JUL0217-T2309.99"
    ]
    assert summary["watch_state"] == "WAITING_FOR_EXECUTABLE_BOOK"


def test_phase3bc_r5_ev_calibration_surfaces_near_misses_without_preflight(
    monkeypatch,
) -> None:
    fixed_now = datetime(2026, 7, 1, 22, tzinfo=UTC)
    monkeypatch.setattr(phase3bc_r5, "utc_now", lambda: fixed_now)

    near_miss = {
        **_row(
            "KXBTC-26JUL0123-B110000",
            expected_value="-0.004",
            latest_ranking_at=fixed_now.isoformat(),
            liquidity_score="60",
        ),
        "freshness_issue": "FRESH",
        "active_window_status": "CURRENT_OR_UNKNOWN",
        "ticker_close_time_utc": "2026-07-01T23:00:00+00:00",
        "blocking_gates": ["ev_not_positive"],
        "price_improvement_needed_for_positive_ev": "0.4",
        "what_would_make_paper_ready": [
            "Best ask must improve by about 0.4 cents or model probability must rise."
        ],
    }
    deeper_negative = {
        **_row(
            "KXETH-26JUL0123-T3500",
            expected_value="-0.025",
            latest_ranking_at=fixed_now.isoformat(),
            liquidity_score="80",
        ),
        "freshness_issue": "FRESH",
        "active_window_status": "CURRENT_OR_UNKNOWN",
        "ticker_close_time_utc": "2026-07-01T23:00:00+00:00",
        "blocking_gates": ["ev_not_positive"],
        "price_improvement_needed_for_positive_ev": "2.5",
    }

    payload = build_phase3bc_r5_payload(
        r3_payload={"summary": {}},
        r7_payload={"summary": {}},
        r4_payload={
            "summary": {
                "active_pure_crypto_rows": 2,
                "current_active_window_rows": 2,
                "paper_ready_candidates": 0,
                "no_positive_ev_rows": 2,
                "missing_or_stale_ranking_rows": 0,
                "true_ranking_gap_after_repair": 0,
                "snapshot_stale_rows": 0,
                "forecast_stale_rows": 0,
                "positive_ev_rows": 0,
                "clean_execution_rows": 2,
                "risk_ready_rows": 0,
                "spread_or_liquidity_blocked_rows": 0,
                "primary_gap": "EV_NOT_POSITIVE",
            },
            "top_blocked_rows": [deeper_negative, near_miss],
        },
        phase3bc_payload={"summary": {}},
        candidates=[],
        blocked=[],
        preflight_results=[],
        risk_preflight=True,
        options={},
        reports={},
    )

    summary = payload["summary"]
    assert summary["watch_state"] == "WAITING_FOR_POSITIVE_EV"
    assert summary["ev_calibration_state"] == "NEAR_MISS_NO_POSITIVE_EV"
    assert summary["best_ev_candidate_ticker"] == "KXBTC-26JUL0123-B110000"
    assert summary["best_current_expected_value_cents"] == "-0.4"
    assert summary["best_ev_gap_to_positive_cents"] == "0.4"
    assert summary["ev_near_miss_rows"] == 1
    assert summary["ev_near_miss_liquidity_positive_rows"] == 1
    assert summary["ev_near_miss_clean_execution_rows"] == 1
    assert payload["positive_ev_preflight_candidates"] == []
    assert payload["phase3m_phase3n_preflight_results"] == []
    assert [row["ticker"] for row in payload["ev_near_miss_examples"]] == [
        "KXBTC-26JUL0123-B110000"
    ]
    assert "do not run paper-only preflight" in payload["recommended_next_action"]


def test_phase3bc_r5_post_refresh_updates_dashboard_truth(monkeypatch, tmp_path) -> None:
    from kalshi_predictor import phase3aw

    reports_dir = tmp_path / "reports"
    output_dir = reports_dir / "phase3bc_r5"
    session = object()
    calls: dict[str, object] = {}

    def fake_status_report(*, output_dir: Path):
        calls["status_output_dir"] = output_dir
        return SimpleNamespace(json_path=output_dir / "phase3bc_r5_status.json")

    def fake_dashboard_truth_report(
        session,
        *,
        output_dir: Path,
        reports_dir: Path,
        settings,
        command_args: list[str],
    ):
        calls["truth_output_dir"] = output_dir
        calls["truth_reports_dir"] = reports_dir
        calls["session_seen"] = session
        assert command_args == [
            "phase3bc-r5-crypto-freshness-watch",
            "post-refresh-dashboard-truth",
        ]
        return SimpleNamespace(
            dashboard_truth_path=output_dir / "dashboard_truth.json",
            executive_summary_path=output_dir / "EXECUTIVE_SUMMARY.md",
        )

    monkeypatch.setattr(phase3bc_r6, "write_phase3bc_r5_status_report", fake_status_report)
    monkeypatch.setattr(
        phase3aw,
        "write_phase3aw_dashboard_truth_report",
        fake_dashboard_truth_report,
    )

    result = phase3bc_r5._write_post_refresh_dashboard_truth(
        session,
        output_dir=output_dir,
        settings=SimpleNamespace(),
    )

    assert result["status"] == "REFRESHED"
    assert result["r5_status_json"] == str(output_dir / "phase3bc_r5_status.json")
    assert result["dashboard_truth_json"] == str(reports_dir / "phase3aw" / "dashboard_truth.json")
    assert calls == {
        "status_output_dir": output_dir,
        "truth_output_dir": reports_dir / "phase3aw",
        "truth_reports_dir": reports_dir,
        "session_seen": session,
    }


def test_phase3bc_r5_post_refresh_skips_nonstandard_output_dir(tmp_path) -> None:
    result = phase3bc_r5._write_post_refresh_dashboard_truth(
        object(),
        output_dir=tmp_path / "custom_r5",
        settings=SimpleNamespace(),
    )

    assert result["status"] == "SKIPPED_NON_STANDARD_OUTPUT_DIR"


def test_phase3bc_r5_ev_calibration_positive_rows_keep_strict_gate(monkeypatch) -> None:
    fixed_now = datetime(2026, 7, 1, 22, tzinfo=UTC)
    monkeypatch.setattr(phase3bc_r5, "utc_now", lambda: fixed_now)

    payload = build_phase3bc_r5_payload(
        r3_payload={"summary": {}},
        r7_payload={"summary": {}},
        r4_payload={
            "summary": {
                "active_pure_crypto_rows": 1,
                "current_active_window_rows": 1,
                "paper_ready_candidates": 0,
                "no_positive_ev_rows": 0,
                "missing_or_stale_ranking_rows": 0,
                "true_ranking_gap_after_repair": 0,
                "snapshot_stale_rows": 0,
                "forecast_stale_rows": 0,
                "positive_ev_rows": 1,
                "clean_execution_rows": 1,
                "risk_ready_rows": 0,
                "spread_or_liquidity_blocked_rows": 0,
            },
            "top_blocked_rows": [
                {
                    **_row(
                        "KXDOGE-26JUL0123-T0.0100000",
                        expected_value="0.012",
                        latest_ranking_at=fixed_now.isoformat(),
                    ),
                    "freshness_issue": "FRESH",
                    "active_window_status": "CURRENT_OR_UNKNOWN",
                    "ticker_close_time_utc": "2026-07-01T23:00:00+00:00",
                    "blocking_gates": ["risk_missing"],
                }
            ],
        },
        phase3bc_payload={"summary": {}},
        candidates=[],
        blocked=[],
        preflight_results=[],
        risk_preflight=True,
        options={},
        reports={},
    )

    summary = payload["summary"]
    assert summary["ev_calibration_state"] == "POSITIVE_EV_AVAILABLE"
    assert summary["best_current_expected_value_cents"] == "1.2"
    assert summary["best_ev_gap_to_positive_cents"] == "0.0"
    assert summary["ev_near_miss_rows"] == 0


def test_phase3bc_r5_snapshot_refresh_allows_unrelated_ranking_gaps(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_repair(session, tickers, *, limit):
        observed["tickers"] = tickers
        observed["limit"] = limit
        return {
            "mode": "PAPER_ONLY_EXACT_TICKER_SNAPSHOT_REFRESH",
            "paper_only_safety": "PAPER_ONLY_NO_EXCHANGE_WRITES",
            "live_or_demo_execution": False,
            "order_submission": False,
            "requested": len(tickers),
            "attempted": len(tickers),
            "repaired": 1,
            "status_counts": {"REPAIRED": 1},
            "rows": [],
        }

    monkeypatch.setattr(phase3bc_r5, "repair_crypto_snapshots_for_tickers", fake_repair)

    result = phase3bc_r5._maybe_refresh_exact_snapshots(
        object(),
        {
            "summary": {
                "snapshot_stale_rows": 2,
                "snapshot_missing_rows": 0,
                "true_ranking_gap_after_repair": 99,
                "forecast_stale_rows": 0,
                "forecast_missing_rows": 0,
            },
            "snapshot_freshness_examples": [
                {
                    "ticker": "KXBTC-CLOSED-POSITIVE-EV",
                    "freshness_issue": "SNAPSHOT_STALE",
                    "active_market": False,
                    "structure_status": "PURE_CRYPTO",
                    "market_status": "finalized",
                    "expected_value": "0.50",
                    "liquidity_score": "60",
                    "spread": "0.01",
                    "opportunity_score": "99",
                },
                {
                    "ticker": "KXDOGE-POSITIVE-EV",
                    "freshness_issue": "SNAPSHOT_STALE",
                    "active_market": True,
                    "structure_status": "PURE_CRYPTO",
                    "market_status": "active",
                    "best_price": "0.0200",
                    "expected_value": "0.04",
                    "liquidity_score": "60",
                    "spread": "0.01",
                    "opportunity_score": "20",
                },
                {
                    "ticker": "KXBTC-NEAR-MISS-EV",
                    "freshness_issue": "SNAPSHOT_STALE",
                    "active_market": True,
                    "structure_status": "PURE_CRYPTO",
                    "market_status": "open",
                    "best_price": "0.0200",
                    "expected_value": "-0.005",
                    "liquidity_score": "80",
                    "spread": "0.01",
                    "opportunity_score": "80",
                },
                {
                    "ticker": "KXBTC-NO-BOOK-POSITIVE-EV",
                    "freshness_issue": "SNAPSHOT_STALE",
                    "active_market": True,
                    "structure_status": "PURE_CRYPTO",
                    "market_status": "active",
                    "best_price": "0.0100",
                    "expected_value": "0.10",
                    "liquidity_score": "0",
                    "spread": "0.01",
                    "opportunity_score": "100",
                },
            ],
        },
        enabled=True,
        limit=2,
    )

    assert observed["tickers"] == [
        "KXBTC-NO-BOOK-POSITIVE-EV",
        "KXDOGE-POSITIVE-EV",
    ]
    assert observed["limit"] == 2
    assert (
        result["trigger"]
        == "R23_EXACT_SNAPSHOT_REFRESH_FOR_ACTIONABLE_CRYPTO_CANDIDATES"
    )
    assert result["ranking_gaps_did_not_block_refresh"] is True
    assert result["positive_ev_priority"] is True
    assert result["book_visible_priority"] is True
    assert result["no_book_recheck_priority"] is True
    assert result["candidate_filter"] == (
        "ACTIVE_OPEN_PURE_CRYPTO_EV_NEAR_MISS_OR_STALE_MAINTENANCE"
    )
    assert result["active_open_candidates"] == 3
    assert result["positive_ev_candidates"] == 2
    assert result["near_miss_candidates"] == 1
    assert result["book_visible_candidates"] == 2
    assert result["no_book_recheck_candidates"] == 1
    assert result["closed_or_unknown_candidates_skipped"] == 1
    assert result["order_submission"] is False


def test_phase3bc_r5_snapshot_refresh_skips_all_closed_candidates(monkeypatch) -> None:
    def fail_repair(*args, **kwargs):
        raise AssertionError("closed snapshot candidates should not be refreshed")

    monkeypatch.setattr(phase3bc_r5, "repair_crypto_snapshots_for_tickers", fail_repair)

    result = phase3bc_r5._maybe_refresh_exact_snapshots(
        object(),
        {
            "summary": {
                "snapshot_stale_rows": 1,
                "snapshot_missing_rows": 0,
            },
            "snapshot_freshness_examples": [
                {
                    "ticker": "KXBTC-CLOSED",
                    "freshness_issue": "SNAPSHOT_STALE",
                    "active_market": False,
                    "structure_status": "PURE_CRYPTO",
                    "market_status": "finalized",
                    "expected_value": "0.25",
                    "liquidity_score": "60",
                    "opportunity_score": "95",
                }
            ],
        },
        enabled=True,
        limit=50,
    )

    assert result["status"] == "NO_ACTIVE_OPEN_TICKERS"
    assert result["attempted"] == 0
    assert result["active_open_candidates"] == 0
    assert result["closed_or_unknown_candidates_skipped"] == 1
    assert result["order_submission"] is False


def test_phase3bc_r5_snapshot_refresh_rechecks_near_miss_visible_book(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_repair(session, tickers, *, limit):
        observed["session"] = session
        observed["tickers"] = tickers
        observed["limit"] = limit
        return {
            "mode": "PAPER_ONLY_EXACT_TICKER_SNAPSHOT_REFRESH",
            "paper_only_safety": "PAPER_ONLY_NO_EXCHANGE_WRITES",
            "live_or_demo_execution": False,
            "order_submission": False,
            "requested": len(tickers),
            "attempted": len(tickers),
            "repaired": 1,
            "status_counts": {"REPAIRED": 1},
            "rows": [],
        }

    monkeypatch.setattr(phase3bc_r5, "repair_crypto_snapshots_for_tickers", fake_repair)

    result = phase3bc_r5._maybe_refresh_exact_snapshots(
        "session",
        {
            "summary": {
                "snapshot_stale_rows": 0,
                "snapshot_missing_rows": 0,
                "true_ranking_gap_after_repair": 0,
            },
            "top_blocked_rows": [
                {
                    "ticker": "KXBTC-NEAR-MISS-VISIBLE-BOOK",
                    "active_market": True,
                    "structure_status": "PURE_CRYPTO",
                    "market_status": "active",
                    "best_price": "0.0200",
                    "expected_value": "-0.005",
                    "liquidity_score": "80",
                    "spread": "0.01",
                    "opportunity_score": "80",
                }
            ],
        },
        enabled=True,
        limit=5,
    )

    assert observed["session"] == "session"
    assert observed["tickers"] == ["KXBTC-NEAR-MISS-VISIBLE-BOOK"]
    assert observed["limit"] == 5
    assert result["attempted"] == 1
    assert result["near_miss_candidates"] == 1
    assert result["positive_ev_candidates"] == 0
    assert result["book_visible_candidates"] == 1
    assert result["clean_execution_candidates"] == 1
    assert result["live_or_demo_execution"] is False
    assert result["order_submission"] is False


def test_phase3bc_r5_snapshot_refresh_skips_expired_crypto_ticker_hours() -> None:
    tickers, selection = phase3bc_r5._snapshot_refresh_selection(
        {
            "snapshot_freshness_examples": [
                {
                    "ticker": "KXBTC-26JUN3019-B59050",
                    "freshness_issue": "SNAPSHOT_STALE",
                    "active_market": True,
                    "structure_status": "PURE_CRYPTO",
                    "market_status": "active",
                    "expected_value": "0.50",
                    "liquidity_score": "60",
                    "spread": "0.01",
                    "opportunity_score": "99",
                },
                {
                    "ticker": "KXBTC-26JUL0117-B59050",
                    "freshness_issue": "SNAPSHOT_STALE",
                    "active_market": True,
                    "structure_status": "PURE_CRYPTO",
                    "market_status": "open",
                    "expected_value": "0.10",
                    "liquidity_score": "60",
                    "spread": "0.01",
                    "opportunity_score": "40",
                },
            ]
        },
        limit=50,
        now=datetime(2026, 6, 30, 23, 30, tzinfo=UTC),
    )

    assert tickers == ["KXBTC-26JUL0117-B59050"]
    assert selection["active_open_candidates"] == 1
    assert selection["closed_or_unknown_candidates_skipped"] == 1
    assert selection["skip_reason_counts"]["TICKER_CLOSE_TIME_PASSED"] == 1


def test_phase3bc_r5_snapshot_refresh_includes_positive_ev_no_book_rows() -> None:
    tickers, selection = phase3bc_r5._snapshot_refresh_selection(
        {
            "snapshot_freshness_examples": [
                {
                    "ticker": "KXBTC-ACTIONABLE",
                    "freshness_issue": "SNAPSHOT_STALE",
                    "active_market": True,
                    "structure_status": "PURE_CRYPTO",
                    "market_status": "active",
                    "best_price": "0.0200",
                    "expected_value": "0.02",
                    "liquidity_score": "50",
                    "spread": "0.01",
                    "opportunity_score": "80",
                },
                {
                    "ticker": "KXBTC-NO-BOOK",
                    "freshness_issue": "SNAPSHOT_STALE",
                    "active_market": True,
                    "structure_status": "PURE_CRYPTO",
                    "market_status": "active",
                    "best_price": "0.0100",
                    "expected_value": "0.04",
                    "liquidity_score": "0",
                    "spread": "0.01",
                    "opportunity_score": "99",
                },
                {
                    "ticker": "KXSPORTS-MIXED",
                    "freshness_issue": "SNAPSHOT_STALE",
                    "active_market": True,
                    "structure_status": "MIXED_CATEGORY",
                    "market_status": "active",
                    "expected_value": "0.05",
                    "liquidity_score": "90",
                    "spread": "0.01",
                    "opportunity_score": "100",
                },
                {
                    "ticker": "KXBTC-TOO-NEGATIVE",
                    "freshness_issue": "SNAPSHOT_STALE",
                    "active_market": True,
                    "structure_status": "PURE_CRYPTO",
                    "market_status": "active",
                    "expected_value": "-0.50",
                    "liquidity_score": "80",
                    "spread": "0.01",
                    "opportunity_score": "85",
                },
            ]
        },
        limit=50,
        now=datetime(2026, 7, 1, 12, tzinfo=UTC),
    )

    assert tickers == ["KXBTC-NO-BOOK", "KXBTC-ACTIONABLE", "KXBTC-TOO-NEGATIVE"]
    assert selection["candidate_filter"] == (
        "ACTIVE_OPEN_PURE_CRYPTO_EV_NEAR_MISS_OR_STALE_MAINTENANCE"
    )
    assert selection["active_open_candidates"] == 3
    assert selection["positive_ev_candidates"] == 2
    assert selection["book_visible_candidates"] == 2
    assert selection["no_book_recheck_candidates"] == 1
    assert selection["stale_current_window_maintenance_candidates"] == 1
    assert selection["skip_reason_counts"]["NOT_PURE_CRYPTO"] == 1
    assert "NOT_POSITIVE_OR_NEAR_MISS_EV" not in selection["skip_reason_counts"]


def test_phase3bc_r5_snapshot_refresh_uses_full_r4_freshness_rows() -> None:
    tickers, selection = phase3bc_r5._snapshot_refresh_selection(
        {
            "snapshot_freshness_rows": [
                {
                    "ticker": "KXBTC-FULL-EXPORT-1",
                    "freshness_issue": "SNAPSHOT_STALE",
                    "active_market": True,
                    "structure_status": "PURE_CRYPTO",
                    "market_status": "active",
                    "expected_value": "-0.01",
                    "liquidity_score": "1",
                    "spread": "0.01",
                    "opportunity_score": "30",
                },
                {
                    "ticker": "KXBTC-FULL-EXPORT-2",
                    "freshness_issue": "SNAPSHOT_STALE",
                    "active_market": True,
                    "structure_status": "PURE_CRYPTO",
                    "market_status": "active",
                    "expected_value": "-0.02",
                    "liquidity_score": "1",
                    "spread": "0.01",
                    "opportunity_score": "20",
                },
                {
                    "ticker": "KXBTC-FULL-EXPORT-3",
                    "freshness_issue": "SNAPSHOT_STALE",
                    "active_market": True,
                    "structure_status": "PURE_CRYPTO",
                    "market_status": "active",
                    "expected_value": "-0.03",
                    "liquidity_score": "1",
                    "spread": "0.01",
                    "opportunity_score": "10",
                },
            ],
            "snapshot_freshness_examples": [],
        },
        limit=2,
        now=datetime(2026, 7, 1, 12, tzinfo=UTC),
    )

    assert tickers == ["KXBTC-FULL-EXPORT-1", "KXBTC-FULL-EXPORT-2"]
    assert selection["active_open_candidates"] == 3
    assert selection["unselected_active_open_candidates"] == 1
    assert selection["unselected_reason"] == "EXACT_TICKER_NOT_REFRESHED"
    assert selection["unselected_tickers"] == ["KXBTC-FULL-EXPORT-3"]


def test_phase3bc_r5_payload_exposes_r8_gap_reconciliation_fields() -> None:
    payload = build_phase3bc_r5_payload(
        r3_payload={"summary": {"rankings_inserted": 12}},
        r7_payload={
            "summary": {
                "rankings_inserted": 250,
                "missing_or_stale_ranking_rows_after": 0,
            }
        },
        r4_payload={
            "summary": {
                "active_pure_crypto_rows": 8,
                "current_active_window_rows": 5,
                "expired_crypto_window_rows": 3,
                "paper_ready_candidates": 0,
                "no_positive_ev_rows": 4,
                "missing_or_stale_ranking_rows": 0,
                "true_ranking_gap_after_repair": 0,
                "snapshot_stale_rows": 3,
                "forecast_stale_rows": 0,
                "positive_ev_rows": 4,
                "clean_execution_rows": 5,
                "risk_ready_rows": 1,
                "spread_or_liquidity_blocked_rows": 0,
                "primary_gap": "SNAPSHOT_STALE",
            }
        },
        phase3bc_payload={"summary": {"main_blocker": "WATCH_NO_POSITIVE_EXPECTED_VALUE"}},
        candidates=[],
        blocked=[],
        preflight_results=[],
        risk_preflight=True,
        options={"cadence_minutes": 15},
        reports={},
        exact_snapshot_refresh_result={
            "mode": "PAPER_ONLY_EXACT_TICKER_SNAPSHOT_REFRESH",
            "live_or_demo_execution": False,
            "order_submission": False,
            "attempted": 3,
            "repaired": 2,
            "selected_tickers": ["KXBTC-ACTIONABLE"],
            "candidate_filter": (
                "ACTIVE_OPEN_PURE_CRYPTO_EV_NEAR_MISS_OR_STALE_MAINTENANCE"
            ),
            "active_open_candidates": 1,
            "book_visible_candidates": 1,
            "no_book_recheck_candidates": 1,
            "clean_execution_candidates": 1,
            "positive_ev_candidates": 1,
            "near_miss_candidates": 0,
        },
        stage_timings=[
            {"stage": "phase3bc_r4_diagnostics", "duration_seconds": 1.25},
            {"stage": "phase3bc_r3_refresh", "duration_seconds": 8.5},
        ],
    )

    summary = payload["summary"]
    assert summary["current_active_window_rows"] == 5
    assert summary["expired_crypto_window_rows"] == 3
    assert summary["true_ranking_gap_after_repair"] == 0
    assert summary["snapshot_stale_rows"] == 3
    assert summary["positive_ev_rows"] == 4
    assert summary["clean_execution_rows"] == 5
    assert summary["risk_ready_rows"] == 1
    assert summary["exact_snapshot_refresh_repaired"] == 2
    assert summary["exact_snapshot_refresh_selected"] == 1
    assert summary["exact_snapshot_refresh_book_visible_candidates"] == 1
    assert summary["exact_snapshot_refresh_no_book_recheck_candidates"] == 1
    assert summary["exact_snapshot_refresh_candidate_filter"] == (
        "ACTIVE_OPEN_PURE_CRYPTO_EV_NEAR_MISS_OR_STALE_MAINTENANCE"
    )
    assert summary["watch_state"] == "REFRESH_SNAPSHOTS"
    assert summary["preflight_blocker_counts"]["LOW_SCORE"] == 0
    assert payload["exact_snapshot_refresh_result"]["order_submission"] is False
    assert payload["stage_duration_seconds"]["phase3bc_r4_diagnostics"] == 1.25
    assert payload["stage_durations_seconds"]["phase3bc_r3_refresh"] == 8.5
    assert summary["stage_duration_seconds"]["phase3bc_r3_refresh"] == 8.5
    assert summary["stage_durations_seconds"]["phase3bc_r4_diagnostics"] == 1.25
    assert summary["slowest_stage"] == "phase3bc_r3_refresh"
    assert summary["slowest_stage_seconds"] == "8.5"


def test_phase3bc_r5_classifies_bounded_freshness_backlog_without_hiding_ev_gap() -> None:
    payload = build_phase3bc_r5_payload(
        r3_payload={"summary": {}},
        r7_payload={"summary": {"missing_or_stale_ranking_rows_after": 0}},
        r4_payload={
            "summary": {
                "active_pure_crypto_rows": 8,
                "current_active_window_rows": 5,
                "expired_crypto_window_rows": 0,
                "paper_ready_candidates": 0,
                "no_positive_ev_rows": 5,
                "missing_or_stale_ranking_rows": 0,
                "true_ranking_gap_after_repair": 0,
                "snapshot_stale_rows": 3,
                "forecast_stale_rows": 2,
                "positive_ev_rows": 0,
                "clean_execution_rows": 2,
                "risk_ready_rows": 0,
                "spread_or_liquidity_blocked_rows": 0,
                "primary_gap": "SNAPSHOT_STALE",
                "primary_gap_scope": "CURRENT_ACTIVE_CRYPTO_WINDOWS",
            }
        },
        phase3bc_payload={"summary": {"main_blocker": "WATCH_NO_POSITIVE_EXPECTED_VALUE"}},
        candidates=[],
        blocked=[],
        preflight_results=[],
        risk_preflight=True,
        options={"cadence_minutes": 15},
        reports={},
        exact_snapshot_refresh_result={
            "attempted": 2,
            "repaired": 2,
            "selected_tickers": ["KXBTC-1", "KXBTC-2"],
            "active_open_candidates": 3,
            "unselected_active_open_candidates": 1,
            "unselected_reason": "EXACT_TICKER_NOT_REFRESHED",
            "unselected_tickers": ["KXBTC-3"],
        },
        exact_forecast_refresh_result={
            "status": "COMPLETE",
            "attempted": 2,
            "forecasts_inserted": 2,
            "skipped": 0,
            "selected_tickers": ["KXBTC-1", "KXBTC-2"],
        },
    )

    summary = payload["summary"]
    assert summary["data_freshness_gap_after_refresh"] == "SNAPSHOT_STALE"
    assert summary["primary_gap_after_refresh"] == "EV_NOT_POSITIVE"
    assert summary["snapshot_backlog_status"] == "EXACT_TICKER_NOT_REFRESHED"
    assert summary["forecast_backlog_status"] == (
        "FORECAST_REFRESH_PENDING_AFTER_SNAPSHOT_REFRESH"
    )
    assert summary["data_freshness_complete"] is False
    assert summary["freshness_backlog_blocks_current_positive_ev"] is False
    assert summary["exact_snapshot_refresh_unselected_tickers"] == ["KXBTC-3"]
    assert summary["exact_forecast_refresh_inserted"] == 2


def test_phase3bc_r5_prefers_fresh_positive_ev_edge_block_over_backlog() -> None:
    payload = build_phase3bc_r5_payload(
        r3_payload={"summary": {}},
        r7_payload={"summary": {"missing_or_stale_ranking_rows_after": 0}},
        r4_payload={
            "summary": {
                "active_pure_crypto_rows": 8,
                "current_active_window_rows": 5,
                "expired_crypto_window_rows": 0,
                "paper_ready_candidates": 0,
                "no_positive_ev_rows": 4,
                "missing_or_stale_ranking_rows": 0,
                "true_ranking_gap_after_repair": 0,
                "snapshot_stale_rows": 3,
                "forecast_stale_rows": 0,
                "positive_ev_rows": 1,
                "clean_execution_rows": 2,
                "risk_ready_rows": 0,
                "spread_or_liquidity_blocked_rows": 0,
                "primary_gap": "SNAPSHOT_STALE",
                "primary_gap_scope": "CURRENT_ACTIVE_CRYPTO_WINDOWS",
            }
        },
        phase3bc_payload={"summary": {"main_blocker": "WATCH_LOW_EDGE"}},
        candidates=[],
        blocked=[
            {
                "ticker": "KXDOGE-LOW-EDGE",
                "expected_value": "0.003",
                "expected_value_cents": "0.3",
                "preflight_blockers": ["LOW_EDGE", "LIQUIDITY_ZERO"],
                "freshness_issue": "FRESH",
            }
        ],
        preflight_results=[],
        risk_preflight=True,
        options={"cadence_minutes": 15},
        reports={},
        exact_snapshot_refresh_result={
            "attempted": 2,
            "repaired": 2,
            "selected_tickers": ["KXBTC-1", "KXBTC-2"],
            "active_open_candidates": 3,
            "unselected_active_open_candidates": 1,
            "unselected_reason": "EXACT_TICKER_NOT_REFRESHED",
        },
    )

    summary = payload["summary"]
    assert summary["data_freshness_gap_after_refresh"] == "SNAPSHOT_STALE"
    assert summary["primary_gap_after_refresh"] == "LOW_EDGE_OR_SCORE_BLOCK"
    assert summary["freshness_backlog_blocks_current_positive_ev"] is False


def test_phase3bc_r5_payload_detects_liquidity_emergence_from_previous_report() -> None:
    fresh = utc_now().isoformat()
    previous_payload = {
        "liquidity_watch_rows": [
            {
                "ticker": "KXBTC-POS",
                "clean_title": "Bitcoin Price Market",
                "expected_value": "0.004",
                "expected_value_cents": "0.4",
                "best_price": "0.20",
                "liquidity_score": "0",
                "spread": "0.01",
            },
            {
                "ticker": "KXETH-NEAR",
                "clean_title": "Ethereum Price Market",
                "expected_value": "-0.004",
                "expected_value_cents": "-0.4",
                "best_price": "0.20",
                "liquidity_score": "0",
                "spread": "0.01",
            },
        ],
    }
    positive_row = _row(
        "KXBTC-POS",
        expected_value="0.004",
        latest_ranking_at=fresh,
        liquidity_score="60",
    )
    near_miss_row = _row(
        "KXETH-NEAR",
        expected_value="-0.004",
        latest_ranking_at=fresh,
        liquidity_score="60",
    )

    payload = build_phase3bc_r5_payload(
        r3_payload={"summary": {}},
        r7_payload={"summary": {}},
        r4_payload={
            "summary": {
                "active_pure_crypto_rows": 2,
                "current_active_window_rows": 2,
                "paper_ready_candidates": 0,
                "no_positive_ev_rows": 1,
                "missing_or_stale_ranking_rows": 0,
                "snapshot_stale_rows": 0,
                "forecast_stale_rows": 0,
                "positive_ev_rows": 1,
                "clean_execution_rows": 2,
                "risk_ready_rows": 0,
                "primary_gap": "LIQUIDITY_BLOCKED",
            },
            "top_blocked_rows": [near_miss_row],
        },
        phase3bc_payload={"summary": {}},
        candidates=[],
        blocked=[positive_row],
        preflight_results=[],
        risk_preflight=True,
        options={"cadence_minutes": 15},
        reports={},
        previous_payload=previous_payload,
    )

    summary = payload["summary"]
    assert summary["liquidity_emergence_rows"] == 2
    assert summary["positive_ev_liquidity_emergence_rows"] == 1
    assert summary["near_miss_liquidity_emergence_rows"] == 1
    assert summary["clean_execution_emergence_rows"] == 2
    assert summary["positive_ev_clean_execution_emergence_rows"] == 1
    assert summary["near_miss_clean_book_emergence_rows"] == 1
    assert payload["liquidity_emergence_examples"][0]["transition_label"] == (
        "Liquidity appeared; Clean execution appeared"
    )
    assert {
        row["ticker"] for row in payload["positive_ev_liquidity_emergence_examples"]
    } == {"KXBTC-POS"}
    assert {
        row["ticker"] for row in payload["near_miss_clean_book_emergence_examples"]
    } == {"KXETH-NEAR"}


def test_phase3bc_r5_cli_smoke_no_external_fetches(tmp_path) -> None:
    get_settings.cache_clear()
    runner = CliRunner()
    db_url = f"sqlite:///{Path(tmp_path) / 'phase3bc_r5_cli.db'}"
    output_dir = Path(tmp_path) / "phase3bc_r5"
    phase3bc_output_dir = Path(tmp_path) / "phase3bc"

    result = runner.invoke(
        app,
        [
            "phase3bc-r5-crypto-freshness-watch",
            "--output-dir",
            str(output_dir),
            "--phase3bc-output-dir",
            str(phase3bc_output_dir),
            "--phase3bc-r3-output-dir",
            str(Path(tmp_path) / "phase3bc_r3"),
            "--phase3bc-r4-output-dir",
            str(Path(tmp_path) / "phase3bc_r4"),
            "--phase3bc-r7-output-dir",
            str(Path(tmp_path) / "phase3bc_r7"),
            "--skip-external-crypto-ingest",
            "--skip-open-market-refresh",
            "--diagnose-snapshots",
            "--crypto-series-tickers",
            "KXBTC,KXETH",
            "--crypto-market-scan-limit",
            "25",
            "--crypto-link-limit",
            "10",
            "--forecast-limit",
            "10",
            "--opportunity-limit",
            "10",
            "--phase3bc-limit",
            "10",
            "--no-risk-preflight",
        ],
        env={"KALSHI_DB_URL": db_url},
    )

    get_settings.cache_clear()
    assert result.exit_code == 0
    assert "PAPER ONLY" in result.output
    assert "Order submission/cancel/replace: blocked" in result.output
    payload_path = output_dir / "phase3bc_r5_crypto_freshness_watch.json"
    assert payload_path.exists()
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["mode"] == "PAPER_ONLY_CRYPTO_FRESHNESS_WATCH_AND_POSITIVE_EV_TRIGGER"
    assert payload["live_or_demo_execution"] is False
    assert payload["order_submission"] is False
    assert payload["summary"]["risk_preflight_enabled"] is False
    assert payload["options"]["external_crypto_ingest"] is False
    assert payload["options"]["refresh_open_markets"] is False
    assert payload["options"]["repair_snapshots"] is False
    assert payload["options"]["forecast_current_windows_only"] is True
    assert payload["options"]["generate_opportunity_report"] is False
    assert payload["options"]["near_money_only"] is True
    assert payload["options"]["near_money_per_symbol_limit"] == 40
    assert payload["options"]["near_money_window_limit"] == 20
    assert payload["options"]["snapshot_fetch_concurrency"] == 2


def test_crypto_freshness_watch_status_marks_report_stale(tmp_path) -> None:
    now = utc_now()
    report_path = Path(tmp_path) / "phase3bc_r5_crypto_freshness_watch.json"
    report_path.write_text(
        json.dumps(
            {
                "generated_at": (now - timedelta(minutes=31)).isoformat(),
                "options": {"cadence_minutes": 15},
                "summary": {
                    "watch_state": "WAITING_FOR_POSITIVE_EV",
                    "active_pure_crypto_rows": 5,
                    "current_active_window_rows": 3,
                    "expired_crypto_window_rows": 2,
                    "paper_ready_candidates": 0,
                    "positive_ev_preflight_candidates": 0,
                    "primary_gap_after_refresh": "EV_NOT_POSITIVE",
                    "primary_gap_scope": "CURRENT_ACTIVE_CRYPTO_WINDOWS",
                    "ev_calibration_state": "NEAR_MISS_NO_POSITIVE_EV",
                    "best_current_expected_value_cents": "-0.5",
                    "best_ev_candidate_ticker": "KXBTC-NEAR",
                    "best_ev_gap_to_positive_cents": "0.5",
                    "ev_near_miss_rows": 2,
                    "ev_near_miss_liquidity_positive_rows": 1,
                    "ev_near_miss_clean_execution_rows": 0,
                    "ev_near_miss_band_cents": "1.0",
                    "liquidity_emergence_rows": 1,
                    "positive_ev_liquidity_emergence_rows": 0,
                    "near_miss_liquidity_emergence_rows": 1,
                    "clean_execution_emergence_rows": 1,
                    "positive_ev_clean_execution_emergence_rows": 0,
                    "near_miss_clean_book_emergence_rows": 1,
                },
                "ev_near_miss_examples": [
                    {
                        "ticker": "KXBTC-NEAR",
                        "clean_title": "Bitcoin Price Market",
                        "best_side": "BUY_YES",
                        "best_price": "0.0200",
                        "expected_value_cents": "-0.5",
                        "gap_to_positive_cents": "0.5",
                        "liquidity_score": "0.50",
                        "side_probability": "0.015",
                        "spread": "0.0100",
                        "blocking_gates": ["ev_not_positive"],
                        "what_would_make_paper_ready": [
                            "Best ask must improve by about 0.5 cents."
                        ],
                    }
                ],
                "liquidity_emergence_examples": [
                    {
                        "ticker": "KXBTC-NEAR",
                        "clean_title": "Bitcoin Price Market",
                        "watch_type": "NEAR_MISS",
                        "transition_label": "Liquidity appeared",
                        "best_side": "BUY_YES",
                        "best_price": "0.0200",
                        "expected_value_cents": "-0.5",
                        "gap_to_positive_cents": "0.5",
                        "liquidity_score": "0.50",
                        "previous_liquidity_score": "0",
                        "spread": "0.0100",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    status = crypto_freshness_watch_status(report_path=report_path, now=now)

    assert status["status"] == "STALE"
    assert status["auto_refresh_seconds"] == 0
    assert status["auto_refresh_label"] == "off"
    assert status["watch_state"] == "WAITING_FOR_POSITIVE_EV"
    assert status["watch_state_label"] == "Waiting for positive EV"
    assert status["active_pure_crypto_rows"] == 5
    assert status["current_active_window_rows"] == 3
    assert status["expired_crypto_window_rows"] == 2
    assert status["primary_gap_scope"] == "CURRENT_ACTIVE_CRYPTO_WINDOWS"
    assert status["primary_gap_label"] == "EV not positive"
    assert status["ev_calibration_label"] == "Near misses only"
    assert status["best_current_expected_value_label"] == "-0.5 cents"
    assert status["best_ev_gap_to_positive_label"] == "0.5 cents"
    assert status["ev_near_miss_rows"] == 2
    assert status["ev_near_miss_liquidity_positive_rows"] == 1
    assert status["liquidity_emergence_rows"] == 1
    assert status["near_miss_liquidity_emergence_rows"] == 1
    assert status["clean_execution_emergence_rows"] == 1
    assert "1 watched crypto row" in status["liquidity_emergence_summary"]
    assert status["liquidity_emergence_examples"][0]["ticker"] == "KXBTC-NEAR"
    assert status["liquidity_emergence_examples"][0]["current_liquidity_label"] == "Medium"
    assert status["liquidity_emergence_examples"][0]["previous_liquidity_label"] == "None"
    assert status["near_miss_examples"][0]["ticker"] == "KXBTC-NEAR"
    assert status["near_miss_examples"][0]["price_label"] == "2.0 cents"
    assert status["near_miss_examples"][0]["liquidity_label"] == "Medium"
    assert status["near_miss_examples"][0]["status_label"] == "Book visible"
    assert status["near_miss_examples"][0]["detail_href"] == "/opportunities/KXBTC-NEAR"
    assert "phase3bc-r5-unattended-start" in status["command"]


def test_crypto_freshness_watch_status_explains_stale_stopped_runner(tmp_path) -> None:
    now = utc_now()
    report_path = Path(tmp_path) / "phase3bc_r5_crypto_freshness_watch.json"
    status_path = Path(tmp_path) / "phase3bc_r5_status.json"
    report_path.write_text(
        json.dumps(
            {
                "generated_at": (now - timedelta(minutes=45)).isoformat(),
                "options": {"cadence_minutes": 15},
                "summary": {
                    "watch_state": "REFRESH_SNAPSHOTS",
                    "positive_ev_rows": 3,
                    "paper_ready_candidates": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    status_path.write_text(
        json.dumps(
            {
                "guard": {
                    "status": "STOPPED_WITH_STALE_PID",
                    "running": False,
                    "pid": 1511,
                    "recommended_next_action": (
                        "No crypto watch process is running; stale PID metadata can be overwritten."
                    ),
                }
            }
        ),
        encoding="utf-8",
    )

    status = crypto_freshness_watch_status(
        report_path=report_path,
        status_path=status_path,
        now=now,
    )

    assert status["status"] == "WATCHER_STOPPED"
    assert status["status_label"] == "Watcher stopped"
    assert status["runner_status"] == "STOPPED_WITH_STALE_PID"
    assert status["runner_status_label"] == "Stopped; old PID"
    assert status["runner_running"] is False
    assert status["runner_pid"] == 1511
    assert "no guarded Phase 3BC-R5 watcher process is running" in status["description"]
    assert "stale PID metadata" in status["runner_next_action"]


def test_crypto_freshness_watch_status_auto_refreshes_when_runner_is_active(
    tmp_path,
) -> None:
    now = utc_now()
    report_path = Path(tmp_path) / "phase3bc_r5_crypto_freshness_watch.json"
    status_path = Path(tmp_path) / "phase3bc_r5_status.json"
    report_path.write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(),
                "options": {"cadence_minutes": 15},
                "summary": {
                    "watch_state": "WAITING_FOR_EXECUTABLE_BOOK",
                    "cycle_number": 4,
                    "total_cycles": 32,
                    "best_current_expected_value_cents": "1.7",
                    "best_ev_candidate_ticker": "KXXRP-BOOK",
                    "positive_ev_no_executable_book_rows": 1,
                    "positive_ev_rows": 1,
                    "paper_ready_candidates": 0,
                },
                "positive_ev_no_executable_book_examples": [
                    {
                        "ticker": "KXXRP-BOOK",
                        "clean_title": "XRP target",
                        "best_side": "BUY_YES",
                        "best_price": "0.0100",
                        "expected_value_cents": "1.7",
                        "liquidity_score": "0",
                        "preflight_blockers": [
                            "LOW_EDGE",
                            "LIQUIDITY_ZERO",
                            "RISK_MISSING",
                        ],
                        "spread": "0.0200",
                        "what_would_make_paper_ready": [
                            "Visible ask liquidity must appear."
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    status_path.write_text(
        json.dumps(
            {
                "guard": {
                    "status": "RUNNING",
                    "running": True,
                    "pid": 5151,
                    "elapsed_seconds": 3600,
                    "duration_budget_seconds": 28800,
                }
            }
        ),
        encoding="utf-8",
    )

    status = crypto_freshness_watch_status(
        report_path=report_path,
        status_path=status_path,
        now=now,
    )

    assert status["status"] == "FRESH"
    assert status["auto_refresh_seconds"] == 60
    assert status["auto_refresh_label"] == "60s"
    assert status["runner_running"] is True
    assert status["watch_progress_label"] == "4 / 32 (12.5%)"
    assert status["elapsed_label"] == "1.0h"
    assert status["eta_label"] == "7.0h"
    assert status["watch_state_label"] == "Monitoring liquidity"
    assert status["actionability_gap"] == "POSITIVE_EV_NO_EXECUTABLE_BOOK"
    assert status["actionability_gap_label"] == "No executable book"
    assert "KXXRP-BOOK" in status["actionability_note"]
    assert "without placing orders" in status["actionability_note"]
    assert status["book_probe_available"] is True
    assert status["book_probe"]["ticker"] == "KXXRP-BOOK"
    assert status["book_probe"]["side"] == "Buy Yes"
    assert status["book_probe"]["expected_value_label"] == "1.7 cents"
    assert status["book_probe"]["liquidity_label"] == "None"
    assert status["book_probe"]["spread_label"] == "2.0 cents"
    assert status["book_probe"]["blockers_label"] == (
        "Low edge, No liquidity, Risk missing"
    )
    assert "Visible ask liquidity" in status["book_probe"]["needed_label"]
    assert (
        "does not create exchange liquidity"
        in status["book_probe"]["safety_label"]
    )


def test_crypto_freshness_watch_status_uses_freshness_window_when_running(
    tmp_path,
) -> None:
    now = utc_now()
    report_path = Path(tmp_path) / "phase3bc_r5_crypto_freshness_watch.json"
    status_path = Path(tmp_path) / "phase3bc_r5_status.json"
    report_path.write_text(
        json.dumps(
            {
                "generated_at": (now - timedelta(minutes=6)).isoformat(),
                "options": {"cadence_minutes": 5, "freshness_minutes": 10},
                "summary": {
                    "watch_state": "WAITING_FOR_POSITIVE_EV",
                    "best_current_expected_value_cents": "-0.5",
                    "best_ev_candidate_ticker": "KXBTC-SLOW-CYCLE",
                    "positive_ev_rows": 0,
                    "paper_ready_candidates": 0,
                    "primary_gap_after_refresh": "EV_NOT_POSITIVE",
                },
            }
        ),
        encoding="utf-8",
    )
    status_path.write_text(
        json.dumps(
            {
                "guard": {
                    "status": "RUNNING",
                    "running": True,
                    "pid": 5151,
                    "recommended_next_action": "Crypto watch is running inside its timeout budget.",
                }
            }
        ),
        encoding="utf-8",
    )

    status = crypto_freshness_watch_status(
        report_path=report_path,
        status_path=status_path,
        phase3ak_status_path=None,
        now=now,
    )

    assert status["status"] == "FRESH"
    assert status["freshness_window_minutes"] == 10
    assert status["runner_status"] == "RUNNING"
    assert status["runner_pid"] == 5151
    assert status["watch_state"] == "WAITING_FOR_POSITIVE_EV"


def test_crypto_freshness_watch_status_ignores_older_phase3ak_overlay(
    tmp_path,
) -> None:
    now = utc_now()
    report_path = Path(tmp_path) / "phase3bc_r5_crypto_freshness_watch.json"
    status_path = Path(tmp_path) / "phase3bc_r5_status.json"
    phase3ak_status_path = Path(tmp_path) / "crypto_watch_status.json"
    report_path.write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(),
                "options": {"cadence_minutes": 15},
                "summary": {
                    "watch_state": "WAITING_FOR_POSITIVE_EV",
                    "best_current_expected_value_cents": "-0.5",
                    "best_ev_candidate_ticker": "KXBTC-FRESH",
                    "positive_ev_rows": 0,
                    "paper_ready_candidates": 0,
                    "primary_gap_after_refresh": "EV_NOT_POSITIVE",
                },
            }
        ),
        encoding="utf-8",
    )
    status_path.write_text(
        json.dumps(
            {
                "guard": {
                    "status": "RUNNING",
                    "running": True,
                    "pid": 5151,
                    "elapsed_seconds": 3600,
                    "duration_budget_seconds": 28800,
                }
            }
        ),
        encoding="utf-8",
    )
    phase3ak_status_path.write_text(
        json.dumps(
            {
                "generated_at": (now - timedelta(days=2)).isoformat(),
                "primary_blocker": "WINDOW_SYNC_STALE",
                "watch_state": "RUNNING_CYCLE_OVERDUE",
                "runner_state": "RUNNING_CYCLE_OVERDUE",
                "runner_status": "RUNNING",
                "runner_running": True,
                "runner_pid": 21067,
                "next_action": "Old stale overlay should not replace fresh R5 status.",
                "window_summary": {"active_windows": 120, "stale_quote_count": 0},
                "readiness_funnel": {
                    "paper_ready_opportunities": 0,
                    "positive_raw_ev": 0,
                    "positive_executable_ev": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    status = crypto_freshness_watch_status(
        report_path=report_path,
        status_path=status_path,
        phase3ak_status_path=phase3ak_status_path,
        now=now,
    )

    assert status["status"] == "FRESH"
    assert status["watch_state"] == "WAITING_FOR_POSITIVE_EV"
    assert status["runner_status"] == "RUNNING"
    assert status["runner_pid"] == 5151
    assert status["best_ev_candidate_ticker"] == "KXBTC-FRESH"
    assert status["phase3ak_status_ignored"] == "stale_older_than_crypto_watch"


def test_phase3bc_r5_unattended_start_writes_guard_metadata(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3bc_r6, "_phase3bc_r5_running_pids", lambda: [])
    monkeypatch.setattr(
        phase3bc_r6,
        "_phase3bc_r5_running_pids_with_limit",
        lambda: ([], False),
    )
    observed: dict[str, object] = {}

    class FakePopen:
        pid = 5151

    def fake_popen(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return FakePopen()

    monkeypatch.setattr(phase3bc_r6.subprocess, "Popen", fake_popen)

    result = start_phase3bc_r5_unattended_watch(
        output_dir=Path("reports/phase3bc_r5"),
        cycles=2,
        interval_minutes=1,
        duration_hours=0.25,
        timeout_grace_seconds=30,
        market_limit=10,
        market_max_pages=1,
        crypto_market_scan_limit=25,
        crypto_link_limit=10,
        forecast_limit=10,
        opportunity_limit=10,
        phase3bc_limit=10,
        risk_preflight=False,
    )

    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))

    assert result.started is True
    assert result.pid == 5151
    assert result.pid_path.read_text(encoding="utf-8") == "5151"
    assert "phase3bc-r5-crypto-freshness-watch" in metadata["command"]
    assert "--no-risk-preflight" in observed["command"]
    assert "--diagnose-snapshots" in observed["command"]
    assert "--forecast-current-windows-only" in observed["command"]
    assert "--skip-opportunity-report" in observed["command"]
    assert "--phase3bc-r7-output-dir" in observed["command"]
    assert "--ranking-repair" in observed["command"]
    assert "--ranking-repair-limit" in observed["command"]
    assert "--near-money-only" in observed["command"]
    assert "--near-money-per-symbol-limit" in observed["command"]
    assert "--near-money-window-limit" in observed["command"]
    assert "--snapshot-fetch-concurrency" in observed["command"]
    assert metadata["near_money_only"] is True
    assert metadata["ranking_repair"] is True
    assert metadata["ranking_repair_limit"] == 500
    assert metadata["near_money_per_symbol_limit"] == 40
    assert metadata["near_money_window_limit"] == 20
    assert metadata["snapshot_fetch_concurrency"] == 2
    assert metadata["timeout_seconds"] == 930
    assert metadata["paper_only_safety"] == "PAPER_ONLY_NO_EXCHANGE_WRITES"
    assert metadata["order_submission"] is False


def test_phase3bc_r5_status_marks_unattended_overrun(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3bc_r6, "_pid_matches_phase3bc_r5_watch", lambda pid: pid == 5151)
    output_dir = Path("reports/phase3bc_r5")
    output_dir.mkdir(parents=True)
    (output_dir / "phase3bc_r5_unattended_job.pid").write_text("5151", encoding="utf-8")
    (output_dir / "phase3bc_r5_unattended_job.json").write_text(
        json.dumps(
            {
                "started_at": (utc_now() - timedelta(seconds=120)).isoformat(),
                "pid": 5151,
                "timeout_seconds": 30,
                "duration_budget_seconds": 10,
                "cadence_minutes": 15,
                "stdout_path": "reports/phase3bc_r5/phase3bc_r5_unattended_stdout.log",
                "stderr_path": "reports/phase3bc_r5/phase3bc_r5_unattended_stderr.log",
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "phase3bc_r5_crypto_freshness_watch.json").write_text(
        json.dumps(
            {
                "generated_at": utc_now().isoformat(),
                "summary": {"watch_state": "REFRESH_RANKINGS"},
                "options": {"cadence_minutes": 15},
            }
        ),
        encoding="utf-8",
    )

    artifacts = write_phase3bc_r5_status_report(output_dir=output_dir)
    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))

    assert payload["guard"]["status"] == "OVERRUNNING"
    assert payload["guard"]["should_stop"] is True
    assert "phase3bc-r5-unattended-guard --stop-overrun" in payload["recommended_next_action"]


def test_phase3bc_r5_status_respects_freshness_window_for_slow_cycle(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3bc_r6, "_pid_matches_phase3bc_r5_watch", lambda pid: pid == 5151)
    output_dir = Path("reports/phase3bc_r5")
    output_dir.mkdir(parents=True)
    (output_dir / "phase3bc_r5_unattended_job.pid").write_text("5151", encoding="utf-8")
    (output_dir / "phase3bc_r5_unattended_job.json").write_text(
        json.dumps(
            {
                "started_at": (utc_now() - timedelta(minutes=20)).isoformat(),
                "pid": 5151,
                "timeout_seconds": 3600,
                "duration_budget_seconds": 28800,
                "cadence_minutes": 5,
                "freshness_minutes": 10,
                "stdout_path": "reports/phase3bc_r5/phase3bc_r5_unattended_stdout.log",
                "stderr_path": "reports/phase3bc_r5/phase3bc_r5_unattended_stderr.log",
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "phase3bc_r5_crypto_freshness_watch.json").write_text(
        json.dumps(
            {
                "generated_at": (utc_now() - timedelta(minutes=6)).isoformat(),
                "summary": {
                    "watch_state": "WAITING_FOR_POSITIVE_EV",
                    "paper_ready_candidates": 0,
                    "positive_ev_rows": 0,
                    "true_ranking_gap_after_repair": 0,
                },
                "options": {"cadence_minutes": 5, "freshness_minutes": 10},
            }
        ),
        encoding="utf-8",
    )

    artifacts = write_phase3bc_r5_status_report(output_dir=output_dir)
    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))

    assert payload["guard"]["status"] == "RUNNING"
    assert payload["guard"]["stale_report"] is False
    assert payload["guard"]["cadence_minutes"] == 5
    assert payload["guard"]["freshness_minutes"] == 10
    assert payload["guard"]["freshness_window_minutes"] == 10
    assert payload["guard"]["recommended_next_action"] == (
        "Crypto watch is running inside its timeout budget."
    )


def test_phase3bc_r5_status_reads_bom_metadata(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3bc_r6, "_pid_matches_phase3bc_r5_watch", lambda pid: pid == 5151)
    output_dir = Path("reports/phase3bc_r5")
    output_dir.mkdir(parents=True)
    (output_dir / "phase3bc_r5_unattended_job.pid").write_text("5151", encoding="utf-8")
    (output_dir / "phase3bc_r5_unattended_job.json").write_text(
        "\ufeff"
        + json.dumps(
            {
                "started_at": (utc_now() - timedelta(seconds=15)).isoformat(),
                "pid": 5151,
                "timeout_seconds": 300,
                "stdout_path": "reports/phase3bc_r5/phase3bc_r5_unattended_stdout.log",
                "stderr_path": "reports/phase3bc_r5/phase3bc_r5_unattended_stderr.log",
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "phase3bc_r5_crypto_freshness_watch.json").write_text(
        json.dumps(
            {
                "generated_at": utc_now().isoformat(),
                "summary": {
                    "watch_state": "WAITING_FOR_POSITIVE_EV",
                    "positive_ev_rows": 0,
                    "slowest_stage": "phase3bc_r3_refresh",
                    "slowest_stage_seconds": "12.5",
                },
                "stage_duration_seconds": {"phase3bc_r3_refresh": 12.5},
                "options": {"cadence_minutes": 15},
            }
        ),
        encoding="utf-8",
    )

    artifacts = write_phase3bc_r5_status_report(output_dir=output_dir)
    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))

    assert payload["guard"]["pid"] == 5151
    assert payload["guard"]["status"] == "RUNNING"
    assert payload["latest_watch_state"] == "WAITING_FOR_POSITIVE_EV"
    assert payload["latest_stage_duration_seconds"]["phase3bc_r3_refresh"] == 12.5
    assert payload["latest_slowest_stage"] == {
        "stage": "phase3bc_r3_refresh",
        "duration_seconds": "12.5",
    }


def test_phase3bc_r5_status_scans_before_declaring_pid_stale(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3bc_r6, "_pid_matches_phase3bc_r5_watch", lambda _pid: False)
    monkeypatch.setattr(
        phase3bc_r6,
        "_phase3bc_r5_running_pids_with_limit",
        lambda: ([6262], False),
    )
    output_dir = Path("reports/phase3bc_r5")
    output_dir.mkdir(parents=True)
    (output_dir / "phase3bc_r5_unattended_job.pid").write_text("5151", encoding="utf-8")
    (output_dir / "phase3bc_r5_unattended_job.json").write_text(
        json.dumps(
            {
                "started_at": utc_now().isoformat(),
                "pid": 5151,
                "timeout_seconds": 300,
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "phase3bc_r5_crypto_freshness_watch.json").write_text(
        json.dumps(
            {
                "generated_at": utc_now().isoformat(),
                "summary": {"watch_state": "WAITING_FOR_POSITIVE_EV"},
                "options": {"cadence_minutes": 15},
            }
        ),
        encoding="utf-8",
    )

    artifacts = write_phase3bc_r5_status_report(output_dir=output_dir)
    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))

    assert payload["process"]["status"] == "RUNNING"
    assert payload["process"]["discovered_by"] == "process_scan_after_pid_miss"
    assert payload["process"]["phase3bc_r5_pids"] == [6262]
    assert payload["process"]["process_scan_skipped"] is False
    assert payload["guard"]["status"] == "RUNNING"
    assert payload["guard"]["running"] is True


def test_phase3bc_r5_status_ignores_stale_metadata_timeout_for_live_scanned_pid(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3bc_r6, "_pid_matches_phase3bc_r5_watch", lambda _pid: False)
    monkeypatch.setattr(
        phase3bc_r6,
        "_phase3bc_r5_running_pids_with_limit",
        lambda: ([6262], False),
    )
    output_dir = Path("reports/phase3bc_r5")
    output_dir.mkdir(parents=True)
    (output_dir / "phase3bc_r5_unattended_job.pid").write_text("5151", encoding="utf-8")
    (output_dir / "phase3bc_r5_unattended_job.json").write_text(
        json.dumps(
            {
                "started_at": (utc_now() - timedelta(hours=9)).isoformat(),
                "pid": 5151,
                "timeout_seconds": 30,
                "duration_budget_seconds": 30,
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "phase3bc_r5_crypto_freshness_watch.json").write_text(
        json.dumps(
            {
                "generated_at": utc_now().isoformat(),
                "summary": {"watch_state": "WAITING_FOR_EXECUTABLE_BOOK"},
                "options": {"cadence_minutes": 15},
            }
        ),
        encoding="utf-8",
    )

    artifacts = write_phase3bc_r5_status_report(output_dir=output_dir)
    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))

    assert payload["process"]["discovered_by"] == "process_scan_after_pid_miss"
    assert payload["guard"]["pid"] == 6262
    assert payload["guard"]["metadata_pid"] == 5151
    assert payload["guard"]["metadata_pid_stale"] is True
    assert payload["guard"]["status"] == "RUNNING"
    assert payload["guard"]["should_stop"] is False


def test_phase3bc_r5_status_reports_stale_pid_after_empty_process_scan(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3bc_r6, "_pid_matches_phase3bc_r5_watch", lambda _pid: False)
    monkeypatch.setattr(
        phase3bc_r6,
        "_phase3bc_r5_running_pids_with_limit",
        lambda: ([], False),
    )
    output_dir = Path("reports/phase3bc_r5")
    output_dir.mkdir(parents=True)
    (output_dir / "phase3bc_r5_unattended_job.pid").write_text("5151", encoding="utf-8")
    (output_dir / "phase3bc_r5_unattended_job.json").write_text(
        json.dumps(
            {
                "started_at": (utc_now() - timedelta(hours=9)).isoformat(),
                "pid": 5151,
                "timeout_seconds": 30,
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "phase3bc_r5_crypto_freshness_watch.json").write_text(
        json.dumps(
            {
                "generated_at": utc_now().isoformat(),
                "summary": {"watch_state": "WAITING_FOR_POSITIVE_EV"},
                "options": {"cadence_minutes": 15},
            }
        ),
        encoding="utf-8",
    )

    artifacts = write_phase3bc_r5_status_report(output_dir=output_dir)
    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))

    assert payload["process"]["status"] == "STOPPED"
    assert payload["process"]["discovered_by"] == "pid_file_stale"
    assert payload["process"]["process_scan_skipped"] is False
    assert payload["guard"]["status"] == "STOPPED_WITH_STALE_PID"
    assert payload["guard"]["should_stop"] is False
    assert "stale PID metadata can be overwritten" in payload["recommended_next_action"]


def test_phase3bc_r5_status_uses_fresh_gh2_scheduled_owner_over_stale_pid(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3bc_r6, "_pid_matches_phase3bc_r5_watch", lambda _pid: False)
    monkeypatch.setattr(
        phase3bc_r6,
        "_phase3bc_r5_running_pids_with_limit",
        lambda: ([], False),
    )
    output_dir = Path("reports/phase3bc_r5")
    output_dir.mkdir(parents=True)
    (output_dir / "phase3bc_r5_unattended_job.pid").write_text("5151", encoding="utf-8")
    (output_dir / "phase3bc_r5_unattended_job.json").write_text(
        json.dumps(
            {
                "started_at": (utc_now() - timedelta(hours=9)).isoformat(),
                "pid": 5151,
                "timeout_seconds": 30,
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "phase3bc_r5_crypto_freshness_watch.json").write_text(
        json.dumps(
            {
                "generated_at": utc_now().isoformat(),
                "summary": {"watch_state": "WAITING_FOR_POSITIVE_EV"},
                "options": {"cadence_minutes": 15},
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "phase3bc_r5_owner.json").write_text(
        json.dumps(
            {
                "owner": "GH-2_SINGLE_WRITER_DECISION_REFRESH",
                "status": "SCHEDULED_OWNER_HEALTHY",
                "generated_at": utc_now().isoformat(),
                "cadence_minutes": 15,
                "paper_order_creation_enabled": False,
                "live_execution_enabled": False,
            }
        ),
        encoding="utf-8",
    )

    artifacts = write_phase3bc_r5_status_report(output_dir=output_dir)
    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))

    assert payload["ownership"]["active"] is True
    assert payload["process"]["status"] == "SCHEDULED_OWNER_IDLE"
    assert payload["process"]["discovered_by"] == "gh2_owner_artifact"
    assert payload["guard"]["status"] == "SCHEDULED_OWNER_HEALTHY"
    assert payload["guard"]["pid"] is None
    assert payload["guard"]["metadata_superseded_by_scheduled_owner"] is True
    assert payload["guard"]["should_stop"] is False
    assert "do not start a duplicate R5 watcher" in payload["recommended_next_action"]


def test_crypto_gate_diagnostics_explain_missing_snapshot_recovery() -> None:
    payload = {
        "blocked_active_pure_examples": [
            {
                "ticker": "KXXRP-26JUL2417-B1.4699500",
                "clean_title": "Ripple price",
                "blocked_reason": "BLOCKED_MISSING_ACTIVE_SNAPSHOT",
                "readiness_status": "BLOCKED_MISSING_ACTIVE_SNAPSHOT",
                "latest_snapshot_at": None,
                "best_price": None,
                "expected_value_cents": None,
            }
        ]
    }

    rows = ui_service._crypto_gate_failure_examples(payload)
    note = ui_service._crypto_actionability_note(
        {"snapshot_missing_rows": 13, "positive_ev_rows": 0},
        "SNAPSHOT_MISSING",
    )

    assert len(rows) == 1
    assert rows[0]["book_label"] == "Snapshot missing"
    assert "Snapshot Missing" in rows[0]["failed_gate_label"]
    assert "GH-1 snapshot recovery" in rows[0]["next_action"]
    assert "13 active crypto rows have no snapshot" in note
    assert "no order action is allowed" in note


def test_phase3bc_r5_pid_exists_treats_permission_error_as_live(monkeypatch) -> None:
    def deny_signal(_pid: int, _signal: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(phase3bc_r6.os, "kill", deny_signal)
    monkeypatch.setattr(phase3bc_r6, "_posix_pid_is_zombie", lambda _pid: False)

    assert phase3bc_r6._pid_exists(5151) is True


def test_phase3bc_r5_pid_exists_rejects_missing_process(monkeypatch) -> None:
    def missing_process(_pid: int, _signal: int) -> None:
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(phase3bc_r6.os, "kill", missing_process)

    assert phase3bc_r6._pid_exists(5151) is False


def test_phase3bc_r5_unattended_guard_can_stop_overrun(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3bc_r6, "_pid_matches_phase3bc_r5_watch", lambda pid: pid == 5151)
    stopped: dict[str, int] = {}

    def fake_terminate(pid, *, grace_seconds):
        stopped["pid"] = pid
        stopped["grace_seconds"] = grace_seconds
        return {"status": "STOPPED_AFTER_TERM", "pid": pid}

    monkeypatch.setattr(phase3bc_r6, "_terminate_pid", fake_terminate)
    output_dir = Path("reports/phase3bc_r5")
    output_dir.mkdir(parents=True)
    (output_dir / "phase3bc_r5_unattended_job.pid").write_text("5151", encoding="utf-8")
    (output_dir / "phase3bc_r5_unattended_job.json").write_text(
        json.dumps(
            {
                "started_at": (utc_now() - timedelta(seconds=120)).isoformat(),
                "pid": 5151,
                "timeout_seconds": 30,
            }
        ),
        encoding="utf-8",
    )

    artifacts = write_phase3bc_r5_unattended_guard_report(
        output_dir=output_dir,
        stop_overrun=True,
        terminate_grace_seconds=3,
    )
    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))

    assert stopped == {"pid": 5151, "grace_seconds": 3}
    assert payload["action"]["termination_result"]["status"] == "STOPPED_AFTER_TERM"


def test_phase3bc_r5_unattended_cli_and_scheduler_profile() -> None:
    runner = CliRunner()

    status = runner.invoke(app, ["phase3bc-r5-status", "--help"])
    start = runner.invoke(app, ["phase3bc-r5-unattended-start", "--help"])
    guard = runner.invoke(app, ["phase3bc-r5-unattended-guard", "--help"])
    plan = scheduler_plan("crypto-watch")

    assert status.exit_code == 0
    assert start.exit_code == 0
    assert guard.exit_code == 0
    assert "phase3bc-r5-status" in status.output
    assert "phase3bc-r5-unattended-start" in start.output
    assert "phase3bc-r5-unattended-guard" in guard.output
    assert plan[0].command.startswith("kalshi-bot phase3bc-r5-unattended-start")
    assert "--interval-minutes 15" in plan[0].command
    assert "--diagnose-snapshots" in plan[0].command
    assert "--forecast-current-windows-only" in plan[0].command
    assert "--skip-opportunity-report" in plan[0].command
    assert "--ranking-repair" in start.output
    assert "--ranking-repair" in plan[0].command
    assert "--ranking-repair-limit 500" in plan[0].command
    assert "--near-money-only" in plan[0].command
    assert "--market-limit 150" in plan[0].command
    assert "--market-max-pages 1" in plan[0].command
    assert "--near-money-per-symbol-limit 40" in plan[0].command
    assert "--near-money-window-limit 20" in plan[0].command
    assert "--snapshot-fetch-concurrency 2" in plan[0].command
    assert "--crypto-market-scan-limit 2500" in plan[0].command
    assert "--crypto-link-limit 500" in plan[0].command


def test_phase3bc_r5_cli_disposes_engine_between_cycles(monkeypatch, tmp_path) -> None:
    class FakeEngine:
        dispose_calls = 0

        def dispose(self) -> None:
            self.dispose_calls += 1

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def commit(self) -> None:
            return None

    fake_engine = FakeEngine()

    monkeypatch.setattr(cli_module, "init_db", lambda: fake_engine)
    monkeypatch.setattr(cli_module, "get_settings", lambda: SimpleNamespace(log_level="INFO"))
    monkeypatch.setattr(cli_module, "get_session_factory", lambda engine: FakeSession)
    monkeypatch.setattr(
        cli_module,
        "write_phase3bc_r5_crypto_freshness_watch_report",
        lambda *args, **kwargs: SimpleNamespace(
            json_path=tmp_path / "watch.json",
            markdown_path=tmp_path / "watch.md",
            preflight_rows_path=tmp_path / "rows.json",
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "phase3bc-r5-crypto-freshness-watch",
            "--cycles",
            "2",
            "--interval-minutes",
            "0",
        ],
    )

    assert result.exit_code == 0
    assert fake_engine.dispose_calls == 2


def test_phase3bc_r5_fast_status_path_writes_report(monkeypatch, capsys) -> None:
    called: dict[str, Path] = {}

    def fake_write_status_report(*, output_dir: Path):
        called["output_dir"] = output_dir
        return SimpleNamespace(
            json_path=output_dir / "phase3bc_r5_status.json",
            markdown_path=output_dir / "phase3bc_r5_status.md",
        )

    monkeypatch.setattr(phase3bc_r6, "write_phase3bc_r5_status_report", fake_write_status_report)

    exit_code = _phase3bc_r5_fast_path_command(
        ["phase3bc-r5-status", "--output-dir", "reports/custom_r5"]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert called["output_dir"] == Path("reports/custom_r5")
    assert "Phase 3BC-R5 crypto freshness watch status" in output
    assert "Order submission/cancel/replace: blocked" in output


def test_phase3bc_r5_fast_path_ignores_help() -> None:
    assert _phase3bc_r5_fast_path_command(["phase3bc-r5-status", "--help"]) is None


def _row(
    ticker: str,
    *,
    expected_value: str,
    latest_ranking_at: str,
    latest_snapshot_at: str | None = None,
    latest_forecast_at: str | None = None,
    structure_status: str = "PURE_CRYPTO",
    readiness_status: str = "PAPER_READY_CANDIDATE",
    final_action: str = "PAPER_READY_CANDIDATE",
    liquidity_score: str = "80",
) -> dict[str, object]:
    return {
        "ticker": ticker,
        "clean_title": "Bitcoin price range",
        "event_ticker": f"{ticker}-EVENT",
        "series_ticker": "KXBTC",
        "active_market": True,
        "structure_status": structure_status,
        "readiness_status": readiness_status,
        "final_action": final_action,
        "best_side": BUY_YES,
        "best_price": "0.40",
        "model_probability": "0.75",
        "expected_value": expected_value,
        "estimated_edge": "0.35",
        "opportunity_score": "85",
        "liquidity_score": liquidity_score,
        "spread": "0.02",
        "latest_snapshot_at": latest_snapshot_at or latest_ranking_at,
        "latest_forecast_at": latest_forecast_at or latest_ranking_at,
        "latest_ranking_at": latest_ranking_at,
    }
