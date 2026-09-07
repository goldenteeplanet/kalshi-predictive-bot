from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.data.schema import MarketLeg, SportsMarketLink
from kalshi_predictor.phase3z_r2 import (
    build_phase3z_r2_sports_provenance_repair,
    write_phase3z_r2_sports_provenance_repair_report,
)
from kalshi_predictor.utils.time import utc_now


def test_phase3zr2_groups_partial_legacy_and_blocks_placeholder_rows(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    ticker = "KXMVESPORTSMULTIGAMEEXTENDED-S202612345678-ABC123"
    _write_upstream_reports(reports_dir, placeholder_tickers=[ticker])

    with session_factory() as session:
        _seed_market(session, ticker=ticker, title="yes rd16-w1,yes Brazil,yes Vinicius Junior: 1+")
        _add_leg(session, ticker=ticker, index=0, market_type="TOTAL", text="yes rd16-w1")
        _add_leg(
            session,
            ticker=ticker,
            index=1,
            market_type="PLAYER_PROP",
            text="yes Vinicius Junior: 1+",
            entity="Vinicius Junior",
        )
        _add_partial_link(session, ticker=ticker, league="SOCCER")

        payload = build_phase3z_r2_sports_provenance_repair(
            session,
            reports_dir=reports_dir,
        )

    assert payload["summary"]["partial_legacy_markets"] == 1
    assert payload["summary"]["rows_safe_to_repair"] == 0
    assert payload["summary"]["auto_upgrades_created"] == 0
    assert payload["phase3ae_gate"]["auto_upgrade_allowed"] is False
    assert payload["phase3ae_gate"]["status"] == "HOLD_PLACEHOLDER_UPGRADES"
    row = payload["degraded_rows"][0]
    assert row["placeholder_involved"] is True
    assert row["safe_to_repair"] is False
    assert "PLACEHOLDER_TEAM" in row["blocked_reasons"]
    assert "PLAYER_PROP_REQUIRES_ROSTER_EVIDENCE" in row["blocked_reasons"]
    assert "UNSUPPORTED_MULTI_LEG" in row["blocked_reasons"]


def test_phase3zr2_excludes_cross_category_composites_from_sports_repair(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    ticker = "KXMVECROSSCATEGORY-S202612345678-CROSS"
    _write_upstream_reports(reports_dir, placeholder_tickers=[])

    with session_factory() as session:
        _seed_market(session, ticker=ticker, title="yes Brazil,yes Bitcoin above $100,000")
        _add_leg(
            session,
            ticker=ticker,
            index=0,
            market_type="PLAYER_PROP",
            text="yes Brazil",
            category="cross_category",
        )
        _add_partial_link(session, ticker=ticker, league="MLB")
        payload = build_phase3z_r2_sports_provenance_repair(
            session,
            reports_dir=reports_dir,
        )

    assert payload["summary"]["raw_partial_legacy_markets"] == 1
    assert payload["summary"]["excluded_composite_partial_markets"] == 1
    assert payload["summary"]["partial_legacy_markets"] == 0
    assert payload["summary"]["candidate_degraded_rows"] == 0
    assert payload["summary"]["rows_reviewed"] == 0
    assert payload["degraded_rows"] == []
    assert payload["phase3ae_gate"]["phase3ae_can_run_from_this_report"] is False


def test_phase3zr2_reconciles_distinct_markets_and_raw_link_rows(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    ticker = "KXMVESPORTSMULTIGAMEEXTENDED-S202612345678-DEF456"
    _write_upstream_reports(reports_dir, placeholder_tickers=[])

    with session_factory() as session:
        _seed_market(session, ticker=ticker, title="yes Dodgers wins by over 6.5 runs")
        _add_leg(session, ticker=ticker, index=0, market_type="TOTAL", text="yes Dodgers")
        _add_partial_link(session, ticker=ticker, league="MLB", game_suffix="a")
        _add_partial_link(session, ticker=ticker, league="MLB", game_suffix="b")
        payload = build_phase3z_r2_sports_provenance_repair(
            session,
            reports_dir=reports_dir,
        )

    values = payload["count_reconciliation"]["values"]
    assert payload["summary"]["partial_legacy_markets"] == 1
    assert payload["summary"]["partial_legacy_link_rows"] == 2
    assert values["db_partial_legacy_markets"] == 1
    assert values["db_partial_legacy_link_rows"] == 2
    assert payload["count_reconciliation"]["consistent_market_count"] is True
    group = payload["grouped_degraded_links"][0]
    assert group["reason_code"] == "LEGACY_IDENTIFIER"
    assert group["count"] == 1
    assert group["safe_to_repair"] is False


def test_phase3zr2_bounded_scan_marks_report_truncated(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_upstream_reports(reports_dir, placeholder_tickers=[])

    with session_factory() as session:
        for index in range(3):
            ticker = f"KXMVESPORTSMULTIGAMEEXTENDED-S202612345678-BOUNDED{index}"
            _seed_market(session, ticker=ticker, title=f"yes Team {index} wins")
            _add_leg(session, ticker=ticker, index=0, market_type="MONEYLINE", text="yes Team")
            _add_partial_link(session, ticker=ticker, league="MLB")

        payload = build_phase3z_r2_sports_provenance_repair(
            session,
            reports_dir=reports_dir,
            max_rows=2,
        )

    assert payload["summary"]["partial_legacy_markets"] == 3
    assert payload["summary"]["candidate_degraded_rows"] == 3
    assert payload["summary"]["rows_reviewed"] == 2
    assert payload["summary"]["row_scan_complete"] is False
    assert payload["summary"]["row_scan_truncated"] is True
    assert payload["row_scan"]["rows_materialized"] == 2
    assert payload["phase3ae_gate"]["status"] == "HOLD_BOUNDED_SCAN_INCOMPLETE"
    assert len(payload["degraded_rows"]) == 2


def test_phase3zr2_uses_fresh_link_coverage_unlinked_count(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_upstream_reports(reports_dir, placeholder_tickers=[])
    _write_json(
        reports_dir / "market_coverage" / "link_coverage.json",
        {
            "category_rows": [
                {
                    "category": "sports",
                    "parsed_markets": 10,
                    "linked_markets": 3,
                    "unlinked_markets": 7,
                    "partial_markets": 0,
                    "partial_link_rows": 0,
                }
            ]
        },
    )

    with session_factory() as session:
        payload = build_phase3z_r2_sports_provenance_repair(
            session,
            reports_dir=reports_dir,
        )

    assert payload["summary"]["total_sports_parsed_markets"] == 10
    assert payload["summary"]["unlinked_parsed_markets"] == 7
    assert payload["row_scan"]["unlinked_candidate_rows"] == 7


def test_phase3zr2_writer_and_cli_help(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    output_dir = tmp_path / "phase3z_r2"
    _write_upstream_reports(reports_dir, placeholder_tickers=[])

    with session_factory() as session:
        artifacts = write_phase3z_r2_sports_provenance_repair_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
        )

    assert artifacts.json_path.exists()
    assert artifacts.rows_path.exists()
    assert "Sports Provenance Coverage Repair" in artifacts.markdown_path.read_text(
        encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["phase3z-r2-sports-provenance-repair", "--help"])
    assert result.exit_code == 0
    assert "phase3z-r2-sports-provenance-repair" in result.output
    assert "--max-rows" in result.output
    assert "--ticker-prefix" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3z_r2.db'}")
    return get_session_factory(engine)


def _seed_market(session, *, ticker: str, title: str) -> None:
    series_ticker = (
        "KXMVECROSSCATEGORY"
        if ticker.startswith("KXMVECROSSCATEGORY")
        else "KXMVESPORTSMULTIGAMEEXTENDED"
    )
    upsert_market(
        session,
        {
            "ticker": ticker,
            "title": title,
            "event_ticker": ticker.rsplit("-", 1)[0],
            "series_ticker": series_ticker,
            "status": "open",
            "close_time": "2026-07-08T19:00:00Z",
            "market_type": "binary",
        },
    )


def _add_leg(
    session,
    *,
    ticker: str,
    index: int,
    market_type: str,
    text: str,
    entity: str | None = None,
    category: str = "sports",
) -> None:
    session.add(
        MarketLeg(
            ticker=ticker,
            leg_index=index,
            parsed_at=utc_now(),
            side="YES",
            category=category,
            market_type=market_type,
            entity_name=entity,
            operator="AT_LEAST",
            threshold_value="1",
            unit="COUNT",
            confidence="0.95",
            raw_text=text,
            reason="test sports leg",
            raw_json=json.dumps({"phase": "3z-r2-test"}),
        )
    )


def _add_partial_link(
    session,
    *,
    ticker: str,
    league: str,
    game_suffix: str = "main",
) -> None:
    session.add(
        SportsMarketLink(
            created_at=utc_now(),
            ticker=ticker,
            league=league,
            game_key=f"{league}:market-derived:{ticker.lower()}:{game_suffix}",
            market_type="PLAYER_PROP" if league == "SOCCER" else "TOTAL",
            link_confidence="0.50",
            link_reason=(
                "Market text names a supported sports league, but no matching ingested "
                "game was found. Ingest sports schedule/team data to upgrade this link."
            ),
            matched_terms_json=json.dumps({"matched_terms": [league.lower(), "market_derived"]}),
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
                "partial_link_rows": 2,
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
                "sports_partial_links_without_upgrade": 2,
            },
            "placeholder_watch_rows": [
                {
                    "game_key": "SOCCER:espn:fifa.world:760510",
                    "source_status": "SOURCE_STILL_PLACEHOLDER",
                    "safe_to_apply": False,
                    "blocks_phase3ae_upgrade": True,
                    "example_tickers": placeholder_tickers,
                }
            ],
        },
    )
    _write_json(reports_dir / "phase_orchestrator.json", {"generated_at": _iso()})


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _iso() -> str:
    return datetime(2026, 6, 28, tzinfo=UTC).isoformat()
