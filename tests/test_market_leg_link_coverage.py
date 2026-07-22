from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.repository import insert_crypto_market_link
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.data.schema import MarketLeg
from kalshi_predictor.market_legs import (
    generate_link_coverage_report,
    link_coverage_dashboard,
    parse_and_store_market_legs,
    parse_market_legs,
)
from kalshi_predictor.professional_ux.service import build_default_shell_context
from kalshi_predictor.sports.repository import insert_sports_market_link
from kalshi_predictor.ui import routes as ui_routes
from kalshi_predictor.ui.app import create_app


def test_market_leg_parser_extracts_player_prop_legs(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-TEST",
                "title": "yes Cal Raleigh: 1+,yes Kyle Schwarber: 1+",
                "series_ticker": "KXMVESPORTSMULTIGAMEEXTENDED",
            },
        )

        legs = parse_market_legs(market)

    assert len(legs) == 2
    assert legs[0].side == "YES"
    assert legs[0].category == "sports"
    assert legs[0].market_type == "PLAYER_PROP"
    assert legs[0].entity_name == "Cal Raleigh"
    assert legs[0].operator == "AT_LEAST"
    assert legs[0].threshold_value == "1"


def test_market_leg_parser_routes_kxmv_sports_family_before_general_fallback(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-S2026SEC-TEST",
                "title": "yes Republic of Korea wins by more than 1.5 goals",
                "series_ticker": "KXMVESPORTSMULTIGAMEEXTENDED",
            },
        )

        legs = parse_market_legs(market)

    assert len(legs) == 1
    assert legs[0].category == "sports"
    assert "KXMV sports multi-game market family" in legs[0].reason


def test_market_leg_parser_keeps_kxmv_target_price_legs_crypto(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-CRYPTO-TARGETS",
                "title": (
                    "yes Target Price: $59,867.76,"
                    "no Target Price: $0.0726134,"
                    "yes Target Price: $1,577.21,"
                    "yes Target Price: $1.0462"
                ),
                "series_ticker": "KXMVESPORTSMULTIGAMEEXTENDED",
            },
        )

        legs = parse_market_legs(market)

    assert len(legs) == 4
    assert {leg.category for leg in legs} == {"crypto"}
    assert all(leg.market_type == "TARGET_PRICE" for leg in legs)
    assert {leg.unit for leg in legs} == {"USD"}


def test_market_leg_parser_keeps_mixed_kxmv_crypto_and_sports_separate(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-MIXED-TARGETS",
                "title": (
                    "yes Target Price: $60,048.93,"
                    "yes Reg Time: Brazil,"
                    "yes Reg Time: Over 2.5 goals scored,"
                    "yes Target Price: $1.0507"
                ),
                "series_ticker": "KXMVESPORTSMULTIGAMEEXTENDED",
            },
        )

        legs = parse_market_legs(market)

    assert [leg.category for leg in legs] == ["crypto", "sports", "sports", "crypto"]
    assert legs[1].market_type == "UNKNOWN"
    assert legs[2].market_type == "TOTAL"


def test_market_leg_parser_blocks_sports_total_inside_crypto_context(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXBTC-MIXED-TITLE",
                "title": "yes Target Price: $100,000,yes Over 5.5 runs scored",
                "series_ticker": "KXBTC",
            },
        )

        legs = parse_market_legs(market)

    assert [leg.category for leg in legs] == ["crypto", "sports"]
    assert legs[0].market_type == "TARGET_PRICE"
    assert legs[1].market_type == "TOTAL"
    assert "sports metric" in legs[1].reason


def test_market_leg_parser_isolates_kxmve_cross_category_family(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXMVECROSSCATEGORY-S2026SEC-TEST",
                "title": "yes Caitlin Clark: 1+,yes Bitcoin above $100,000",
                "series_ticker": "KXMVECROSSCATEGORY",
            },
        )

        legs = parse_market_legs(market)

    assert len(legs) == 2
    assert {leg.category for leg in legs} == {"cross_category"}
    assert all("KXMV cross-category market family" in leg.reason for leg in legs)


def test_market_leg_parser_routes_kxcod_esports_family_before_general_fallback(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXCODMAP-26JUN281315TEXLAT-1-LAT",
                "title": (
                    "Will Los Angeles Thieves win map 1 in the OpTic Texas vs. "
                    "Los Angeles Thieves match?"
                ),
                "event_ticker": "KXCODMAP-26JUN281315TEXLAT-1",
            },
        )

        legs = parse_market_legs(market)

    assert len(legs) == 1
    assert legs[0].category == "sports"
    assert legs[0].market_type == "MONEYLINE"
    assert "KXCOD esports market family" in legs[0].reason


