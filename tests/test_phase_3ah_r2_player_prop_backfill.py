import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.phase3ah_r2 import (
    CURATED_ROSTER_EVIDENCE,
    run_phase3ah_r2_backfill,
    write_phase3ah_r2_backfill_report,
)


def test_phase3ah_r2_applies_curated_roster_rows_and_schedule_backfill(tmp_path) -> None:
    template_path = Path(tmp_path) / "phase3ah_roster_review_template.json"
    diagnostics_path = Path(tmp_path) / "phase3ae_roster_candidate_diagnostics.json"
    roster_output_dir = Path(tmp_path) / "phase3ah_sports"
    output_dir = Path(tmp_path) / "phase3ah_r2"
    schedule_output_dir = Path(tmp_path) / "schedules"
    template_path.write_text(
        json.dumps(
            [
                {
                    "league": "SOCCER",
                    "player_name": "Breel Embolo",
                    "count": 34,
                    "example_player_prop_tickers": ["OLD"],
                    "example_tickers": ["OLD"],
                    "review_status": "UNVERIFIED",
                    "verified_entity_type": "PLAYER",
                    "canonical_player_id": "",
                    "current_team_key": "",
                    "current_team_name": "",
                    "roster_source_url": "",
                    "valid_from": "",
                    "valid_to": "",
                    "safe_to_apply": False,
                }
            ]
        ),
        encoding="utf-8",
    )
    diagnostics_path.write_text(
        json.dumps(
            {
                "top_missing_roster_players": [
                    {
                        "player_name": "Breel Embolo",
                        "league": "SOCCER",
                        "count": 35,
                        "example_tickers": ["NEW"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def fake_schedule_runner(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return {
            "league_results": [{"league": "SOCCER", "games": 2}],
            "schedule_paths": [str(schedule_output_dir / "sports_verified_soccer.json")],
            "summary": {"games_inserted": 2},
        }

    artifacts = write_phase3ah_r2_backfill_report(
        None,
        output_dir=output_dir,
        roster_template_path=template_path,
        roster_output_dir=roster_output_dir,
        diagnostics_path=diagnostics_path,
        schedule_output_dir=schedule_output_dir,
        ingest_schedules=False,
        schedule_runner=fake_schedule_runner,
    )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    rows = json.loads(template_path.read_text(encoding="utf-8"))
    breel = next(row for row in rows if row["player_name"] == "Breel Embolo")
    assert payload["summary"]["curated_roster_rows_applied"] == len(CURATED_ROSTER_EVIDENCE)
    assert payload["summary"]["auto_upgrades_created"] == 0
    assert breel["review_status"] == "VERIFIED"
    assert breel["safe_to_apply"] is True
    assert breel["current_team_key"] == "SOCCER:sui"
    assert breel["example_player_prop_tickers"] == ["OLD", "NEW"]
    assert calls[0]["kwargs"]["leagues"] == ("SOCCER",)
    assert calls[0]["kwargs"]["start_date"] == "2026-07-07"
    assert calls[0]["kwargs"]["days_ahead"] == 4
    assert calls[0]["kwargs"]["ingest"] is False
    assert artifacts.verified_roster_evidence_path.exists()


def test_phase3ah_r2_requires_session_for_schedule_ingestion(tmp_path) -> None:
    template_path = Path(tmp_path) / "phase3ah_roster_review_template.json"
    template_path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="database session"):
        run_phase3ah_r2_backfill(
            None,
            roster_template_path=template_path,
            ingest_schedules=True,
        )


def test_phase3ah_r2_cli_help() -> None:
    result = CliRunner().invoke(app, ["phase3ah-r2-player-prop-backfill", "--help"])

    assert result.exit_code == 0
    assert "Usage" in result.output
