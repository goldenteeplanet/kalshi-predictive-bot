import json
from datetime import timedelta
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor import phase3ad
from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market, upsert_settlement
from kalshi_predictor.data.schema import MarketRanking, PaperOrder
from kalshi_predictor.paper.models import ORDER_FILLED
from kalshi_predictor.phase3ad import build_phase_orchestrator, write_phase_orchestrator_report
from kalshi_predictor.utils.time import utc_now


def test_phase3ad_builds_paper_only_self_improvement_roadmap(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3ad, "build_market_coverage_doctor", _coverage_stub)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        session.add(
            _ranking(
                "KXFAST-ROADMAP",
                title="Bitcoin above target today",
                minutes="90",
                score="60",
                edge="0.04",
            )
        )

        payload = build_phase_orchestrator(session, settings=Settings(), scan_limit=50)

    assert payload["phase"] == "3AD"
    assert payload["mode"] == "PAPER_ONLY_ROADMAP_ENGINE"
    assert payload["automation_policy"]["executes_generated_code"] is False
    assert payload["automation_policy"]["enables_live_trading"] is False
    assert payload["automation_policy"]["enables_demo_execution"] is False
    assert "rl-evaluate" in payload["expected_commands"]
    assert "feature-discovery-run" in payload["expected_commands"]
    assert "phase3ag-sports-link-repair-pass" in payload["expected_commands"]
    assert "phase3ah-round-placeholder-resolution" in payload["expected_commands"]
    assert "phase3ah-sports-placeholder-watch" in payload["expected_commands"]
    assert "phase3ah-roster-participant-verification" in payload["expected_commands"]
    assert "phase3ay-health-refresh" in payload["expected_commands"]
    assert "phase3ay-status" in payload["expected_commands"]
    assert "phase3az-gap-analysis" in payload["expected_commands"]
    assert "phase3bb-r2-general-source-intake" in payload["expected_commands"]
    assert "phase3bb-r2-general-source-evidence" in payload["expected_commands"]
    assert "phase3bb-r2-general-source-availability" in payload["expected_commands"]
    assert "phase3bb-r3-general-reclassification" in payload["expected_commands"]
    assert "phase3aa-r5-closed-market-outcome-capture" in payload["expected_commands"]
    assert payload["evidence"]["self_improvement"]["policy"]["can_execute_generated_code"] is False
    rl_status = payload["evidence"]["self_improvement"]["engines"]["reinforcement_learning"][
        "status"
    ]
    assert rl_status in {
        "ACTIVE",
        "NEEDS_FIRST_RUN",
    }
    candidate_ids = {row["id"] for row in payload["improvement_candidates"]}
    assert {"feature_discovery", "rl_policy_replay", "self_evaluation_journal"} <= candidate_ids
    assert "Reinforcement learning must remain offline/shadow" in payload["implementation_prompt"]
    assert "must not auto-edit or auto-deploy code" in payload["implementation_prompt"]


def test_phase3ad_prioritizes_exact_settlements_as_reward_signal(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3ad, "build_market_coverage_doctor", _coverage_stub)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        order = _paper_order("KX3AD-EXACT")
        session.add(order)
        upsert_market(
            session,
            {
                "ticker": order.ticker,
                "status": "settled",
                "title": "Phase 3AD exact reward market",
            },
        )
        upsert_settlement(
            session,
            {
                "ticker": order.ticker,
                "result": "yes",
                "settlement_ts": "2026-06-24T12:00:00Z",
            },
        )

        payload = build_phase_orchestrator(session, settings=Settings(), scan_limit=50)

    assert payload["evidence"]["settlement"]["summary"]["eligible_exact_settlements"] == 1
    assert payload["next_phase"]["title"] == "Outcome Feedback Integrator"
    assert payload["improvement_candidates"][0]["id"] == "settlement_outcome_feedback"
    assert payload["improvement_candidates"][0]["priority"] == 90