def test_market_leg_parser_routes_obvious_esports_families_before_general_fallback(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    cases = [
        (
            "KXCS2GAME-26JUN300400FORDON-DON",
            "Will Donstu Esports win the Fortress vs. Donstu Esports CS2 match?",
            "KXCS2 esports market family",
        ),
        (
            "KXCS2TOTALMAPS-26JUN300400FORDON-T2",
            "Will Fortress vs. Donstu Esports CS2 match go to a third map?",
            "KXCS2 esports market family",
        ),
        (
            "KXVALORANTMAP-26JUL010700XCAT1A-1-T1A",
            "Will XLG Esports win map 1 in the XLG Esports vs. Cat 1A Valorant match?",
            "KXVALORANT esports market family",
        ),
    ]
    with session_factory() as session:
        for ticker, title, reason in cases:
            market = upsert_market(
                session,
                {
                    "ticker": ticker,
                    "title": title,
                    "event_ticker": ticker.rsplit("-", 1)[0],
                },
            )

            legs = parse_market_legs(market)

            assert len(legs) == 1
            assert legs[0].category == "sports"
            assert reason in legs[0].reason


def test_market_leg_parser_routes_obvious_cricket_families_before_general_fallback(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    cases = [
        (
            "KXT20MATCH-26JUL011400LANDER-LAN",
            "Will Lancashire win the Lancashire vs. Derbyshire T20 match?",
        ),
        (
            "KXWT20MATCH-26JUL010930LATYOR-LAT",
            "Will Lancashire Thunder win the Lancashire Thunder vs. Yorkshire WT20 match?",
        ),
        (
            "KXWODIMATCH-26JUL120545WINIRL-IRE",
            "Will Ireland win the Ireland vs. England WODI match?",
        ),
    ]
    with session_factory() as session:
        for ticker, title in cases:
            market = upsert_market(
                session,
                {
                    "ticker": ticker,
                    "title": title,
                    "event_ticker": ticker.rsplit("-", 1)[0],
                },
            )

            legs = parse_market_legs(market)

            assert len(legs) == 1
            assert legs[0].category == "sports"
            assert "Kalshi cricket market family" in legs[0].reason


def test_link_coverage_displays_cross_category_as_observed_not_linkable(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXMVECROSSCATEGORY-COVERAGE",
                "title": "yes Caitlin Clark: 1+,yes Bitcoin above $100,000",
                "series_ticker": "KXMVECROSSCATEGORY",
            },
        )
        parse_and_store_market_legs(session, refresh=True)

        coverage = link_coverage_dashboard(session)

    cross_category = _coverage_row(coverage, "cross_category")
    assert cross_category["parsed_markets"] == 1
    assert cross_category["linked_markets"] == 0
    assert cross_category["unlinked_markets"] == 0
    assert cross_category["status"] == "OBSERVED"
    assert cross_category["coverage_percent"] == "n/a"
    assert cross_category["status_class"] == "status-incomplete"
    assert "Parked as non-linkable cross-category context" in cross_category["next_action"]


def test_link_coverage_excludes_cross_category_legacy_sports_links_from_partial(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXMVECROSSCATEGORY-LEGACY-SPORTS-LINK",
                "title": "yes Brazil,yes Bitcoin above $100,000",
                "series_ticker": "KXMVECROSSCATEGORY",
            },
        )
        parse_and_store_market_legs(session, refresh=True)
        insert_sports_market_link(
            session,
            ticker="KXMVECROSSCATEGORY-LEGACY-SPORTS-LINK",
            league="MLB",
            game_key="MLB:market-derived:kxmvecrosscategory-legacy",
            market_type="PLAYER_PROP",
            link_confidence=Decimal("0.50"),
            link_reason="Market-derived fallback link.",
            matched_terms=["mlb", "market_derived"],
            raw_json={"source": "market-derived-fallback"},
        )

        coverage = link_coverage_dashboard(session)

    sports = _coverage_row(coverage, "sports")
    cross_category = _coverage_row(coverage, "cross_category")
    assert sports["partial_link_rows"] == 1
    assert sports["partial_markets"] == 0
    assert sports["status"] == "NO_PARSED_MARKETS"
    assert cross_category["unsupported_multileg_markets"] == 1
    assert coverage["bottleneck"]["status"] == "CONNECTED"
    assert "parked outside single-market remediation" in coverage["bottleneck"]["message"]
    assert "safe_to_apply_rows" in coverage["bottleneck"]["next_action"]
    assert coverage["next_commands"] == []
    assert not any("phase-orchestrator" in command for command in coverage["next_commands"])
    assert not any("phase3ah" in command for command in coverage["next_commands"])


