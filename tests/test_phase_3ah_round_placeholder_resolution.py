import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.phase3ah_placeholders import (
    build_phase3ah_round_placeholder_resolution,
    write_phase3ah_round_placeholder_resolution_report,
)


def test_phase3ah_placeholder_resolver_fills_real_source_teams(tmp_path) -> None:
    template_path = _write_template(tmp_path)

    payload = build_phase3ah_round_placeholder_resolution(
        template_path=template_path,
        fetcher=lambda _url: _summary_payload(
            home_abbreviation="BRA",
            home_name="Brazil",
            away_abbreviation="CAN",
            away_name="Canada",
        ),
    )

    row = payload["rows"][0]

    assert payload["summary"]["safe_to_apply_rows"] == 1
    assert row["source_status"] == "RESOLVED_FROM_SOURCE"
    assert row["review_status"] == "VERIFIED"
    assert row["resolved_home_team_key"] == "SOCCER:bra"
    assert row["resolved_home_team_name"] == "Brazil"
    assert row["resolved_away_team_key"] == "SOCCER:can"
    assert row["resolved_away_team_name"] == "Canada"
    assert row["safe_to_apply"] is True
    assert row["blocks_phase3ae_upgrade"] is False


def test_phase3ah_placeholder_resolver_supports_mlb_espn_event_keys(tmp_path) -> None:
    template_path = _write_template(
        tmp_path,
        league="MLB",
        game_key="MLB:espn:mlb:401816082",
        home_placeholder_team_key="MLB:tbd-home",
        away_placeholder_team_key="MLB:tbd-away",
        example_tickers=["KXMLB-PLACEHOLDER"],
    )
    fetched_urls: list[str] = []

    def fetcher(url: str) -> dict:
        fetched_urls.append(url)
        return _summary_payload(
            home_abbreviation="SEA",
            home_name="Seattle Mariners",
            away_abbreviation="NYY",
            away_name="New York Yankees",
        )

    payload = build_phase3ah_round_placeholder_resolution(
        template_path=template_path,
        fetcher=fetcher,
    )

    row = payload["rows"][0]

    assert fetched_urls == [
        "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/summary?event=401816082"
    ]
    assert payload["summary"]["safe_to_apply_rows"] == 1
    assert row["source_status"] == "RESOLVED_FROM_SOURCE"
    assert row["source_event_url"] == "https://www.espn.com/mlb/game/_/gameId/401816082"
    assert row["resolved_home_team_key"] == "MLB:sea"
    assert row["resolved_home_team_name"] == "Seattle Mariners"
    assert row["resolved_away_team_key"] == "MLB:nyy"
    assert row["resolved_away_team_name"] == "New York Yankees"
    assert row["safe_to_apply"] is True
    assert row["blocks_phase3ae_upgrade"] is False


def test_phase3ah_placeholder_resolver_blocks_when_source_still_placeholder(tmp_path) -> None:
    template_path = _write_template(tmp_path)

    payload = build_phase3ah_round_placeholder_resolution(
        template_path=template_path,
        fetcher=lambda _url: _summary_payload(
            home_abbreviation="RD16 W1",
            home_name="Round of 16 1 Winner",
            away_abbreviation="RD16 W2",
            away_name="Round of 16 2 Winner",
        ),
    )

    row = payload["rows"][0]

    assert payload["summary"]["safe_to_apply_rows"] == 0
    assert payload["summary"]["still_placeholder_rows"] == 1
    assert row["source_status"] == "SOURCE_STILL_PLACEHOLDER"
    assert row["source_home_team_name"] == "Round of 16 1 Winner"
    assert row["source_away_team_name"] == "Round of 16 2 Winner"
    assert row["resolved_home_team_key"] == ""
    assert row["safe_to_apply"] is False
    assert row["blocks_phase3ae_upgrade"] is True


def test_phase3ah_placeholder_resolver_writer_emits_report_and_filled_template(tmp_path) -> None:
    template_path = _write_template(tmp_path)
    output_dir = Path(tmp_path) / "phase3ah"

    artifacts = write_phase3ah_round_placeholder_resolution_report(
        output_dir=output_dir,
        template_path=template_path,
        fetcher=lambda _url: _summary_payload(
            home_abbreviation="BRA",
            home_name="Brazil",
            away_abbreviation="CAN",
            away_name="Canada",
        ),
    )

    assert artifacts.json_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.filled_template_path.exists()
    filled_rows = json.loads(artifacts.filled_template_path.read_text(encoding="utf-8"))
    assert filled_rows[0]["safe_to_apply"] is True


def test_phase3ah_placeholder_resolver_cli_help() -> None:
    result = CliRunner().invoke(app, ["phase3ah-round-placeholder-resolution", "--help"])

    assert result.exit_code == 0
    assert "phase3ah-round-placeholder-resolution" in result.output


def _write_template(
    tmp_path,
    *,
    league: str = "SOCCER",
    game_key: str = "SOCCER:espn:fifa.world:760510",
    home_placeholder_team_key: str = "SOCCER:rd16-w1",
    away_placeholder_team_key: str = "SOCCER:rd16-w2",
    example_tickers: list[str] | None = None,
) -> Path:
    path = Path(tmp_path) / "phase3ah_round_placeholder_resolution_template.json"
    path.write_text(
        json.dumps(
            [
                {
                    "league": league,
                    "game_key": game_key,
                    "home_placeholder_team_key": home_placeholder_team_key,
                    "away_placeholder_team_key": away_placeholder_team_key,
                    "count": 48,
                    "example_tickers": example_tickers or ["SOCCER-RD16"],
                    "review_status": "UNVERIFIED",
                    "resolved_home_team_key": "",
                    "resolved_home_team_name": "",
                    "resolved_away_team_key": "",
                    "resolved_away_team_name": "",
                    "official_schedule_source_url": "",
                    "safe_to_apply": False,
                    "blocks_phase3ae_upgrade": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    return path


def _summary_payload(
    *,
    home_abbreviation: str,
    home_name: str,
    away_abbreviation: str,
    away_name: str,
) -> dict:
    return {
        "header": {
            "competitions": [
                {
                    "competitors": [
                        {
                            "homeAway": "home",
                            "team": {
                                "abbreviation": home_abbreviation,
                                "displayName": home_name,
                                "shortDisplayName": home_name,
                            },
                        },
                        {
                            "homeAway": "away",
                            "team": {
                                "abbreviation": away_abbreviation,
                                "displayName": away_name,
                                "shortDisplayName": away_name,
                            },
                        },
                    ]
                }
            ]
        }
    }
