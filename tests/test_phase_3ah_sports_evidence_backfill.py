import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3ah_sports import (
    build_phase3ah_sports_evidence_backfill,
    write_phase3ah_sports_evidence_report,
)


def test_phase3ah_groups_failed_close_dates_into_schedule_windows(tmp_path) -> None:
    repair_path, alias_path = _write_inputs(tmp_path)
    payload = build_phase3ah_sports_evidence_backfill(
        None,
        repair_path=repair_path,
        alias_candidates_path=alias_path,
        leagues="MLB,WNBA,SOCCER",
        window_days_before=1,
        window_days_after=1,
    )

    windows = payload["schedule_backfill_plan"]
    mlb_window = next(row for row in windows if row["league"] == "MLB")
    wnba_window = next(row for row in windows if row["league"] == "WNBA")

    assert payload["summary"]["schedule_backfill_rows"] == 2
    assert mlb_window["start_date"] == "2026-06-23"
    assert mlb_window["end_date"] == "2026-06-25"
    assert "--start-date 2026-06-23 --days-ahead 3" in mlb_window["command"]
    assert wnba_window["start_date"] == "2026-06-26"
    assert wnba_window["end_date"] == "2026-06-28"
    assert payload["summary"]["auto_upgrades_created"] == 0
    assert payload["auto_upgrade_policy"]["phase3ah_creates_verified_links"] is False


def test_phase3ah_separates_team_aliases_from_roster_evidence(tmp_path) -> None:
    repair_path, alias_path = _write_inputs(tmp_path)
    payload = build_phase3ah_sports_evidence_backfill(
        None,
        repair_path=repair_path,
        alias_candidates_path=alias_path,
        leagues="MLB,WNBA,SOCCER",
    )

    team_aliases = payload["team_alias_review_template"]
    roster_rows = payload["roster_review_template"]
    gate = payload["phase3ae_ready_gate"]

    assert any(row["entity"] == "Man City" for row in team_aliases)
    assert any(row["player_name"] == "Vinicius Junior" for row in roster_rows)
    assert any(row["player_name"] == "Caitlin Clark" for row in roster_rows)
    assert all(row["blocks_team_link_upgrade"] for row in roster_rows)
    assert payload["summary"]["player_prop_rows"] == 2
    assert gate["phase3ah_auto_upgrades_created"] == 0
    assert any(
        row["reason"] == "PLAYER_PROP_REQUIRES_ROSTER_MAPPING"
        for row in gate["blocked_breakdown"]
    )


def test_phase3ah_uses_current_roster_diagnostics_and_preserves_verified_rows(
    tmp_path,
) -> None:
    repair_path, alias_path = _write_inputs(tmp_path)
    diagnostics_path = _write_roster_diagnostics(tmp_path)
    existing_path = Path(tmp_path) / "existing_roster_template.json"
    existing_path.write_text(
        json.dumps(
            [
                {
                    "league": "SOCCER",
                    "player_name": "Vinicius Junior",
                    "count": 590,
                    "review_status": "VERIFIED",
                    "safe_to_apply": True,
                    "verified_entity_type": "PLAYER",
                    "canonical_player_id": "fifa:brazil:vinicius-junior",
                    "current_team_key": "SOCCER:bra",
                    "current_team_name": "Brazil",
                    "roster_source_url": "https://www.fifa.com/",
                    "valid_from": "2026-06-27",
                    "valid_to": "",
                    "example_player_prop_tickers": ["SOCCER-OLD"],
                },
                {
                    "league": "SOCCER",
                    "player_name": "Shohei Ohtani",
                    "count": 4,
                    "review_status": "UNVERIFIED",
                    "safe_to_apply": False,
                    "verified_entity_type": "PLAYER",
                },
            ]
        ),
        encoding="utf-8",
    )

    payload = build_phase3ah_sports_evidence_backfill(
        None,
        repair_path=repair_path,
        alias_candidates_path=alias_path,
        roster_candidate_diagnostics_path=diagnostics_path,
        existing_roster_template_path=existing_path,
        leagues="MLB,WNBA,SOCCER",
    )

    roster_rows = payload["roster_review_template"]
    player_names = {row["player_name"] for row in roster_rows}
    vinicius = next(row for row in roster_rows if row["player_name"] == "Vinicius Junior")
    amad = next(row for row in roster_rows if row["player_name"] == "Amad Diallo")

    assert "Shohei Ohtani" not in player_names
    assert vinicius["review_status"] == "VERIFIED"
    assert vinicius["canonical_player_id"] == "fifa:brazil:vinicius-junior"
    assert amad["source"] == "phase3ae_roster_candidate_diagnostics"
    assert payload["summary"]["current_roster_candidate_rows"] == 2
    assert payload["summary"]["round_placeholder_resolution_rows"] == 1
    assert payload["round_placeholder_resolution_template"][0]["game_key"] == (
        "SOCCER:espn:fifa.world:760510"
    )