def test_phase3ad_uses_r5_closed_outcome_capture_for_due_settlement_guidance(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3ad, "build_market_coverage_doctor", _coverage_stub)
    _write_json(
        Path("reports/phase3aa_r5/phase3aa_r5_closed_market_outcome_capture.json"),
        {
            "summary": {
                "closed_without_outcome_rows": 42,
                "usable_outcome_candidate_rows": 0,
            }
        },
    )
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        order = _paper_order("KX3AD-DUE")
        session.add(order)
        upsert_market(
            session,
            {
                "ticker": order.ticker,
                "status": "active",
                "title": "Phase 3AD due market",
                "close_time": utc_now() - timedelta(hours=2),
            },
        )

        payload = build_phase_orchestrator(session, settings=Settings(), scan_limit=50)

    due_bottleneck = next(
        row for row in payload["bottlenecks"] if row["code"] == "DUE_OR_OVERDUE_SETTLEMENTS"
    )
    harvest_candidate = next(
        row for row in payload["improvement_candidates"] if row["id"] == "exact_settlement_harvest"
    )
    assert "R5 found closed exact-market payloads" in due_bottleneck["next_action"]
    assert "R5 found closed exact-market payloads" in harvest_candidate["blocked_by"]
    assert harvest_candidate["next_command"] == (
        "kalshi-bot phase3ay-health-refresh --cycles 1 --interval-seconds 0"
    )


def test_phase3ad_uses_phase3az_implementation_queue_for_next_phase(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3ad, "build_market_coverage_doctor", _coverage_stub)
    _write_json(
        Path("reports/phase3az/phase3az_gap_analysis.json"),
        {
            "summary": {"implementation_needed_count": 1},
            "implementation_queue": [
                {
                    "phase": "3BB-R2",
                    "gap_id": "general_domain_taxonomy_actionable",
                    "priority": "MEDIUM",
                    "objective": "General markets need taxonomy and candidate-routing work.",
                    "starter_command": (
                        "kalshi-bot phase3bb-r2-general-candidate-routing "
                        "--output-dir reports/phase3bb_r2"
                    ),
                }
            ],
            "recommended_next_action": (
                "Implement 3BB-R2 for general_domain_taxonomy_actionable next."
            ),
        },
    )
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        payload = build_phase_orchestrator(session, settings=Settings(), scan_limit=50)

    assert payload["next_phase"]["phase"] == "3BB-R2"
    assert payload["next_phase"]["title"] == (
        "General markets need taxonomy and candidate-routing work."
    )
    assert payload["improvement_candidates"][0]["id"] == "phase3az_next_gap"
    assert "PHASE3AZ_GENERAL_DOMAIN_TAXONOMY_ACTIONABLE" in {
        row["code"] for row in payload["bottlenecks"]
    }
    assert "phase3bb-r2-general-candidate-routing" in payload["implementation_prompt"]


def test_phase3ad_uses_cached_market_coverage_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    def fail_full_scan(*_args, **_kwargs):
        raise AssertionError("full diagnostics should not run by default")

    monkeypatch.setattr(phase3ad, "build_market_coverage_doctor", fail_full_scan)
    monkeypatch.setattr(phase3ad, "build_learning_diagnostics", fail_full_scan)
    monkeypatch.setattr(phase3ad, "build_sports_provenance_snapshot", fail_full_scan)
    _write_json(
        Path("reports/market_coverage/market_coverage_doctor.json"),
        {
            "stage_counts": {
                "parse_failures": 0,
                "parsed_markets": 10,
                "parsed_legs": 20,
            },
            "recommendations": [{"summary": "cached report is enough"}],
            "coverage_rows": [
                {
                    "scope_key": "crypto",
                    "health": "HEALTHY",
                    "parsed_markets": 10,
                    "external_linked_markets": 10,
                    "partial_markets": 0,
                    "coverage": 1.0,
                    "next_action": {"summary": "No action required"},
                }
            ],
        },
    )
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        payload = build_phase_orchestrator(session, settings=Settings(), scan_limit=25)

    coverage = payload["evidence"]["market_coverage"]
    assert coverage["source"].startswith("cached_report:")
    assert coverage["parsed_markets"] == 10
    assert payload["bounded_runtime"]["market_coverage_source"].startswith("cached_report:")
    assert payload["bounded_runtime"]["full_market_coverage_scan_default"] is False


