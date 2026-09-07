import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.phase3ah_roster import (
    build_phase3ah_roster_verification,
    write_phase3ah_roster_verification_report,
)


def test_phase3ah_roster_validated_row_becomes_verified_evidence(tmp_path) -> None:
    template_path = _write_roster_template(
        tmp_path,
        [
            {
                "league": "WNBA",
                "player_name": "Caitlin Clark",
                "count": 3,
                "review_status": "VERIFIED",
                "safe_to_apply": True,
                "verified_entity_type": "PLAYER",
                "canonical_player_id": "wnba:caitlin-clark",
                "current_team_key": "WNBA:IND",
                "current_team_name": "Indiana Fever",
                "roster_source_url": "https://example.com/wnba/roster",
                "valid_from": "2026-06-01",
                "valid_to": "2026-12-31",
                "example_tickers": ["WNBA-PLAYER"],
                "example_player_prop_tickers": ["WNBA-PLAYER"],
            }
        ],
    )

    payload = build_phase3ah_roster_verification(roster_template_path=template_path)
    evidence = payload["verified_roster_evidence"][0]

    assert payload["summary"]["verified_roster_rows"] == 1
    assert payload["summary"]["rework_rows"] == 0
    assert payload["summary"]["player_prop_example_tickers_covered"] == 1
    assert payload["summary"]["auto_upgrades_created"] == 0
    assert payload["auto_upgrade_policy"]["phase3ah_roster_creates_verified_links"] is False
    assert evidence["evidence_id"]
    assert evidence["canonical_player_id"] == "wnba:caitlin-clark"
    assert evidence["current_team_key"] == "WNBA:IND"
    assert evidence["blocks_team_link_upgrade"] is False


def test_phase3ah_roster_unverified_template_rows_stay_blocked(tmp_path) -> None:
    template_path = _write_roster_template(
        tmp_path,
        [
            {
                "league": "SOCCER",
                "player_name": "Vinicius Junior",
                "count": 590,
                "review_status": "UNVERIFIED",
                "safe_to_apply": False,
                "verified_entity_type": "PLAYER",
                "canonical_player_id": "",
                "current_team_key": "",
                "current_team_name": "",
                "roster_source_url": "",
                "valid_from": "",
                "valid_to": "",
                "example_tickers": ["SOCCER-PLAYER"],
                "example_player_prop_tickers": ["SOCCER-PLAYER"],
            }
        ],
    )

    payload = build_phase3ah_roster_verification(roster_template_path=template_path)
    reason_counts = {row["reason"]: row["count"] for row in payload["reason_breakdown"]}
    blocker = payload["player_prop_blockers"][0]

    assert payload["summary"]["verified_roster_rows"] == 0
    assert payload["summary"]["rework_rows"] == 1
    assert payload["summary"]["player_prop_example_tickers_still_blocked"] == 1
    assert reason_counts["REVIEW_STATUS_NOT_VERIFIED"] == 1
    assert reason_counts["SAFE_TO_APPLY_FALSE"] == 1
    assert reason_counts["MISSING_CANONICAL_PLAYER_ID"] == 1
    assert reason_counts["MISSING_CURRENT_TEAM_KEY"] == 1
    assert reason_counts["MISSING_CURRENT_TEAM_NAME"] == 1
    assert reason_counts["MISSING_ROSTER_SOURCE_URL"] == 1
    assert reason_counts["INVALID_VALID_FROM"] == 1
    assert blocker["player_name"] == "Vinicius Junior"
    assert blocker["blocked_example_tickers"] == ["SOCCER-PLAYER"]


def test_phase3ah_roster_writer_emits_artifacts(tmp_path) -> None:
    template_path = _write_roster_template(
        tmp_path,
        [
            {
                "league": "SOCCER",
                "player_name": "Jonathan David",
                "count": 5,
                "review_status": "UNVERIFIED",
                "safe_to_apply": False,
                "verified_entity_type": "PLAYER",
                "example_player_prop_tickers": ["SOCCER-PLAYER"],
            }
        ],
    )
    output_dir = Path(tmp_path) / "phase3ah"

    artifacts = write_phase3ah_roster_verification_report(
        output_dir=output_dir,
        roster_template_path=template_path,
    )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    markdown = artifacts.markdown_path.read_text(encoding="utf-8")

    assert artifacts.json_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.verified_evidence_path.exists()
    assert artifacts.rework_queue_path.exists()
    assert artifacts.player_prop_blockers_path.exists()
    assert payload["summary"]["auto_upgrades_created"] == 0
    assert "Phase 3AH Roster / Participant Verification" in markdown


def test_phase3ah_roster_verification_cli_help() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3ah-roster-participant-verification", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3ah-roster-participant-verification" in result.output


def _write_roster_template(tmp_path, rows: list[dict]) -> Path:
    path = Path(tmp_path) / "phase3ah_roster_review_template.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path