def test_link_coverage_derived_connected_sports_does_not_rerun_repair(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXMLB-DERIVED",
                "title": "yes Dodgers wins by more than 6.5 runs",
                "series_ticker": "KXMLB",
            },
        )
        parse_and_store_market_legs(session, refresh=True)
        insert_sports_market_link(
            session,
            ticker="KXMLB-DERIVED",
            league="MLB",
            game_key="MLB:kalshi-event-derived:kxmlb-derived",
            market_type="MONEYLINE",
            link_confidence=Decimal("0.75"),
            link_reason="Kalshi-event-derived sports link.",
            matched_terms=["mlb", "kalshi_event_derived"],
            raw_json={"source": "kalshi_event_derived"},
        )

        coverage = link_coverage_dashboard(session)

    sports = _coverage_row(coverage, "sports")
    assert sports["status"] == "DERIVED_CONNECTED"
    assert sports["partial_markets"] == 0
    assert "Sports single-market links are covered" in sports["next_action"]
    assert "Phase 3Z-R2" not in sports["next_action"]


def test_link_coverage_connected_crypto_has_no_ingest_action(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXBTC-CONNECTED",
                "title": "yes Target Price: $100,000",
                "series_ticker": "KXBTC",
            },
        )
        parse_and_store_market_legs(session, refresh=True)
        insert_crypto_market_link(
            session,
            ticker="KXBTC-CONNECTED",
            symbol="BTC",
            confidence=Decimal("1.0"),
            reason="test link",
        )

        coverage = link_coverage_dashboard(session)

    crypto = _coverage_row(coverage, "crypto")
    assert crypto["status"] == "CONNECTED"
    assert crypto["coverage_percent"] == "100.0%"
    assert "No category coverage action is required" in crypto["next_action"]
    assert "ingest-crypto" not in crypto["next_action"]


def test_link_coverage_excludes_unsupported_sports_multileg_from_actionable_gap(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-UNSUPPORTED",
                "title": "yes Seattle,yes Arizona,yes Over 8.5 runs scored",
                "series_ticker": "KXMVESPORTSMULTIGAMEEXTENDED",
                "event_ticker": "KXMVESPORTSMULTIGAMEEXTENDED-S2026TEST",
            },
        )
        upsert_market(
            session,
            {
                "ticker": "KXBTC-ACTIONABLE",
                "title": "yes Target Price: $100,000",
                "series_ticker": "KXBTC",
            },
        )
        upsert_market(
            session,
            {
                "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-CRYPTO-COMPOSITE",
                "title": "yes Target Price: $59,554.39,no Target Price: $1,589.69",
                "series_ticker": "KXMVESPORTSMULTIGAMEEXTENDED",
                "event_ticker": "KXMVESPORTSMULTIGAMEEXTENDED-S2026CRYPTO",
            },
        )
        parse_and_store_market_legs(session, refresh=True)

        coverage = link_coverage_dashboard(session)

    sports = _coverage_row(coverage, "sports")
    crypto = _coverage_row(coverage, "crypto")
    assert sports["raw_unlinked_markets"] == 1
    assert sports["unsupported_multileg_markets"] == 1
    assert sports["unlinked_markets"] == 0
    assert sports["status"] == "UNSUPPORTED_MULTI_LEG"
    assert crypto["raw_unlinked_markets"] == 2
    assert crypto["unsupported_multileg_markets"] == 1
    assert crypto["unlinked_markets"] == 1
    assert coverage["bottleneck"]["category"] == "crypto"
    assert coverage["bottleneck"]["status"] == "UNLINKED"
    unlinked_example_tickers = {row["ticker"] for row in coverage["unlinked_examples"]}
    assert "KXMVESPORTSMULTIGAMEEXTENDED-UNSUPPORTED" not in unlinked_example_tickers
    assert "KXMVESPORTSMULTIGAMEEXTENDED-CRYPTO-COMPOSITE" not in (
        unlinked_example_tickers
    )
    assert "KXBTC-ACTIONABLE" in {
        row["ticker"] for row in coverage["unlinked_examples"]
    }
    assert any(
        card["label"] == "Unsupported Composites" and card["value"] == 2
        for card in coverage["summary_cards"]
    )