def test_phase3ah_skips_non_player_roster_candidates_from_diagnostics(tmp_path) -> None:
    repair_path, alias_path = _write_inputs(tmp_path)
    diagnostics_path = Path(tmp_path) / "phase3ae_roster_candidate_diagnostics.json"
    diagnostics_path.write_text(
        json.dumps(
            {
                "top_missing_roster_players": [
                    {
                        "league": "SOCCER",
                        "player_name": "Congo DR",
                        "count": 2,
                        "example_tickers": ["SOCCER-CONGO"],
                        "verified_entity_type": "TEAM_OR_COMPETITION_ENTITY",
                        "blocks_roster_evidence": True,
                    },
                    {
                        "league": "SOCCER",
                        "player_name": "Amad Diallo",
                        "count": 4,
                        "example_tickers": ["SOCCER-AMAD"],
                        "verified_entity_type": "PLAYER",
                    },
                ],
                "top_round_placeholder_games": [],
            }
        ),
        encoding="utf-8",
    )

    payload = build_phase3ah_sports_evidence_backfill(
        None,
        repair_path=repair_path,
        alias_candidates_path=alias_path,
        roster_candidate_diagnostics_path=diagnostics_path,
        leagues="MLB,WNBA,SOCCER",
    )

    player_names = {row["player_name"] for row in payload["roster_review_template"]}

    assert "Amad Diallo" in player_names
    assert "Congo DR" not in player_names
    assert payload["summary"]["current_roster_candidate_rows"] == 1


def test_phase3ah_roster_template_filters_alias_team_and_cross_sport_leaks(
    tmp_path,
) -> None:
    repair_path, alias_path = _write_inputs(tmp_path)
    alias_rows = json.loads(alias_path.read_text(encoding="utf-8"))
    alias_rows.extend(
        [
            {
                "league": "SOCCER",
                "entity": "Bosnia and Herzegovina",
                "entity_role": "player_or_participant_alias",
                "count": 12,
                "example_tickers": ["SOCCER-COUNTRY"],
            },
            {
                "league": "SOCCER",
                "entity": "Bosnia and Herzegovina",
                "entity_role": "team_or_entity_alias",
                "count": 2,
                "example_tickers": ["SOCCER-COUNTRY"],
            },
            {
                "league": "SOCCER",
                "entity": "Shohei Ohtani",
                "entity_role": "player_or_participant_alias",
                "count": 4,
                "example_tickers": ["SOCCER-SHOHEI"],
            },
        ]
    )
    alias_path.write_text(json.dumps(alias_rows), encoding="utf-8")

    payload = build_phase3ah_sports_evidence_backfill(
        None,
        repair_path=repair_path,
        alias_candidates_path=alias_path,
        leagues="MLB,WNBA,SOCCER",
    )

    player_names = {row["player_name"] for row in payload["roster_review_template"]}

    assert "Vinicius Junior" in player_names
    assert "Bosnia and Herzegovina" not in player_names
    assert "Shohei Ohtani" not in player_names


def test_phase3ah_diagnostics_without_selected_roster_rows_suppresses_alias_fallback(
    tmp_path,
) -> None:
    repair_path, alias_path = _write_inputs(tmp_path)
    diagnostics_path = Path(tmp_path) / "phase3ae_roster_candidate_diagnostics.json"
    diagnostics_path.write_text(
        json.dumps(
            {
                "top_missing_roster_players": [
                    {
                        "league": "WNBA",
                        "player_name": "Courtney Williams",
                        "count": 2,
                        "example_tickers": ["WNBA-COURTNEY"],
                    }
                ],
                "top_round_placeholder_games": [],
            }
        ),
        encoding="utf-8",
    )

    payload = build_phase3ah_sports_evidence_backfill(
        None,
        repair_path=repair_path,
        alias_candidates_path=alias_path,
        roster_candidate_diagnostics_path=diagnostics_path,
        leagues="SOCCER",
    )

    assert payload["summary"]["current_roster_candidate_rows"] == 0
    assert payload["roster_review_template"] == []


def test_phase3ah_fetches_windows_through_phase3af_runner(tmp_path) -> None:
    repair_path, alias_path = _write_inputs(tmp_path)
    calls = []

    def fake_bootstrap(session, **kwargs):
        calls.append(kwargs)
        return {
            "summary": {"games_inserted": 2 if kwargs["ingest"] else 0},
            "schedule_paths": [f"schedule-{kwargs['leagues']}.json"],
            "errors": [],
        }

    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        artifacts = write_phase3ah_sports_evidence_report(
            session,
            output_dir=Path(tmp_path) / "phase3ah",
            repair_path=repair_path,
            alias_candidates_path=alias_path,
            fetch_schedules=True,
            ingest_schedules=True,
            bootstrap_runner=fake_bootstrap,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))

    assert calls
    assert all(call["ingest"] is True for call in calls)
    assert any(call["start_date"] == "2026-06-23" for call in calls)
    assert artifacts.schedule_plan_path.exists()
    assert artifacts.roster_template_path.exists()
    assert payload["summary"]["schedule_fetches_run"] == len(calls)
    assert payload["summary"]["schedules_ingested"] == len(calls) * 2
    assert artifacts.round_placeholder_template_path.exists()


