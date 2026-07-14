from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.data.schema import MarketLeg, SportsMarketLink
from kalshi_predictor.phase3bb_r6_sports_provenance import (
    build_phase3bb_r6_sports_provenance_repair,
    write_phase3bb_r6_sports_provenance_repair_report,
)
from kalshi_predictor.utils.time import utc_now


def test_phase3bb_r6_keeps_partial_placeholder_rows_unsafe(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    ticker = "KXMLBGAME-S203001010000-ABC123"
    _write_upstream_reports(reports_dir, placeholder_tickers=[ticker])

    with session_factory() as session:
        _seed_market(session, ticker=ticker, title="yes rd16-w1, yes Brazil")
        _add_leg(session, ticker=ticker, index=0, text="yes rd16-w1")
        _add_partial_link(session, ticker=ticker, league="SOCCER")
        payload = build_phase3bb_r6_sports_provenance_repair(
            session,
            reports_dir=reports_dir,
            max_rows=1000,
        )

    summary = payload["summary"]
    assert summary["status"] == "HOLD_DIAGNOSTIC_ONLY"
    assert summary["partial_rows_before"] == 1
    assert summary["partial_rows_after"] == 1
    assert summary["placeholder_rows_before"] == 1
    assert summary["placeholder_rows_after"] == 1
    assert summary["safe_repair_candidates"] == 0
    assert summary["safe_repairs_applied"] == 0
    assert summary["db_writes_performed"] == 0
    assert payload["sports_repair_candidates"] == []
    assert payload["unsafe_sports_rows"][0]["classification"] == "PLACEHOLDER_ROW"
    assert "PARTIAL_LEGACY_IDENTIFIER" in payload["unsafe_sports_rows"][0]["blocked_reasons"]
    assert payload["safety_flags"]["uses_fuzzy_matching"] is False
    assert payload["safety_flags"]["treats_placeholders_as_real_teams"] is False


def test_phase3bb_r6_writes_requested_artifacts(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    output_dir = reports_dir / "phase3bb_r6"
    ticker = "KXMLBGAME-S203001010000-DEF456"
    _write_upstream_reports(reports_dir, placeholder_tickers=[ticker])

    with session_factory() as session:
        _seed_market(session, ticker=ticker, title="yes rd16-w1, yes Dodgers")
        _add_leg(session, ticker=ticker, index=0, text="yes rd16-w1")
        _add_partial_link(session, ticker=ticker, league="MLB")
        artifacts = write_phase3bb_r6_sports_provenance_repair_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
        )

    assert artifacts.executive_summary_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.candidates_csv_path.exists()
    assert artifacts.unsafe_rows_csv_path.exists()
    assert artifacts.manifest_path.exists()
    assert "DB fingerprint" in artifacts.executive_summary_path.read_text(encoding="utf-8")
    assert "PARTIAL_LEGACY_IDENTIFIER" in artifacts.unsafe_rows_csv_path.read_text(
        encoding="utf-8"
    )
    assert "db_writes_performed" in artifacts.candidates_csv_path.read_text(encoding="utf-8")


def test_phase3bb_r6_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r6-sports-provenance-repair", "--help"])

    assert result.exit_code == 0
    assert "phase3bb-r6-sports-provenance-repair" in result.output
    assert "--max-rows" in result.output
    assert "--ticker-prefix" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3bb_r6.db'}")
    return get_session_factory(engine)


def _seed_market(session, *, ticker: str, title: str) -> None:
    upsert_market(
        session,
        {
            "ticker": ticker,
            "title": title,
            "event_ticker": ticker.rsplit("-", 1)[0],
            "series_ticker": "KXMLBGAME",
            "status": "open",
            "close_time": "2030-01-01T19:00:00Z",
            "market_type": "binary",
        },
    )


def _add_leg(session, *, ticker: str, index: int, text: str) -> None:
    session.add(
        MarketLeg(
            ticker=ticker,
            leg_index=index,
            parsed_at=utc_now(),
            side="YES",
            category="sports",
            market_type="PLAYER_PROP",
            entity_name="rd16-w1",
            operator="AT_LEAST",
            threshold_value="1",
            unit="COUNT",
            confidence="0.95",
            raw_text=text,
            reason="test sports leg",
            raw_json=json.dumps({"phase": "3bb-r6-test"}),
        )
    )


def _add_partial_link(session, *, ticker: str, league: str) -> None:
    session.add(
        SportsMarketLink(
            created_at=utc_now(),
            ticker=ticker,
            league=league,
            game_key=f"{league}:market-derived:{ticker.lower()}:main",
            market_type="PLAYER_PROP",
            link_confidence="0.50",
            link_reason=(
                "Market text names a supported sports league, but no matching ingested "
                "game was found. Ingest sports schedule/team data to upgrade this link."
            ),
            matched_terms_json=json.dumps({"matched_terms": [league.lower()]}),
            raw_json=json.dumps({"source": "market-derived-fallback"}),
        )
    )


def _write_upstream_reports(reports_dir: Path, *, placeholder_tickers: list[str]) -> None:
    _write_json(
        reports_dir / "market_coverage" / "coverage_rows.json",
        [
            {
                "scope_key": "sports",
                "health": "LINKER_DEGRADED",
                "parsed_markets": 1,
                "partial_markets": 1,
                "partial_link_rows": 1,
                "derived_usable_markets": 0,
                "verified_schedule_markets": 0,
                "verified_schedule_link_rows": 0,
            }
        ],
    )
    _write_json(
        reports_dir / "market_coverage" / "link_coverage.json",
        {"reconciliation": {"sports": {"unresolved_partial_markets": 1}}},
    )
    _write_json(
        reports_dir / "phase3az" / "phase3az_gap_analysis.json",
        {
            "gaps": [
                {
                    "gap_id": "sports_partial_provenance",
                    "evidence": "1 sports partial link(s) without upgrade.",
                }
            ]
        },
    )
    _write_json(
        reports_dir / "phase3ah_sports" / "phase3ah_sports_placeholder_watch.json",
        {
            "summary": {
                "phase3ae_blocked_placeholder_rows": len(placeholder_tickers),
                "still_placeholder_rows": len(placeholder_tickers),
                "sports_partial_links_without_upgrade": 1,
            },
            "placeholder_watch_rows": [
                {
                    "source_status": "SOURCE_STILL_PLACEHOLDER",
                    "safe_to_apply": False,
                    "blocks_phase3ae_upgrade": True,
                    "example_tickers": placeholder_tickers,
                }
            ],
        },
    )
    _write_json(reports_dir / "phase_orchestrator.json", {"generated_at": utc_now().isoformat()})


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