def test_market_leg_parser_extracts_target_price_legs(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXBTC-TARGET",
                "title": "yes Target Price: $62,382.15,no Target Price: $60,000",
                "series_ticker": "KXBTC",
            },
        )

        legs = parse_market_legs(market)

    assert len(legs) == 2
    assert {leg.side for leg in legs} == {"YES", "NO"}
    assert all(leg.category == "crypto" for leg in legs)
    assert all(leg.market_type == "TARGET_PRICE" for leg in legs)
    assert legs[0].threshold_value == "62382.15"
    assert legs[0].unit == "USD"


def test_market_legs_parse_persists_rows_and_refreshes(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXBTC-PARSE",
                "title": "yes Target Price: $100,000",
                "series_ticker": "KXBTC",
            },
        )

        first = parse_and_store_market_legs(session)
        skipped = parse_and_store_market_legs(session)
        refreshed = parse_and_store_market_legs(session, refresh=True)
        count = session.scalar(select(func.count(MarketLeg.id)))

    assert first.legs_inserted == 1
    assert skipped.legs_inserted == 0
    assert skipped.markets_skipped_existing == 1
    assert refreshed.legs_inserted == 1
    assert count == 1


def test_link_coverage_identifies_unlinked_and_partial_markets(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXBTC-LINKED",
                "title": "yes Target Price: $100,000",
                "series_ticker": "KXBTC",
            },
        )
        upsert_market(
            session,
            {
                "ticker": "KXETH-UNLINKED",
                "title": "yes Target Price: $4,000",
                "series_ticker": "KXETH",
            },
        )
        upsert_market(
            session,
            {
                "ticker": "KXMLB-PARTIAL",
                "title": "yes Cal Raleigh: 1+",
                "series_ticker": "KXMVESPORTSMULTIGAMEEXTENDED",
            },
        )
        parse_and_store_market_legs(session, refresh=True)
        insert_crypto_market_link(
            session,
            ticker="KXBTC-LINKED",
            symbol="BTC",
            confidence=Decimal("1.0"),
            reason="test link",
        )
        insert_sports_market_link(
            session,
            ticker="KXMLB-PARTIAL",
            league="MLB",
            game_key="MLB:market-derived:kxmlb-partial",
            market_type="PLAYER_PROP",
            link_confidence=Decimal("0.50"),
            link_reason="Market-derived fallback link.",
            matched_terms=["mlb", "market_derived"],
            raw_json={"source": "market-derived-fallback"},
        )

        coverage = link_coverage_dashboard(session)

    crypto = _coverage_row(coverage, "crypto")
    sports = _coverage_row(coverage, "sports")
    assert crypto["parsed_markets"] == 2
    assert crypto["linked_markets"] == 1
    assert crypto["unlinked_markets"] == 1
    assert sports["status"] == "PARTIAL"
    assert "Phase 3AH placeholder/roster watch" in sports["next_action"]
    assert coverage["partial_examples"][0]["ticker"] == "KXMLB-PARTIAL"
    assert "KXETH-UNLINKED" in {row["ticker"] for row in coverage["unlinked_examples"]}


def test_link_coverage_bottleneck_uses_current_gap_and_keeps_history(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXBTC-HISTORICAL-UNLINKED",
                "title": "yes Target Price: $60,000",
                "series_ticker": "KXBTC",
                "status": "active",
                "close_time": "2020-01-01T00:00:00+00:00",
            },
        )
        upsert_market(
            session,
            {
                "ticker": "KXETH-CURRENT-UNLINKED",
                "title": "yes Target Price: $2,000",
                "series_ticker": "KXETH",
                "status": "active",
                "close_time": "2100-01-01T00:00:00+00:00",
            },
        )
        parse_and_store_market_legs(session, refresh=True)

        coverage = link_coverage_dashboard(session)

    crypto = _coverage_row(coverage, "crypto")
    assert crypto["parsed_markets"] == 2
    assert crypto["unlinked_markets"] == 2
    assert crypto["current_parsed_markets"] == 1
    assert crypto["current_unlinked_markets"] == 1
    assert crypto["historical_unlinked_markets"] == 1
    assert coverage["bottleneck"]["status"] == "UNLINKED"
    assert "1 current parsed market(s)" in coverage["bottleneck"]["message"]
    assert "1 historical market(s)" in coverage["bottleneck"]["message"]
    assert coverage["unlinked_examples"][0]["ticker"] == "KXETH-CURRENT-UNLINKED"
    assert coverage["unlinked_examples"][0]["scope"] == "CURRENT"