def test_phase3ah_sports_evidence_cli_help() -> None:
    result = CliRunner().invoke(app, ["phase3ah-sports-evidence-backfill", "--help"])

    assert result.exit_code == 0
    assert "phase3ah-sports-evidence-backfill" in result.output


def _write_inputs(tmp_path) -> tuple[Path, Path]:
    repair_path = Path(tmp_path) / "phase3ag_repair.json"
    alias_path = Path(tmp_path) / "phase3ag_aliases.json"
    repair_path.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "ticker": "MLB-WINDOW",
                        "phase3ae_status": "NO_VERIFIED_MATCH",
                        "league": "MLB",
                        "market_type": "TOTAL",
                        "market_close_time": "2026-06-24T03:25:54",
                        "market_title": "Dodgers and Boston total",
                        "primary_cause": "NO_VERIFIED_GAMES_IN_SCHEDULE_WINDOW",
                        "entities": ["Boston", "Dodgers"],
                        "clean_candidate_count": 0,
                        "game_candidates": [],
                    },
                    {
                        "ticker": "WNBA-PLAYER",
                        "phase3ae_status": "NO_VERIFIED_MATCH",
                        "league": "WNBA",
                        "market_type": "PLAYER_PROP",
                        "market_close_time": "2026-06-27T18:00:00",
                        "market_title": "Caitlin Clark over points",
                        "primary_cause": "NO_VERIFIED_GAMES_IN_SCHEDULE_WINDOW",
                        "entities": ["Caitlin Clark"],
                        "clean_candidate_count": 0,
                        "game_candidates": [],
                    },
                    {
                        "ticker": "SOCCER-PLAYER",
                        "phase3ae_status": "NO_VERIFIED_MATCH",
                        "league": "SOCCER",
                        "market_type": "PLAYER_PROP",
                        "market_close_time": "2026-06-29T18:00:00",
                        "market_title": "Vinicius Junior shot on target",
                        "primary_cause": "PLAYER_PROP_NEEDS_PLAYER_TEAM_MAPPING",
                        "entities": ["Vinicius Junior"],
                        "clean_candidate_count": 0,
                        "game_candidates": [],
                    },
                    {
                        "ticker": "SOCCER-MULTI",
                        "phase3ae_status": "NO_VERIFIED_MATCH",
                        "league": "SOCCER",
                        "market_type": "MONEYLINE",
                        "market_close_time": "2026-06-29T18:00:00",
                        "market_title": "Man City and Arsenal and Tie",
                        "primary_cause": "MULTI_LEG_MARKET_REQUIRES_MANUAL_DISAMBIGUATION",
                        "entities": ["Man City", "Arsenal"],
                        "clean_candidate_count": 0,
                        "game_candidates": [],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    alias_path.write_text(
        json.dumps(
            [
                {
                    "league": "SOCCER",
                    "entity": "Man City",
                    "entity_role": "team_or_entity_alias",
                    "count": 4,
                    "example_tickers": ["SOCCER-MULTI"],
                },
                {
                    "league": "SOCCER",
                    "entity": "Vinicius Junior",
                    "entity_role": "player_or_participant_alias",
                    "count": 5,
                    "example_tickers": ["SOCCER-PLAYER"],
                },
                {
                    "league": "WNBA",
                    "entity": "Caitlin Clark",
                    "entity_role": "player_or_participant_alias",
                    "count": 3,
                    "example_tickers": ["WNBA-PLAYER"],
                },
            ]
        ),
        encoding="utf-8",
    )
    return repair_path, alias_path


def _write_roster_diagnostics(tmp_path) -> Path:
    path = Path(tmp_path) / "phase3ae_roster_candidate_diagnostics.json"
    path.write_text(
        json.dumps(
            {
                "top_missing_roster_players": [
                    {
                        "league": "SOCCER",
                        "player_name": "Amad Diallo",
                        "count": 4,
                        "example_tickers": ["SOCCER-AMAD"],
                    },
                    {
                        "league": "WNBA",
                        "player_name": "Courtney Williams",
                        "count": 2,
                        "example_tickers": ["WNBA-COURTNEY"],
                    },
                ],
                "top_cross_sport_player_leaks": [
                    {
                        "target_league": "SOCCER",
                        "inferred_league": "MLB",
                        "player_name": "Shohei Ohtani",
                        "count": 5,
                    }
                ],
                "top_round_placeholder_games": [
                    {
                        "league": "SOCCER",
                        "game_key": "SOCCER:espn:fifa.world:760510",
                        "home_team_key": "SOCCER:rd16-w1",
                        "away_team_key": "SOCCER:rd16-w2",
                        "count": 48,
                        "example_tickers": ["SOCCER-RD16"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ah_sports.db'}")
    return get_session_factory(engine)