def test_phase3ad_falls_back_to_bounded_coverage_without_cached_report(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    def fail_full_scan(*_args, **_kwargs):
        raise AssertionError("full diagnostics should not run by default")

    monkeypatch.setattr(phase3ad, "build_market_coverage_doctor", fail_full_scan)
    monkeypatch.setattr(phase3ad, "build_learning_diagnostics", fail_full_scan)
    monkeypatch.setattr(phase3ad, "build_sports_provenance_snapshot", fail_full_scan)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        payload = build_phase_orchestrator(session, settings=Settings(), scan_limit=10)

    assert payload["evidence"]["market_coverage"]["source"] == "bounded_sql_aggregate"
    assert payload["evidence"]["learning_diagnostics"]["source"] == "bounded_sql_aggregate"
    assert payload["evidence"]["sports_provenance"]["source"] == "bounded_sql_aggregate"
    assert payload["bounded_runtime"]["scan_limit"] == 10
    assert payload["bounded_runtime"]["market_coverage_refresh_requested"] is False


def test_phase3ad_writes_markdown_json_and_next_prompt(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3ad, "build_market_coverage_doctor", _coverage_stub)
    session_factory = _session_factory(tmp_path)
    output = tmp_path / "phase_orchestrator.md"
    json_output = tmp_path / "phase_orchestrator.json"
    next_prompt = tmp_path / "next_phase.md"
    with session_factory() as session:
        artifacts = write_phase_orchestrator_report(
            session,
            output_path=output,
            json_path=json_output,
            next_prompt_path=next_prompt,
            settings=Settings(),
            scan_limit=50,
        )

    assert artifacts.output_path == output
    assert artifacts.json_path == json_output
    assert artifacts.next_prompt_path == next_prompt
    markdown = output.read_text(encoding="utf-8")
    prompt = next_prompt.read_text(encoding="utf-8")
    assert "AI Build Candidates" in markdown
    assert "Automation Guardrails" in markdown
    assert "Build: Phase 3AE" in prompt
    assert "phase3ag-sports-link-repair-pass" in prompt
    assert "phase3ah-round-placeholder-resolution" in prompt
    assert "phase3ah-sports-placeholder-watch" in prompt
    assert "phase3ah-roster-participant-verification" in prompt
    assert "phase3ay-health-refresh" in prompt
    assert "phase3az-gap-analysis" in prompt
    assert "Do NOT add live trading" in prompt


def test_phase3ad_cli_help() -> None:
    result = CliRunner().invoke(app, ["phase-orchestrator", "--help"])

    assert result.exit_code == 0
    assert "Usage" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ad.db'}")
    return get_session_factory(engine)


def _coverage_stub(_session, *, settings=None, parse_first=False):
    return {
        "stage_counts": {
            "parse_failures": 0,
            "parsed_markets": 0,
            "parsed_legs": 0,
        },
        "recommendations": [],
        "coverage_rows": [
            {
                "scope_key": "general",
                "health": "HEALTHY",
                "parsed_markets": 0,
                "external_linked_markets": 0,
                "partial_markets": 0,
                "coverage": "0.0%",
                "next_action": {"summary": "No coverage action needed for this test."},
            }
        ],
    }


def _paper_order(ticker: str) -> PaperOrder:
    return PaperOrder(
        ticker=ticker,
        forecast_id=None,
        created_at=utc_now(),
        model_name="ensemble_v2",
        side="BUY_YES",
        probability="0.55",
        market_price="0.50",
        limit_price="0.50",
        edge="0.05",
        quantity=1,
        status=ORDER_FILLED,
        reason="phase 3ad test",
        raw_decision_json="{}",
    )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _ranking(
    ticker: str,
    *,
    title: str,
    minutes: str,
    score: str,
    edge: str,
) -> MarketRanking:
    return MarketRanking(
        ticker=ticker,
        ranked_at=utc_now(),
        title=title,
        status="open",
        series_ticker=ticker.split("-", 1)[0],
        event_ticker=f"{ticker}-EVENT",
        volume="100",
        open_interest="100",
        liquidity="100",
        spread="0.02",
        midpoint="0.50",
        time_to_close_minutes=minutes,
        forecast_model="ensemble_v2",
        forecast_probability="0.55",
        best_side="YES",
        best_price="0.50",
        estimated_edge=edge,
        liquidity_score="60",
        spread_score="80",
        time_score="80",
        model_confidence_score="50",
        opportunity_score=score,
        reason="test ranking",
        raw_json="{}",
    )