def test_link_coverage_historical_only_gap_does_not_block_current_coverage(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXBTC-HISTORICAL-ONLY",
                "title": "yes Target Price: $60,000",
                "series_ticker": "KXBTC",
                "status": "active",
                "close_time": "2020-01-01T00:00:00+00:00",
            },
        )
        parse_and_store_market_legs(session, refresh=True)

        coverage = link_coverage_dashboard(session)

    crypto = _coverage_row(coverage, "crypto")
    assert crypto["unlinked_markets"] == 1
    assert crypto["current_unlinked_markets"] == 0
    assert crypto["historical_unlinked_markets"] == 1
    assert coverage["bottleneck"]["status"] == "CONNECTED"
    assert "historical market(s) remain" in coverage["bottleneck"]["message"]
    assert coverage["next_commands"] == []


def test_market_leg_parser_can_scope_active_writer_work_to_exact_tickers(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        for ticker in ("KXBTC-SCOPED", "KXTEMPNYCH-NOT-SCOPED"):
            upsert_market(
                session,
                {
                    "ticker": ticker,
                    "title": "yes Target Price: 75",
                    "series_ticker": ticker.split("-")[0],
                },
            )

        result = parse_and_store_market_legs(
            session,
            tickers=["KXBTC-SCOPED"],
        )
        parsed_tickers = set(session.scalars(select(MarketLeg.ticker).distinct()))

    assert result.markets_scanned == 1
    assert result.markets_with_legs == 1
    assert parsed_tickers == {"KXBTC-SCOPED"}


def test_link_coverage_partial_sports_bottleneck_uses_evidence_commands(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXMLB-PARTIAL-ONLY",
                "title": "yes Cal Raleigh: 1+",
                "series_ticker": "KXMVESPORTSMULTIGAMEEXTENDED",
            },
        )
        parse_and_store_market_legs(session, refresh=True)
        insert_sports_market_link(
            session,
            ticker="KXMLB-PARTIAL-ONLY",
            league="MLB",
            game_key="MLB:market-derived:kxmlb-partial-only",
            market_type="PLAYER_PROP",
            link_confidence=Decimal("0.50"),
            link_reason="Market-derived fallback link.",
            matched_terms=["mlb", "market_derived"],
            raw_json={"source": "market-derived-fallback"},
        )

        coverage = link_coverage_dashboard(session)

    assert coverage["bottleneck"]["category"] == "sports"
    assert coverage["bottleneck"]["status"] == "PARTIAL"
    assert coverage["next_commands"][:3] == [
        "kalshi-bot phase3ah-round-placeholder-resolution --output-dir reports/phase3ah_sports",
        "kalshi-bot phase3ah-sports-placeholder-watch --output-dir reports/phase3ah_sports",
        "kalshi-bot phase3z-r2-sports-provenance-repair --output-dir reports/phase3z_r2",
    ]


