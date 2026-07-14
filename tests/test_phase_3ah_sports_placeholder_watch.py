from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.phase3ah_placeholder_watch import (
    SETTLEMENT_HARVEST_COMMAND,
    SETTLEMENT_REALIZE_COMMAND,
    build_phase3ah_sports_placeholder_watch,
    write_phase3ah_sports_placeholder_watch_report,
)


def test_phase3ah_placeholder_watch_holds_still_placeholder_rows(tmp_path) -> None:
    placeholder_path = _write_placeholder_report(tmp_path, safe=False)
    sports_path = _write_sports_evidence(tmp_path)
    settlement_path = _write_settlement_harvest(tmp_path, exact_settlements=0)

    payload = build_phase3ah_sports_placeholder_watch(
        placeholder_report_path=placeholder_path,
        sports_evidence_path=sports_path,
        settlement_harvest_path=settlement_path,
        **_missing_freshness_paths(tmp_path),
    )

    assert payload["summary"]["still_placeholder_rows"] == 1
    assert payload["summary"]["auto_upgrades_created"] == 0
    assert payload["summary"]["phase3ae_gate_status"] == "HOLD_PLACEHOLDER_UPGRADES"
    assert payload["phase3ae_gate"]["status"] == "HOLD_PLACEHOLDER_UPGRADES"
    assert payload["phase3ae_gate"]["phase3ae_can_create_links_from_placeholders"] is False
    assert payload["settlement_watch"]["harvest_command"] == SETTLEMENT_HARVEST_COMMAND
    assert SETTLEMENT_HARVEST_COMMAND in payload["next_commands"]
    assert "Settlement work remains separate" in payload["recommended_next_action"]


def test_phase3ah_placeholder_watch_surfaces_safe_rows_for_phase3ae(tmp_path) -> None:
    placeholder_path = _write_placeholder_report(tmp_path, safe=True)
    sports_path = _write_sports_evidence(tmp_path)
    settlement_path = _write_settlement_harvest(tmp_path, exact_settlements=2)

    payload = build_phase3ah_sports_placeholder_watch(
        placeholder_report_path=placeholder_path,
        sports_evidence_path=sports_path,
        settlement_harvest_path=settlement_path,
        **_missing_freshness_paths(tmp_path),
    )

    assert payload["phase3ae_gate"]["status"] == "READY_FOR_PHASE3AE_SAFE_ROWS"
    assert payload["phase3ae_gate"]["phase3ae_can_evaluate_safe_rows"] is True
    assert payload["settlement_watch"]["status"] == "EXACT_SETTLEMENTS_READY_TO_REALIZE"
    assert payload["settlement_watch"]["next_action"] == SETTLEMENT_REALIZE_COMMAND
    assert any(
        "--candidate-game-key SOCCER:espn:fifa.world:760510" in command
        for command in payload["next_commands"]
    )
    assert payload["recommended_next_action"].startswith("Realize the newly harvested")


def test_phase3ah_placeholder_watch_suppresses_stale_realize_prompt_after_r3_clear(
    tmp_path,
) -> None:
    placeholder_path = _write_placeholder_report(tmp_path, safe=False)
    sports_path = _write_sports_evidence(tmp_path)
    settlement_path = _write_settlement_harvest(tmp_path, exact_settlements=20)
    phase3aa_path = _write_phase3aa_report(tmp_path)
    r3_path = _write_phase3aa_r3_report(tmp_path)
    paper_path = _write_paper_settlement_report(tmp_path)

    payload = build_phase3ah_sports_placeholder_watch(
        placeholder_report_path=placeholder_path,
        sports_evidence_path=sports_path,
        settlement_harvest_path=settlement_path,
        phase3aa_report_path=phase3aa_path,
        phase3aa_r3_report_path=r3_path,
        paper_settlement_path=paper_path,
    )

    assert payload["settlement_freshness"]["realization_cleared_by_fresher_reports"] is True
    assert payload["settlement_watch"]["stale_realize_prompt_suppressed"] is True
    assert payload["settlement_watch"]["status"] == "NO_EXACT_SETTLEMENTS_AVAILABLE"
    assert not payload["recommended_next_action"].startswith("Realize")