def test_link_coverage_report_and_ui_render(tmp_path, monkeypatch) -> None:
    session_factory = _session_factory(tmp_path)
    output = Path(tmp_path) / "link_coverage.md"
    monkeypatch.setattr(ui_routes, "LINK_COVERAGE_SNAPSHOT_PATH", tmp_path / "missing.json")

    def fake_shell_context(*, settings=None):
        context = build_default_shell_context(settings)
        context["paper_runtime"] = {
            "code": "ok",
            "tone": "positive",
            "label": "PAPER TEST",
            "icon": "PAP",
            "description": "Test paper health evidence.",
        }
        context["market_freshness"] = {
            "code": "ok",
            "tone": "positive",
            "label": "MARKET TEST",
            "icon": "MKT",
            "description": "Test market freshness evidence.",
        }
        context["snapshot_as_of"] = "2026-07-02T12:00:00+00:00"
        context["phase_3v"] = {
            "status": "NOT_READY",
            "label": "3V NOT READY TEST",
            "href": "/live-readiness",
        }
        return context

    monkeypatch.setattr(ui_routes, "load_shell_status_context", fake_shell_context)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXBTC-REPORT",
                "title": "yes Target Price: $100,000",
                "series_ticker": "KXBTC",
            },
        )
        parse_and_store_market_legs(session, refresh=True)
        path = generate_link_coverage_report(session, output_path=output)
        session.commit()

    text = path.read_text(encoding="utf-8")
    assert "# Market Link Coverage Report" in text
    assert "Category Coverage" in text

    client = TestClient(create_app(session_factory=session_factory, settings=Settings()))
    response = client.get("/links/coverage")
    assert response.status_code == 200
    assert "Market Link Coverage" in response.text
    assert "Category Coverage" in response.text
    assert "KXBTC-REPORT" in response.text
    assert "PAPER TEST" in response.text
    assert "MARKET TEST" in response.text
    assert "2026-07-02T12:00:00+00:00" in response.text
    assert "SRC UNINITIALIZED" not in response.text
    assert "3V UNKNOWN" not in response.text


def test_link_coverage_ui_uses_matching_generated_snapshot(tmp_path, monkeypatch) -> None:
    session_factory = _session_factory(tmp_path)
    snapshot = tmp_path / "link_coverage.json"
    snapshot.write_text(
        """{
  "generated_at": "2026-06-29T00:00:00+00:00",
  "mode": "PAPER ONLY",
  "summary_cards": [
    {"label": "Markets", "value": 1, "definition": "Stored markets"},
    {"label": "Parsed Legs", "value": 0, "definition": "Snapshot leg count"},
    {"label": "Linked Legs", "value": 999, "definition": "Snapshot-only value"}
  ],
  "table_counts": {},
  "category_rows": [],
  "bottleneck": {"status": "CONNECTED", "message": "Snapshot coverage.", "next_action": "None."},
  "link_counts": [],
  "count_definitions": [],
  "unlinked_examples": [],
  "partial_examples": [],
  "next_commands": []
}""",
        encoding="utf-8",
    )
    monkeypatch.setattr(ui_routes, "LINK_COVERAGE_SNAPSHOT_PATH", snapshot)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXBTC-SNAPSHOT-MATCH",
                "title": "yes Target Price: $100,000",
                "series_ticker": "KXBTC",
            },
        )
        session.commit()

    client = TestClient(create_app(session_factory=session_factory, settings=Settings()))
    response = client.get("/links/coverage")

    assert response.status_code == 200
    assert "generated_snapshot" in response.text
    assert "REPORT_STALE" in response.text
    assert "999" in response.text


def test_link_coverage_ui_prefers_snapshot_without_live_count_check(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = _session_factory(tmp_path)
    snapshot = tmp_path / "link_coverage.json"
    snapshot.write_text(
        """{
  "generated_at": "2026-06-29T00:00:00+00:00",
  "mode": "PAPER ONLY",
  "summary_cards": [
    {"label": "Markets", "value": 999999, "definition": "Snapshot market count"}
  ],
  "table_counts": {},
  "category_rows": [],
  "bottleneck": {"status": "CONNECTED", "message": "Snapshot coverage.", "next_action": "None."},
  "link_counts": [],
  "count_definitions": [],
  "unlinked_examples": [],
  "partial_examples": [],
  "next_commands": []
}""",
        encoding="utf-8",
    )
    monkeypatch.setattr(ui_routes, "LINK_COVERAGE_SNAPSHOT_PATH", snapshot)
    monkeypatch.setattr(
        ui_routes,
        "link_coverage_dashboard",
        lambda session: (_ for _ in ()).throw(AssertionError("live fallback called")),
    )

    client = TestClient(create_app(session_factory=session_factory, settings=Settings()))
    response = client.get("/links/coverage")

    assert response.status_code == 200
    assert "generated_snapshot" in response.text
    assert "999999" in response.text


def test_market_leg_cli_help_smoke() -> None:
    runner = CliRunner()
    for command in ("market-legs-parse", "link-coverage"):
        get_settings.cache_clear()
        result = runner.invoke(app, [command, "--help"])
        get_settings.cache_clear()
        assert result.exit_code == 0
        assert "Usage" in result.output


def _coverage_row(coverage: dict, category: str) -> dict:
    return next(row for row in coverage["category_rows"] if row["category"] == category)


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'link_coverage.db'}")
    return get_session_factory(engine)