def test_phase3ah_placeholder_watch_writer_emits_artifacts(tmp_path) -> None:
    placeholder_path = _write_placeholder_report(tmp_path, safe=False)
    sports_path = _write_sports_evidence(tmp_path)
    settlement_path = _write_settlement_harvest(tmp_path, exact_settlements=0)
    output_dir = Path(tmp_path) / "phase3ah"

    artifacts = write_phase3ah_sports_placeholder_watch_report(
        output_dir=output_dir,
        placeholder_report_path=placeholder_path,
        sports_evidence_path=sports_path,
        settlement_harvest_path=settlement_path,
        **_missing_freshness_paths(tmp_path),
    )

    assert artifacts.json_path.exists()
    assert artifacts.markdown_path.exists()
    assert "Sports Placeholder Watch" in artifacts.markdown_path.read_text(encoding="utf-8")


def test_phase3ah_placeholder_watch_cli_help() -> None:
    result = CliRunner().invoke(app, ["phase3ah-sports-placeholder-watch", "--help"])

    assert result.exit_code == 0
    assert "phase3ah-sports-placeholder-watch" in result.output


def _write_placeholder_report(tmp_path, *, safe: bool) -> Path:
    path = Path(tmp_path) / "phase3ah_round_placeholder_resolution_report.json"
    status = "RESOLVED_FROM_SOURCE" if safe else "SOURCE_STILL_PLACEHOLDER"
    row = {
        "league": "SOCCER",
        "game_key": "SOCCER:espn:fifa.world:760510",
        "home_placeholder_team_key": "SOCCER:rd16-w1",
        "away_placeholder_team_key": "SOCCER:rd16-w2",
        "source_status": status,
        "safe_to_apply": safe,
        "blocks_phase3ae_upgrade": not safe,
        "source_home_team_name": "Brazil" if safe else "Round of 16 1 Winner",
        "source_away_team_name": "Canada" if safe else "Round of 16 2 Winner",
        "resolved_home_team_key": "SOCCER:bra" if safe else "",
        "resolved_away_team_key": "SOCCER:can" if safe else "",
        "example_tickers": ["KXSOCCER-PLACEHOLDER"],
        "next_action": "Rerun Phase 3AE." if safe else "Wait for bracket advancement.",
    }
    path.write_text(
        json.dumps(
            {
                "summary": {
                    "rows_reviewed": 1,
                    "safe_to_apply_rows": 1 if safe else 0,
                    "still_placeholder_rows": 0 if safe else 1,
                    "fetch_error_rows": 0,
                    "unsupported_rows": 0,
                    "phase3ah_auto_upgrades_created": 0,
                },
                "rows": [row],
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_sports_evidence(tmp_path) -> Path:
    path = Path(tmp_path) / "phase3ah_sports_evidence_backfill.json"
    path.write_text(
        json.dumps(
            {
                "summary": {
                    "repair_rows_reviewed": 12,
                    "round_placeholder_resolution_rows": 1,
                    "phase3ae_ready_rows": 0,
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_settlement_harvest(tmp_path, *, exact_settlements: int) -> Path:
    path = Path(tmp_path) / "phase3aa_r2_exact_settlement_harvest.json"
    path.write_text(
        json.dumps(
            {
                "summary": {
                    "exact_settlements_written": exact_settlements,
                    "eligible_exact_settlements_after": exact_settlements,
                    "fetch_errors": 0,
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_phase3aa_report(tmp_path) -> Path:
    path = Path(tmp_path) / "phase3aa_outcome_realizer.json"
    path.write_text(
        json.dumps({"generated_at": "2026-06-28T01:38:52+00:00", "eligible_after_realize": 0}),
        encoding="utf-8",
    )
    return path


def _write_phase3aa_r3_report(tmp_path) -> Path:
    path = Path(tmp_path) / "phase3aa_r3_residual_settlement_audit.json"
    path.write_text(
        json.dumps({"summary": {"residue_cleared": True, "residual_rows": 0}}),
        encoding="utf-8",
    )
    return path


def _write_paper_settlement_report(tmp_path) -> Path:
    path = Path(tmp_path) / "paper_settlement_reconciliation.json"
    path.write_text(
        json.dumps({"summary": {"eligible_to_settle_now": 0}}),
        encoding="utf-8",
    )
    return path


def _missing_freshness_paths(tmp_path) -> dict[str, Path]:
    base = Path(tmp_path) / "missing_freshness"
    return {
        "phase3aa_report_path": base / "phase3aa_outcome_realizer.json",
        "phase3aa_r3_report_path": base / "phase3aa_r3_residual_settlement_audit.json",
        "paper_settlement_path": base / "paper_settlement_reconciliation.json",
    }
