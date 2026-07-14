import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.schema import (
    EconomicEvent,
    EconomicFeature,
    Forecast,
    Market,
    MarketLeg,
    MarketOpportunity,
    MarketSnapshot,
    NewsFeature,
    NewsItem,
    NewsMarketLink,
    PaperOrder,
    Settlement,
    SportsMarketLink,
)
from kalshi_predictor.phase3bb import (
    build_phase3bb_domain_readiness,
    build_phase3bb_general_candidate_routing,
    build_phase3bb_general_reclassification,
    build_phase3bb_general_source_availability,
    build_phase3bb_general_source_evidence,
    build_phase3bb_general_source_intake,
    write_phase3bb_domain_readiness_report,
    write_phase3bb_general_candidate_routing_report,
    write_phase3bb_general_reclassification_report,
    write_phase3bb_general_source_availability_report,
    write_phase3bb_general_source_evidence_report,
    write_phase3bb_general_source_intake_report,
    write_phase3bb_r3_composite_operator_preflight_report,
    write_phase3bb_r3_composite_preview_gate_report,
    write_phase3bb_r3_exact_sports_link_report,
    write_phase3bb_r3_safe_parser_reparse_report,
)
from kalshi_predictor.utils.time import utc_now


def test_phase3bb_reports_economic_news_and_general_readiness(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_market(session)
        _seed_market_leg(session, market.ticker, category="general")
        _seed_economic_evidence(session)
        news_item = _seed_news_item(session)
        _seed_news_link_and_feature(session, news_item_id=int(news_item.id), ticker=market.ticker)

        payload = build_phase3bb_domain_readiness(session)

    economic = _domain(payload, "economic")
    news = _domain(payload, "news")
    general = _domain(payload, "general")

    assert economic["status"] == "WAITING_FOR_COMPATIBLE_MARKETS"
    assert economic["counts"]["events"] == 1
    assert economic["counts"]["features"] == 1
    assert economic["counts"]["parsed_markets"] == 0
    assert news["status"] == "CONTEXT_READY_NO_ACTIVE_NEWS_MARKETS"
    assert news["counts"]["items"] == 1
    assert news["counts"]["links"] == 1
    assert general["status"] == "OBSERVED_ONLY_NO_SPECIALIZED_LINKER"
    assert general["counts"]["parsed_markets"] == 1
    assert general["taxonomy_counts"]["GENERAL_UNCLASSIFIED"] == 1
    assert payload["summary"]["domains_reviewed"] == 3
    assert "phase3bb-domain-readiness" in " ".join(payload["next_commands"])


def test_phase3bb_general_taxonomy_identifies_sports_leakage(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_market(
            session,
            ticker="KXMVESPORTSMULTIGAME-TEST",
            title="yes Brazil advances,yes Over 2.5 goals scored",
        )
        _seed_market_leg(
            session,
            market.ticker,
            category="general",
            raw_text="yes Brazil advances",
        )

        payload = build_phase3bb_domain_readiness(session)

    general = _domain(payload, "general")

    assert general["taxonomy_counts"]["SPORTS_OR_CROSS_CATEGORY_LEAKAGE"] == 1
    example = general["taxonomy_examples"]["SPORTS_OR_CROSS_CATEGORY_LEAKAGE"][0]
    assert example["ticker"] == "KXMVESPORTSMULTIGAME-TEST"


def test_phase3bb_taxonomy_keeps_kxmv_rows_out_of_company_news(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_market(
            session,
            ticker="KXMVESPORTSMULTIGAMEEXTENDED-SEC-TEST",
            title="yes Over 1.5 goals scored in 2nd sec",
        )
        _seed_market_leg(
            session,
            market.ticker,
            category="general",
            raw_text="yes Over 1.5 goals scored in 2nd sec",
        )

        payload = build_phase3bb_general_candidate_routing(session, limit_per_bucket=10)

    row = payload["route_rows"][0]
    assert row["taxonomy_bucket"] == "SPORTS_OR_CROSS_CATEGORY_LEAKAGE"
    assert row["route_domain"] == "sports"
    assert row["parser_recommendation"] == "sports_parser_reclassification_or_placeholder_watch"
    assert "COMPANY_NEWS_CANDIDATE" not in payload["summary"]["bucket_counts"]
    assert "Economic/news candidates are empty" in payload["recommended_next_action"]


def test_phase3bb_r2_routes_sports_name_war_without_geopolitical_false_positive(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_market(
            session,
            ticker="KXT20MATCH-26JUL011400SUSWAR-SUS",
            title="Warwickshire vs Sussex Winner?",
        )
        _seed_market_leg(
            session,
            market.ticker,
            category="general",
            raw_text="Warwickshire vs Sussex Winner?",
        )

        payload = build_phase3bb_general_candidate_routing(session, limit_per_bucket=10)

    row = payload["route_rows"][0]
    assert row["taxonomy_bucket"] == "SPORTS_OR_CROSS_CATEGORY_LEAKAGE"
    assert row["route_domain"] == "sports"
    assert row["parser_recommendation"] == "sports_parser_reclassification_or_placeholder_watch"
    assert "GEOPOLITICAL_NEWS_CANDIDATE" not in payload["summary"]["bucket_counts"]
    assert payload["summary"]["candidate_buckets"]["news"] == 0


def test_phase3bb_r2_routes_operational_and_commodity_general_candidates(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        avocado = _seed_market(
            session,
            ticker="KXAMSAVO-26JUL03-T1.20",
            title=(
                'Will the "This Week" weighted average advertised price for '
                "Avocados, Hass, each, for July 3, 2026 be above $1.20?"
            ),
        )
        _seed_market_leg(
            session,
            avocado.ticker,
            category="general",
            raw_text="weighted average advertised price for Avocados, Hass",
        )
        flights = _seed_market(
            session,
            ticker="KXUSFLYCAN-26JUL03-T3000",
            title=(
                "Will total cancellations within, into, or out of the United States "
                "for week ending July 3, 2026 be above 3000?"
            ),
        )
        _seed_market_leg(
            session,
            flights.ticker,
            category="general",
            raw_text="total cancellations within, into, or out of the United States",
        )
        capacity = _seed_market(
            session,
            ticker="KXUSDCCAPACITY-27MAR05-T55.0",
            title="Will Americas operational data center capacity for 2026 be above 55.0 GW?",
        )
        _seed_market_leg(
            session,
            capacity.ticker,
            category="general",
            raw_text="Americas operational data center capacity above 55.0 GW",
        )

        payload = build_phase3bb_general_candidate_routing(session, limit_per_bucket=10)

    rows = {row["ticker"]: row for row in payload["route_rows"]}
    diagnostics = {
        row["ticker"]: row for row in payload["general_signal_diagnostic_rows"]
    }
    assert payload["summary"]["candidate_buckets"]["operational_or_commodity"] == 3
    assert payload["summary"]["safe_link_upgrade_candidates"] == 0
    assert payload["summary"]["general_signal_diagnostics"]["diagnostic_rows"] == 3
    assert payload["summary"]["general_signal_diagnostics"]["safe_to_apply_rows"] == 0
    assert payload["summary"]["general_signal_diagnostics"]["safe_to_forecast_rows"] == 0
    assert payload["summary"]["general_signal_diagnostics"]["proposed_db_writes"] == 0
    assert rows["KXAMSAVO-26JUL03-T1.20"]["taxonomy_bucket"] == (
        "COMMODITY_PRICE_CANDIDATE"
    )
    assert rows["KXAMSAVO-26JUL03-T1.20"]["parser_recommendation"] == (
        "commodity_price_source_parser_diagnostic"
    )
    assert rows["KXUSFLYCAN-26JUL03-T3000"]["taxonomy_bucket"] == (
        "TRANSPORTATION_OPERATION_CANDIDATE"
    )
    assert rows["KXUSFLYCAN-26JUL03-T3000"]["parser_recommendation"] == (
        "transportation_operations_source_parser_diagnostic"
    )
    assert rows["KXUSDCCAPACITY-27MAR05-T55.0"]["taxonomy_bucket"] == (
        "INFRASTRUCTURE_CAPACITY_CANDIDATE"
    )
    assert rows["KXUSDCCAPACITY-27MAR05-T55.0"]["parser_recommendation"] == (
        "infrastructure_capacity_source_parser_diagnostic"
    )
    avocado_diag = diagnostics["KXAMSAVO-26JUL03-T1.20"]
    assert avocado_diag["diagnostic_name"] == "commodity_advertised_price_parser"
    assert avocado_diag["source_adapter_key"] == "commodity_advertised_price_source"
    assert avocado_diag["parsed_fields"]["source_subject"] == "Avocados, Hass"
    assert avocado_diag["parsed_fields"]["metric"] == "weighted_average_advertised_price"
    assert avocado_diag["parsed_fields"]["threshold"] == "1.20"
    assert avocado_diag["parsed_fields"]["threshold_unit"] == "USD_EACH"
    assert avocado_diag["parsed_fields"]["direction"] == "above"
    assert avocado_diag["parsed_fields"]["time_window"] == "July 3, 2026"
    assert avocado_diag["safe_to_forecast"] is False
    assert avocado_diag["proposed_db_writes"] == 0
    assert "source_adapter_missing" in avocado_diag["evidence_gaps"]
    flight_diag = diagnostics["KXUSFLYCAN-26JUL03-T3000"]
    assert flight_diag["diagnostic_name"] == "transportation_cancellation_count_parser"
    assert flight_diag["parsed_fields"]["metric"] == "total_flight_cancellations"
    assert flight_diag["parsed_fields"]["threshold"] == "3000"
    assert flight_diag["parsed_fields"]["threshold_unit"] == "CANCELLATIONS"
    assert flight_diag["parsed_fields"]["region"] == "United States"
    assert flight_diag["parsed_fields"]["time_window"] == "July 3, 2026"
    capacity_diag = diagnostics["KXUSDCCAPACITY-27MAR05-T55.0"]
    assert capacity_diag["diagnostic_name"] == "infrastructure_capacity_parser"
    assert capacity_diag["parsed_fields"]["metric"] == "operational_data_center_capacity"
    assert capacity_diag["parsed_fields"]["threshold"] == "55.0"
    assert capacity_diag["parsed_fields"]["threshold_unit"] == "GW"
    assert capacity_diag["parsed_fields"]["region"] == "Americas"
    assert capacity_diag["parsed_fields"]["time_window"] == "2026"
    assert all(row["safe_to_apply"] is False for row in payload["route_rows"])
    assert all(
        row["safe_to_apply"] is False
        for row in payload["general_signal_diagnostic_rows"]
    )


def test_phase3bb_r2_general_source_evidence_reports_exact_local_matches(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    evidence_dir = Path(tmp_path) / "source_evidence"
    evidence_dir.mkdir()
    _write_json(
        evidence_dir / "commodity_advertised_price_source.json",
        {
            "records": [
                {
                    "commodity": "Avocados",
                    "variety": "Hass",
                    "metric": "weighted_average_advertised_price",
                    "price_usd_each": "1.24",
                    "as_of_date": "July 3, 2026",
                    "source_name": "paper test source",
                    "source_url": "https://example.com/avocado",
                }
            ]
        },
    )
    _write_json(
        evidence_dir / "transportation_flight_cancellation_source.json",
        [
            {
                "region": "United States",
                "metric": "total_flight_cancellations",
                "period_start": "June 27, 2026",
                "period_end": "July 3, 2026",
                "cancellation_count": 3120,
                "source_name": "paper test source",
                "source_url": "https://example.com/flights",
            }
        ],
    )
    with session_factory() as session:
        avocado = _seed_market(
            session,
            ticker="KXAMSAVO-26JUL03-T1.20",
            title=(
                'Will the "This Week" weighted average advertised price for '
                "Avocados, Hass, each, for July 3, 2026 be above $1.20?"
            ),
        )
        _seed_market_leg(
            session,
            avocado.ticker,
            category="general",
            raw_text="weighted average advertised price for Avocados, Hass",
        )
        flights = _seed_market(
            session,
            ticker="KXUSFLYCAN-26JUL03-T3000",
            title=(
                "Will total cancellations within, into, or out of the United States "
                "for week ending July 3, 2026 be above 3000?"
            ),
        )
        _seed_market_leg(
            session,
            flights.ticker,
            category="general",
            raw_text="total cancellations within, into, or out of the United States",
        )
        capacity = _seed_market(
            session,
            ticker="KXUSDCCAPACITY-27MAR05-T55.0",
            title="Will Americas operational data center capacity for 2026 be above 55.0 GW?",
        )
        _seed_market_leg(
            session,
            capacity.ticker,
            category="general",
            raw_text="Americas operational data center capacity above 55.0 GW",
        )

        payload = build_phase3bb_general_source_evidence(
            session,
            evidence_dir=evidence_dir,
            limit_per_bucket=10,
        )

    rows = {row["ticker"]: row for row in payload["evidence_rows"]}
    assert payload["summary"]["diagnostic_rows"] == 3
    assert payload["summary"]["evidence_rows"] == 3
    assert payload["summary"]["exact_evidence_ready_rows"] == 2
    assert payload["summary"]["safe_to_link_rows"] == 0
    assert payload["summary"]["safe_to_forecast_rows"] == 0
    assert payload["summary"]["proposed_db_writes"] == 0
    assert payload["safety_gate"]["writes_links"] is False
    assert payload["safety_gate"]["writes_forecasts"] is False
    assert rows["KXAMSAVO-26JUL03-T1.20"]["evidence_status"] == (
        "EXACT_EVIDENCE_READY_FOR_REVIEW"
    )
    assert rows["KXUSFLYCAN-26JUL03-T3000"]["evidence_status"] == (
        "EXACT_EVIDENCE_READY_FOR_REVIEW"
    )
    assert rows["KXUSDCCAPACITY-27MAR05-T55.0"]["evidence_status"] == (
        "MISSING_SOURCE_EVIDENCE_FILE"
    )
    assert all(row["safe_to_forecast"] is False for row in payload["evidence_rows"])


def test_phase3bb_r2_general_source_evidence_tracks_unavailable_source_values(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    evidence_dir = Path(tmp_path) / "source_evidence"
    evidence_dir.mkdir()
    _write_json(
        evidence_dir / "commodity_advertised_price_source.json",
        {
            "records": [
                {
                    "commodity": "Avocados",
                    "variety": "Hass",
                    "metric": "weighted_average_advertised_price",
                    "price_usd_each": None,
                    "as_of_date": "July 3, 2026",
                    "source_name": "USDA AMS FVWRETAIL",
                    "source_url": "https://www.ams.usda.gov/mnreports/fvwretail.pdf",
                    "verification_status": "SOURCE_NOT_AVAILABLE",
                    "evidence_available": False,
                    "evidence_notes": "July 3 source value not published yet.",
                }
            ]
        },
    )
    _write_json(
        evidence_dir / "transportation_flight_cancellation_source.json",
        {"records": []},
    )
    _write_json(
        evidence_dir / "infrastructure_data_center_capacity_source.json",
        {"records": []},
    )
    with session_factory() as session:
        avocado = _seed_market(
            session,
            ticker="KXAMSAVO-26JUL03-T1.20",
            title=(
                'Will the "This Week" weighted average advertised price for '
                "Avocados, Hass, each, for July 3, 2026 be above $1.20?"
            ),
        )
        _seed_market_leg(
            session,
            avocado.ticker,
            category="general",
            raw_text="weighted average advertised price for Avocados, Hass",
        )

        payload = build_phase3bb_general_source_evidence(
            session,
            evidence_dir=evidence_dir,
            limit_per_bucket=10,
        )

    row = payload["evidence_rows"][0]
    assert row["evidence_status"] == "SOURCE_EVIDENCE_UNAVAILABLE"
    assert row["missing_evidence_fields"] == ["price_usd_each"]
    assert row["safe_to_link"] is False
    assert row["safe_to_forecast"] is False
    assert payload["summary"]["source_evidence_unavailable_rows"] == 1
    assert payload["summary"]["exact_evidence_ready_rows"] == 0
    assert "not published or directly available" in payload["recommended_next_action"]


def test_phase3bb_r2_general_source_availability_watches_pending_publications(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    evidence_dir = Path(tmp_path) / "source_evidence"
    evidence_dir.mkdir()
    _write_json(
        evidence_dir / "commodity_advertised_price_source.json",
        {
            "records": [
                {
                    "commodity": "Avocados",
                    "variety": "Hass",
                    "metric": "weighted_average_advertised_price",
                    "price_usd_each": None,
                    "as_of_date": "July 3, 2026",
                    "source_name": "USDA AMS FVWRETAIL",
                    "source_url": "https://www.ams.usda.gov/mnreports/fvwretail.pdf",
                    "verification_status": "SOURCE_NOT_AVAILABLE",
                    "evidence_available": False,
                }
            ]
        },
    )
    _write_json(
        evidence_dir / "transportation_flight_cancellation_source.json",
        {
            "records": [
                {
                    "region": "United States",
                    "metric": "total_flight_cancellations",
                    "period_start": "June 27, 2026",
                    "period_end": "July 3, 2026",
                    "cancellation_count": 1247,
                    "source_name": "Kalshi outcome page citing FlightAware",
                    "source_url": "https://example.com/flights",
                }
            ]
        },
    )
    _write_json(
        evidence_dir / "infrastructure_data_center_capacity_source.json",
        {
            "records": [
                {
                    "region": "Americas",
                    "metric": "operational_data_center_capacity",
                    "measurement_year": "2026",
                    "capacity_gw": None,
                    "source_name": "Cushman & Wakefield Americas Data Center Update",
                    "source_url": "https://example.com/data-centers",
                    "verification_status": "SOURCE_PENDING_PUBLICATION",
                    "evidence_available": False,
                }
            ]
        },
    )
    with session_factory() as session:
        avocado = _seed_market(
            session,
            ticker="KXAMSAVO-26JUL03-T1.20",
            title=(
                'Will the "This Week" weighted average advertised price for '
                "Avocados, Hass, each, for July 3, 2026 be above $1.20?"
            ),
        )
        _seed_market_leg(
            session,
            avocado.ticker,
            category="general",
            raw_text="weighted average advertised price for Avocados, Hass",
        )
        flights = _seed_market(
            session,
            ticker="KXUSFLYCAN-26JUL03-T3000",
            title=(
                "Will total cancellations within, into, or out of the United States "
                "for week ending July 3, 2026 be above 3000?"
            ),
        )
        _seed_market_leg(
            session,
            flights.ticker,
            category="general",
            raw_text="total cancellations within, into, or out of the United States",
        )
        capacity = _seed_market(
            session,
            ticker="KXUSDCCAPACITY-27MAR05-T55.0",
            title="Will Americas operational data center capacity for 2026 be above 55.0 GW?",
        )
        _seed_market_leg(
            session,
            capacity.ticker,
            category="general",
            raw_text="Americas operational data center capacity above 55.0 GW",
        )

        payload = build_phase3bb_general_source_availability(
            session,
            evidence_dir=evidence_dir,
            limit_per_bucket=10,
        )

    rows = {row["source_adapter_key"]: row for row in payload["availability_rows"]}
    assert payload["summary"]["source_value_available_rows"] == 1
    assert payload["summary"]["pending_source_publication_rows"] == 2
    assert payload["summary"]["safe_to_link_rows"] == 0
    assert payload["summary"]["safe_to_forecast_rows"] == 0
    assert payload["summary"]["proposed_db_writes"] == 0
    assert payload["safety_gate"]["writes_links"] is False
    assert rows["commodity_advertised_price_source"]["availability_status"] == (
        "PENDING_SOURCE_PUBLICATION"
    )
    assert rows["transportation_flight_cancellation_source"]["availability_status"] == (
        "SOURCE_VALUE_AVAILABLE_FOR_REVIEW"
    )
    assert rows["infrastructure_data_center_capacity_source"]["availability_status"] == (
        "PENDING_SOURCE_PUBLICATION"
    )
    assert all(
        row["remote_check"]["status"] == "NOT_REQUESTED"
        for row in payload["availability_rows"]
    )
    assert "Keep Phase 3BB-R2 active" in payload["recommended_next_action"]


def test_phase3bb_r2_general_source_intake_writes_canonical_evidence_files(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    evidence_dir = Path(tmp_path) / "source_evidence"
    input_file = Path(tmp_path) / "verified_sources.json"
    _write_json(
        input_file,
        [
            {
                "source_adapter_key": "commodity_advertised_price_source",
                "commodity": "Avocados",
                "variety": "Hass",
                "metric": "weighted_average_advertised_price",
                "price_usd_each": "1.24",
                "as_of_date": "July 3, 2026",
                "source_name": "paper test source",
                "source_url": "https://example.com/avocado",
            },
            {
                "source_adapter_key": "transportation_flight_cancellation_source",
                "region": "United States",
                "metric": "total_flight_cancellations",
                "period_start": "June 27, 2026",
                "period_end": "July 3, 2026",
                "cancellation_count": 3120,
                "source_name": "paper test source",
                "source_url": "https://example.com/flights",
            },
            {
                "source_adapter_key": "infrastructure_data_center_capacity_source",
                "region": "Americas",
                "metric": "operational_data_center_capacity",
                "measurement_year": "2026",
                "capacity_gw": "56.2",
                "source_name": "paper test source",
                "source_url": "https://example.com/data-centers",
            },
        ],
    )
    with session_factory() as session:
        avocado = _seed_market(
            session,
            ticker="KXAMSAVO-26JUL03-T1.20",
            title=(
                'Will the "This Week" weighted average advertised price for '
                "Avocados, Hass, each, for July 3, 2026 be above $1.20?"
            ),
        )
        _seed_market_leg(
            session,
            avocado.ticker,
            category="general",
            raw_text="weighted average advertised price for Avocados, Hass",
        )
        flights = _seed_market(
            session,
            ticker="KXUSFLYCAN-26JUL03-T3000",
            title=(
                "Will total cancellations within, into, or out of the United States "
                "for week ending July 3, 2026 be above 3000?"
            ),
        )
        _seed_market_leg(
            session,
            flights.ticker,
            category="general",
            raw_text="total cancellations within, into, or out of the United States",
        )
        capacity = _seed_market(
            session,
            ticker="KXUSDCCAPACITY-27MAR05-T55.0",
            title="Will Americas operational data center capacity for 2026 be above 55.0 GW?",
        )
        _seed_market_leg(
            session,
            capacity.ticker,
            category="general",
            raw_text="Americas operational data center capacity above 55.0 GW",
        )

        intake_payload = build_phase3bb_general_source_intake(
            session,
            input_file=input_file,
            evidence_dir=evidence_dir,
            limit_per_bucket=10,
            write_evidence_files=True,
        )
        evidence_payload = build_phase3bb_general_source_evidence(
            session,
            evidence_dir=evidence_dir,
            limit_per_bucket=10,
        )

    assert intake_payload["summary"]["valid_input_rows"] == 3
    assert intake_payload["summary"]["evidence_files_written"] == 3
    assert intake_payload["summary"]["link_writes"] is False
    assert intake_payload["summary"]["forecast_writes"] is False
    assert len(intake_payload["evidence_files_written"]) == 3
    assert evidence_payload["summary"]["exact_evidence_ready_rows"] == 3
    assert evidence_payload["summary"]["safe_to_forecast_rows"] == 0
    assert all(
        (evidence_dir / f"{adapter}.json").exists()
        for adapter in (
            "commodity_advertised_price_source",
            "transportation_flight_cancellation_source",
            "infrastructure_data_center_capacity_source",
        )
    )


def test_phase3bb_r2_general_source_intake_creates_requested_report_bundle(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    output_dir = Path(tmp_path) / "phase3bb_r2_sources"
    input_file = Path(tmp_path) / "verified_sources.json"
    _write_json(
        input_file,
        [
            {
                "source_adapter_key": "commodity_advertised_price_source",
                "commodity": "Avocados",
                "variety": "Hass",
                "metric": "weighted_average_advertised_price",
                "price_usd_each": "1.24",
                "as_of_date": "July 3, 2026",
                "source_name": "licensed commodity desk",
                "source_url": "https://example.com/avocado",
                "payload": {"restricted": "do not expose"},
            }
        ],
    )
    with session_factory() as session:
        commodity = _seed_market(
            session,
            ticker="KXAMSAVO-26JUL03-T1.20",
            title=(
                'Will the "This Week" weighted average advertised price for '
                "Avocados, Hass, each, for July 3, 2026 be above $1.20?"
            ),
        )
        _seed_market_leg(
            session,
            commodity.ticker,
            category="general",
            raw_text="weighted average advertised price for Avocados, Hass",
        )
        transportation = _seed_market(
            session,
            ticker="KXUSFLYCAN-26JUL03-T3000",
            title=(
                "Will total flight cancellations within, into, or out of the United States "
                "for week ending July 3, 2026 be above 3000?"
            ),
        )
        _seed_market_leg(
            session,
            transportation.ticker,
            category="general",
            raw_text="total flight cancellations within, into, or out of the United States",
        )
        infrastructure = _seed_market(
            session,
            ticker="KXUSDCCAPACITY-27MAR05-T55.0",
            title="Will Americas operational data center capacity for 2026 be above 55.0 GW?",
        )
        _seed_market_leg(
            session,
            infrastructure.ticker,
            category="general",
            raw_text="Americas operational data center capacity above 55.0 GW",
        )
        sports = _seed_market(
            session,
            ticker="KXMVESPORTSMULTIGAME-BUNDLE",
            title="yes Brazil advances",
        )
        _seed_market_leg(
            session,
            sports.ticker,
            category="general",
            raw_text="yes Brazil advances",
        )
        unclassified = _seed_market(
            session,
            ticker="KXGENERAL-UNCLASSIFIED",
            title="Will the local index be above the threshold?",
        )
        _seed_market_leg(
            session,
            unclassified.ticker,
            category="general",
            raw_text="yes local index threshold",
        )

        artifacts = write_phase3bb_general_source_intake_report(
            session,
            output_dir=output_dir,
            input_file=input_file,
            evidence_dir=Path(tmp_path) / "source_evidence",
        )
        payload = json.loads(artifacts.canonical_json_path.read_text(encoding="utf-8"))
        matrix_payload = json.loads(
            artifacts.source_readiness_matrix_path.read_text(encoding="utf-8")
        )
        taxonomy_payload = json.loads(artifacts.taxonomy_review_path.read_text(encoding="utf-8"))
        samples_payload = json.loads(
            artifacts.candidate_market_samples_path.read_text(encoding="utf-8")
        )
        next_actions_text = artifacts.next_actions_path.read_text(encoding="utf-8")

        assert session.query(Forecast).count() == 0
        assert session.query(MarketOpportunity).count() == 0
        assert session.query(PaperOrder).count() == 0
        assert session.query(EconomicFeature).count() == 0
        assert session.query(NewsFeature).count() == 0

    required_paths = [
        artifacts.canonical_json_path,
        artifacts.canonical_markdown_path,
        artifacts.taxonomy_review_path,
        artifacts.source_evidence_requirements_path,
        artifacts.source_readiness_matrix_path,
        artifacts.candidate_market_samples_path,
        artifacts.next_actions_path,
    ]
    assert all(path.exists() for path in required_paths)
    assert payload["safety_mode"] == "REPORT_ONLY_NO_WRITES"
    assert payload["git_commit"]
    assert payload["database_fingerprint"].startswith("sha256:")
    assert payload["data_watermark"]["general_market_leg_rows"] == 5
    assert payload["command_arguments"]["output_dir"] == str(output_dir)
    assert payload["summary"]["taxonomy_counts"] == {
        "COMMODITY_PRICE_CANDIDATE": 1,
        "GENERAL_UNCLASSIFIED": 1,
        "INFRASTRUCTURE_CAPACITY_CANDIDATE": 1,
        "SPORTS_OR_CROSS_CATEGORY_LEAKAGE": 1,
        "TRANSPORTATION_OPERATION_CANDIDATE": 1,
    }
    assert payload["safety_gate"]["writes_links"] is False
    assert payload["safety_gate"]["writes_features"] is False
    assert payload["safety_gate"]["writes_forecasts"] is False
    assert payload["safety_gate"]["writes_opportunities"] is False
    assert payload["safety_gate"]["places_paper_orders"] is False
    assert payload["safety_gate"]["settles_trades"] is False
    assert payload["input_rows"][0]["raw_row"]["payload"] == "[REDACTED_RESTRICTED_PAYLOAD]"

    taxonomy_rows = {row["taxonomy_label"]: row for row in taxonomy_payload["data"]}
    assert "USDA values are currently unavailable" in " ".join(
        taxonomy_rows["COMMODITY_PRICE_CANDIDATE"]["known_blockers"]
    )
    assert "FlightAware" in " ".join(
        taxonomy_rows["TRANSPORTATION_OPERATION_CANDIDATE"]["known_blockers"]
    )
    assert "Cushman" in " ".join(
        taxonomy_rows["INFRASTRUCTURE_CAPACITY_CANDIDATE"]["known_blockers"]
    )
    assert "NEEDS_PHASE_3AH_EVIDENCE" in taxonomy_rows[
        "SPORTS_OR_CROSS_CATEGORY_LEAKAGE"
    ]["leakage_diagnostic"]["reason_codes"]
    assert "needs human review" in taxonomy_rows["GENERAL_UNCLASSIFIED"][
        "unclassified_diagnostic"
    ]["reason_codes"]

    matrix = {row["source_name"]: row for row in matrix_payload["data"]}
    assert matrix["USDA"]["link_safe"] is False
    assert matrix["USDA"]["forecast_safe"] is False
    assert matrix["Cushman"]["link_safe"] is False
    assert matrix["Cushman"]["forecast_safe"] is False
    assert matrix["FlightAware"]["readiness_state"] == "READY_FOR_REVIEW"
    assert matrix["FlightAware"]["link_safe"] is False
    assert matrix["FlightAware"]["forecast_safe"] is False
    assert "COMMODITY_PRICE_CANDIDATE" in samples_payload["data"]
    assert "| 1 | code |" in next_actions_text
    assert "Link writes: blocked" in next_actions_text


def test_phase3bb_r2_routes_general_candidates_without_auto_apply(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        economic = _seed_market(
            session,
            ticker="KXGENERAL-CPI",
            title="Will CPI inflation be above forecast?",
        )
        _seed_market_leg(
            session,
            economic.ticker,
            category="general",
            raw_text="yes CPI inflation above forecast",
        )
        news = _seed_market(
            session,
            ticker="KXGENERAL-OIL",
            title="Will oil sanctions be announced?",
        )
        _seed_market_leg(
            session,
            news.ticker,
            category="general",
            raw_text="yes oil sanction announced",
        )
        sports = _seed_market(
            session,
            ticker="KXMVESPORTSMULTIGAME-ROUTE",
            title="yes Brazil advances",
        )
        _seed_market_leg(
            session,
            sports.ticker,
            category="general",
            raw_text="yes Brazil advances",
        )
        multileg = _seed_market(
            session,
            ticker="KXGENERAL-MULTI",
            title="yes option A,yes option B",
        )
        _seed_market_leg(
            session,
            multileg.ticker,
            category="general",
            raw_text="yes option A",
            leg_index=0,
        )
        _seed_market_leg(
            session,
            multileg.ticker,
            category="general",
            raw_text="yes option B",
            leg_index=1,
        )

        payload = build_phase3bb_general_candidate_routing(session, limit_per_bucket=10)

    rows = {row["ticker"]: row for row in payload["route_rows"]}
    assert payload["summary"]["safe_link_upgrade_candidates"] == 0
    assert payload["summary"]["bucket_counts"]["ECONOMIC_CANDIDATE"] == 1
    assert payload["summary"]["bucket_counts"]["GEOPOLITICAL_NEWS_CANDIDATE"] == 1
    assert payload["summary"]["bucket_counts"]["SPORTS_OR_CROSS_CATEGORY_LEAKAGE"] == 1
    assert payload["summary"]["bucket_counts"]["UNSUPPORTED_MULTI_LEG_GENERAL"] == 1
    assert payload["recommended_next_action"].startswith("Review the economic/news")
    assert payload["summary"]["route_domain_counts"]["economic"] == 1
    assert payload["summary"]["route_domain_counts"]["news"] == 1
    assert payload["summary"]["top_families_by_bucket"]["ECONOMIC_CANDIDATE"][0] == {
        "family_key": "KXGENERAL-EVENT",
        "count": 1,
    }
    assert rows["KXGENERAL-CPI"]["route_domain"] == "economic"
    assert rows["KXGENERAL-CPI"]["candidate_priority"] == "REVIEW_HIGH"
    assert rows["KXGENERAL-CPI"]["matched_terms"] == ["cpi", "inflation"]
    assert rows["KXGENERAL-CPI"]["parser_recommendation"] == (
        "economic_indicator_or_fed_parser_diagnostic"
    )
    assert rows["KXGENERAL-OIL"]["route_domain"] == "news"
    assert rows["KXGENERAL-OIL"]["matched_terms"] == ["oil", "sanction"]
    assert rows["KXMVESPORTSMULTIGAME-ROUTE"]["route_domain"] == "sports"
    assert rows["KXMVESPORTSMULTIGAME-ROUTE"]["candidate_priority"] == "LEAKAGE_REVIEW"
    assert rows["KXGENERAL-MULTI"]["route_domain"] == "unsupported"
    assert rows["KXGENERAL-MULTI"]["parser_recommendation"] == (
        "structured_multi_leg_component_parser_required"
    )
    assert all(row["safe_to_apply"] is False for row in payload["route_rows"])


def test_phase3bb_r3_groups_reclassification_and_manual_review_rows(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        sports = _seed_market(
            session,
            ticker="KXMVESPORTSMULTIGAMEEXTENDED-R3",
            title="yes Brazil wins by more than 1.5 goals",
        )
        _seed_market_leg(
            session,
            sports.ticker,
            category="general",
            raw_text="yes Brazil wins by more than 1.5 goals",
        )
        cross_category = _seed_market(
            session,
            ticker="KXMVECROSSCATEGORY-R3",
            title="yes sports leg and yes event leg",
        )
        _seed_market_leg(
            session,
            cross_category.ticker,
            category="general",
            raw_text="yes sports leg",
        )
        valorant_title = "Will T1 Academy win map 1 in the X-CAST vs. T1 Academy match?"
        valorant = _seed_market(
            session,
            ticker="KXVALORANTMAP-26JUL010700XCAT1A-1-T1A",
            title=valorant_title,
        )
        _seed_market_leg(
            session,
            valorant.ticker,
            category="general",
            raw_text=valorant_title,
        )
        cricket_title = "Will Ireland win the Ireland vs. England WODI match?"
        cricket = _seed_market(
            session,
            ticker="KXWODIMATCH-26JUL120545WINIRL-IRE",
            title=cricket_title,
        )
        _seed_market_leg(
            session,
            cricket.ticker,
            category="general",
            raw_text=cricket_title,
        )
        unclassified = _seed_market(
            session,
            ticker="KXGENERAL-R3",
            title="Will the local index be above the threshold?",
        )
        _seed_market_leg(
            session,
            unclassified.ticker,
            category="general",
            raw_text="yes local index threshold",
        )

        payload = build_phase3bb_general_reclassification(session, sample_limit=10)

    assert payload["summary"]["sports_cross_category_reclassification_candidates"] == 4
    assert payload["summary"]["manual_review_rows"] == 1
    assert payload["summary"]["safe_to_apply_rows"] == 0
    assert payload["summary"]["rows_safe_to_reparse"] == 3
    assert payload["summary"]["proposed_db_writes"] == 0
    assert payload["safety_gate"]["writes_market_legs"] is False
    assert payload["safety_gate"]["writes_links"] is False
    assert payload["safety_gate"]["safe_to_reparse"] is True
    categories = {
        row["ticker"]: row["proposed_category"]
        for row in payload["reclassification_candidates"]
    }
    assert categories["KXMVESPORTSMULTIGAMEEXTENDED-R3"] == "sports"
    assert categories["KXMVECROSSCATEGORY-R3"] == "cross_category"
    assert categories["KXVALORANTMAP-26JUL010700XCAT1A-1-T1A"] == "sports"
    assert categories["KXWODIMATCH-26JUL120545WINIRL-IRE"] == "sports"
    safe_preview = {
        row["ticker"]: row["parser_preview"]["safe_to_reparse"]
        for row in payload["reclassification_candidates"]
    }
    assert safe_preview["KXMVESPORTSMULTIGAMEEXTENDED-R3"] is True
    assert safe_preview["KXMVECROSSCATEGORY-R3"] is False
    assert safe_preview["KXVALORANTMAP-26JUL010700XCAT1A-1-T1A"] is True
    assert safe_preview["KXWODIMATCH-26JUL120545WINIRL-IRE"] is True
    reasons = {
        reason
        for row in payload["reclassification_candidates"]
        for reason in row["leakage_reasons"]
    }
    assert {"KXMV_PREFIX", "SPORTS_TERM_MATCH"} <= reasons
    assert payload["manual_review_rows"][0]["ticker"] == "KXGENERAL-R3"


def test_phase3bb_r3_safe_parser_reparse_refreshes_only_preview_safe_rows(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    output_dir = Path(tmp_path) / "phase3bb_r3"
    with session_factory() as session:
        safe_title = "Will T1 Academy win map 1 in the X-CAST vs. T1 Academy match?"
        safe_market = _seed_market(
            session,
            ticker="KXVALORANTMAP-26JUL010700XCAT1A-1-T1A",
            title=safe_title,
        )
        _seed_market_leg(
            session,
            safe_market.ticker,
            category="general",
            raw_text=safe_title,
        )
        blocked_market = _seed_market(
            session,
            ticker="KXVALORANTMAP-26JUL010700XCAT1A-2-T1A",
            title="Will T1 Academy win map 2 in the X-CAST vs. T1 Academy match?",
        )
        _seed_market_leg(
            session,
            blocked_market.ticker,
            category="general",
            raw_text="stale stored leg text",
        )

        artifacts = write_phase3bb_r3_safe_parser_reparse_report(
            session,
            output_dir=output_dir,
        )
        session.commit()

        safe_legs = (
            session.query(MarketLeg)
            .filter(MarketLeg.ticker == safe_market.ticker)
            .order_by(MarketLeg.leg_index)
            .all()
        )
        blocked_legs = (
            session.query(MarketLeg)
            .filter(MarketLeg.ticker == blocked_market.ticker)
            .order_by(MarketLeg.leg_index)
            .all()
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["status"] == "APPLIED"
    assert artifacts.rows_safe_to_reparse == 1
    assert artifacts.rows_reparsed == 1
    assert artifacts.rows_inserted == 1
    assert safe_legs[0].category == "sports"
    assert blocked_legs[0].category == "general"


def test_phase3bb_r3_exact_sports_link_preview_and_apply(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output_dir = Path(tmp_path) / "phase3bb_r3"
    with session_factory() as session:
        safe_market = _seed_market(
            session,
            ticker="KXCS2GAME-26JUN300400FORDON-DON",
            title="Will Donstu Esports win the Fortress vs. Donstu Esports CS2 match?",
        )
        _seed_market_leg(
            session,
            safe_market.ticker,
            category="sports",
            raw_text=safe_market.title,
            market_type="MONEYLINE",
        )
        blocked_market = _seed_market(
            session,
            ticker="KXCS2GAME-26JUN300400FORDON-MIXED",
            title="Will a mixed category market win?",
        )
        _seed_market_leg(
            session,
            blocked_market.ticker,
            category="sports",
            raw_text=blocked_market.title,
            market_type="MONEYLINE",
        )
        _seed_market_leg(
            session,
            blocked_market.ticker,
            category="general",
            raw_text="general side",
            leg_index=1,
        )

        preview = write_phase3bb_r3_exact_sports_link_report(
            session,
            output_dir=output_dir,
        )
        payload = json.loads(preview.json_path.read_text(encoding="utf-8"))

        applied = write_phase3bb_r3_exact_sports_link_report(
            session,
            output_dir=output_dir,
            apply=True,
        )
        session.commit()

        links = session.query(SportsMarketLink).order_by(SportsMarketLink.ticker).all()

    assert preview.rows_safe_to_link == 1
    assert payload["summary"]["blocked_rows"] == 1
    assert payload["blocked_rows"][0]["ticker"] == "KXCS2GAME-26JUN300400FORDON-MIXED"
    assert applied.links_created == 1
    assert [link.ticker for link in links] == [safe_market.ticker]
    assert links[0].game_key.startswith("SPORTS:kalshi-event-derived:")
    assert links[0].market_type == "MONEYLINE"


def test_phase3bb_r3_composite_preview_gate_splits_verified_component_rows(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    output_dir = Path(tmp_path) / "phase3bb_r3_composites"
    with session_factory() as session:
        true_market = _seed_market(
            session,
            ticker="KXMVESPORTSMULTIGAMEEXTENDED-TRUE-COMPOSITE",
            title="yes Team A,no Team B",
        )
        _seed_market_leg(
            session,
            true_market.ticker,
            category="sports",
            raw_text="yes Team A",
            market_type="MONEYLINE",
        )
        _seed_market_leg(
            session,
            true_market.ticker,
            category="sports",
            raw_text="no Team B",
            leg_index=1,
            market_type="MONEYLINE",
        )
        verified_market = _seed_market(
            session,
            ticker="KXMVESPORTSMULTIGAMEEXTENDED-VERIFIED-COMPONENTS",
            title="yes Component A,no Component B",
            raw_json={
                "source": "test",
                "mve_selected_legs": [
                    {"market_ticker": "KXCOMP-A", "side": "yes"},
                    {"market_ticker": "KXCOMP-B", "side": "no"},
                ],
            },
        )
        _seed_market_leg(
            session,
            verified_market.ticker,
            category="sports",
            raw_text="yes Component A",
            market_type="MONEYLINE",
        )
        _seed_market_leg(
            session,
            verified_market.ticker,
            category="sports",
            raw_text="no Component B",
            leg_index=1,
            market_type="MONEYLINE",
        )
        _seed_market(session, ticker="KXCOMP-A", title="Will component A resolve yes?")
        _seed_market(session, ticker="KXCOMP-B", title="Will component B resolve yes?")
        _seed_settlement(session, "KXCOMP-A", "yes")
        _seed_settlement(session, "KXCOMP-B", "no")

        artifacts = write_phase3bb_r3_composite_preview_gate_report(
            session,
            output_dir=output_dir,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    rows = {row["ticker"]: row for row in payload["rows"]}
    assert artifacts.rows_reviewed == 2
    assert artifacts.verified_component_evidence_rows == 1
    assert artifacts.true_composite_rows == 1
    assert payload["summary"]["safe_to_apply_rows"] == 0
    assert payload["safety_gate"]["runs_single_market_remediation"] is False
    assert payload["summary"]["category_counts"]["sports"]["markets"] == 2
    assert rows[true_market.ticker]["classification"] == "TRUE_COMPOSITE_NO_COMPONENT_MAPPING"
    assert rows[verified_market.ticker]["classification"] == "VERIFIED_COMPONENT_EVIDENCE"
    assert rows[verified_market.ticker]["safe_for_single_market_remediation"] is False
    assert artifacts.rows_path.exists()


def test_phase3bb_r3_composite_operator_preflight_requires_fresh_liquid_quote(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    output_dir = Path(tmp_path) / "phase3bb_r3_composites"
    with session_factory() as session:
        market = _seed_market(
            session,
            ticker="KXMVESPORTSMULTIGAMEEXTENDED-PREFLIGHT",
            title="yes Component A,no Component B",
            raw_json={
                "source": "test",
                "mve_selected_legs": [
                    {"market_ticker": "KXCOMP-A", "side": "yes"},
                    {"market_ticker": "KXCOMP-B", "side": "no"},
                ],
            },
        )
        _seed_market_leg(
            session,
            market.ticker,
            category="sports",
            raw_text="yes Component A",
            market_type="MONEYLINE",
        )
        _seed_market_leg(
            session,
            market.ticker,
            category="sports",
            raw_text="no Component B",
            leg_index=1,
            market_type="MONEYLINE",
        )
        _seed_market(session, ticker="KXCOMP-A", title="Will component A resolve yes?")
        _seed_market(session, ticker="KXCOMP-B", title="Will component B resolve yes?")
        _seed_settlement(session, "KXCOMP-A", "yes")
        _seed_settlement(session, "KXCOMP-B", "no")
        _seed_snapshot(session, market.ticker)

        preview = write_phase3bb_r3_composite_preview_gate_report(
            session,
            output_dir=output_dir,
        )
        preflight = write_phase3bb_r3_composite_operator_preflight_report(
            session,
            output_dir=output_dir,
            preview_path=preview.json_path,
        )

    payload = json.loads(preflight.json_path.read_text(encoding="utf-8"))
    assert preflight.paper_composite_review_ready_rows == 1
    assert preflight.blocked_rows == 0
    assert payload["summary"]["safe_to_apply_rows"] == 0
    assert payload["safety_gate"]["creates_paper_trades"] is False
    assert payload["rows"][0]["paper_composite_review_ready"] is True


def test_phase3bb_r3_reports_no_work_when_general_rows_are_clear(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        payload = build_phase3bb_general_reclassification(session, sample_limit=10)

    assert payload["summary"]["general_markets_reviewed"] == 0
    assert payload["summary"]["sports_cross_category_reclassification_candidates"] == 0
    assert payload["summary"]["manual_review_rows"] == 0
    assert "No general reclassification work remains" in payload["recommended_next_action"]


def test_phase3bb_writer_outputs_artifacts(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_market(session)
        _seed_market_leg(session, market.ticker, category="general")

        artifacts = write_phase3bb_domain_readiness_report(
            session,
            output_dir=Path(tmp_path) / "phase3bb",
        )

    assert artifacts.json_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.rows_path.exists()
    assert "Phase 3BB Domain Readiness" in artifacts.markdown_path.read_text(
        encoding="utf-8"
    )

    with session_factory() as session:
        r2_artifacts = write_phase3bb_general_candidate_routing_report(
            session,
            output_dir=Path(tmp_path) / "phase3bb_r2",
        )
    assert r2_artifacts.json_path.exists()
    assert r2_artifacts.markdown_path.exists()
    assert r2_artifacts.rows_path.exists()
    assert r2_artifacts.diagnostics_path.exists()
    assert "Phase 3BB-R2 General Candidate Routing" in r2_artifacts.markdown_path.read_text(
        encoding="utf-8"
    )
    assert "General Signal Parser Diagnostics" in r2_artifacts.markdown_path.read_text(
        encoding="utf-8"
    )

    with session_factory() as session:
        intake_artifacts = write_phase3bb_general_source_intake_report(
            session,
            output_dir=Path(tmp_path) / "phase3bb_r2_sources",
            evidence_dir=Path(tmp_path) / "source_evidence",
        )
    assert intake_artifacts.json_path.exists()
    assert intake_artifacts.markdown_path.exists()
    assert intake_artifacts.template_json_path.exists()
    assert intake_artifacts.template_csv_path.exists()
    assert intake_artifacts.manifest_path.exists()
    assert "general_source_intake.json" in intake_artifacts.manifest_path.read_text(
        encoding="utf-8"
    )
    assert "Phase 3BB-R2 General Source Intake" in (
        intake_artifacts.markdown_path.read_text(encoding="utf-8")
    )

    with session_factory() as session:
        source_artifacts = write_phase3bb_general_source_evidence_report(
            session,
            output_dir=Path(tmp_path) / "phase3bb_r2_sources",
            evidence_dir=Path(tmp_path) / "source_evidence",
        )
    assert source_artifacts.json_path.exists()
    assert source_artifacts.markdown_path.exists()
    assert source_artifacts.evidence_rows_path.exists()
    assert source_artifacts.templates_path.exists()
    assert "Phase 3BB-R2 General Source Evidence" in (
        source_artifacts.markdown_path.read_text(encoding="utf-8")
    )

    with session_factory() as session:
        availability_artifacts = write_phase3bb_general_source_availability_report(
            session,
            output_dir=Path(tmp_path) / "phase3bb_r2_sources",
            evidence_dir=Path(tmp_path) / "source_evidence",
        )
    assert availability_artifacts.json_path.exists()
    assert availability_artifacts.markdown_path.exists()
    assert availability_artifacts.availability_rows_path.exists()
    assert "Phase 3BB-R2 General Source Availability" in (
        availability_artifacts.markdown_path.read_text(encoding="utf-8")
    )

    with session_factory() as session:
        r3_artifacts = write_phase3bb_general_reclassification_report(
            session,
            output_dir=Path(tmp_path) / "phase3bb_r3",
        )
    assert r3_artifacts.json_path.exists()
    assert r3_artifacts.markdown_path.exists()
    assert r3_artifacts.candidates_path.exists()
    assert r3_artifacts.manual_review_path.exists()
    assert "Phase 3BB-R3 General Reclassification" in r3_artifacts.markdown_path.read_text(
        encoding="utf-8"
    )


def test_phase3bb_domain_readiness_cli_help() -> None:
    result = CliRunner().invoke(app, ["phase3bb-domain-readiness", "--help"])

    assert result.exit_code == 0
    assert "Usage" in result.output

    r2_result = CliRunner().invoke(app, ["phase3bb-r2-general-candidate-routing", "--help"])
    assert r2_result.exit_code == 0
    assert "Usage" in r2_result.output

    intake_result = CliRunner().invoke(app, ["phase3bb-r2-general-source-intake", "--help"])
    assert intake_result.exit_code == 0
    assert "Usage" in intake_result.output

    source_result = CliRunner().invoke(app, ["phase3bb-r2-general-source-evidence", "--help"])
    assert source_result.exit_code == 0
    assert "Usage" in source_result.output

    availability_result = CliRunner().invoke(
        app,
        ["phase3bb-r2-general-source-availability", "--help"],
    )
    assert availability_result.exit_code == 0
    assert "Usage" in availability_result.output

    r3_result = CliRunner().invoke(app, ["phase3bb-r3-general-reclassification", "--help"])
    assert r3_result.exit_code == 0
    assert "Usage" in r3_result.output

    composite_result = CliRunner().invoke(app, ["phase3bb-r3-composite-preview-gate", "--help"])
    assert composite_result.exit_code == 0
    assert "Usage" in composite_result.output

    composite_preflight_result = CliRunner().invoke(
        app,
        ["phase3bb-r3-composite-operator-preflight", "--help"],
    )
    assert composite_preflight_result.exit_code == 0
    assert "Usage" in composite_preflight_result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3bb.db'}")
    return get_session_factory(engine)


def _seed_market(
    session,
    *,
    ticker: str = "KXGENERAL-TEST",
    title: str = "Will a general market resolve yes?",
    raw_json: dict | None = None,
) -> Market:
    now = utc_now()
    market = Market(
        ticker=ticker,
        event_ticker="KXGENERAL-EVENT",
        series_ticker="KXGENERAL",
        title=title,
        subtitle=None,
        market_type="binary",
        status="open",
        result=None,
        open_time=now,
        close_time=now,
        expected_expiration_time=now,
        expiration_time=None,
        settlement_ts=None,
        settlement_value_dollars=None,
        volume_fp="100",
        open_interest_fp="50",
        liquidity_dollars="1000",
        rules_primary="General test market.",
        rules_secondary=None,
        raw_json=json.dumps(raw_json or {"source": "test"}),
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(market)
    session.flush()
    return market


def _seed_market_leg(
    session,
    ticker: str,
    *,
    category: str,
    raw_text: str = "yes general market",
    leg_index: int = 0,
    market_type: str = "BINARY",
) -> None:
    session.add(
        MarketLeg(
            ticker=ticker,
            leg_index=leg_index,
            parsed_at=utc_now(),
            side="yes",
            category=category,
            market_type=market_type,
            entity_name="general market",
            operator="eq",
            threshold_value=None,
            unit=None,
            confidence="0.90",
            raw_text=raw_text,
            reason="test",
            raw_json=json.dumps({"source": "test"}),
        )
    )
    session.flush()


def _seed_settlement(session, ticker: str, result: str) -> None:
    now = utc_now()
    session.add(
        Settlement(
            ticker=ticker,
            settled_at=now,
            result=result,
            yes_settlement_value="1" if result == "yes" else "0",
            raw_json=json.dumps({"source": "test", "result": result}),
            updated_at=now,
        )
    )
    session.flush()


def _seed_snapshot(session, ticker: str) -> None:
    now = utc_now()
    session.add(
        MarketSnapshot(
            ticker=ticker,
            captured_at=now,
            status="open",
            yes_bid_dollars="0.45",
            yes_ask_dollars="0.55",
            no_bid_dollars="0.45",
            no_ask_dollars="0.55",
            best_yes_bid="0.45",
            best_yes_ask="0.55",
            best_no_bid="0.45",
            best_no_ask="0.55",
            spread="0.10",
            last_price_dollars="0.50",
            volume_fp="100",
            volume_24h_fp="50",
            open_interest_fp="100",
            raw_market_json=json.dumps({"source": "test"}),
            raw_orderbook_json=json.dumps({"source": "test"}),
        )
    )
    session.flush()


def _seed_economic_evidence(session) -> None:
    now = utc_now()
    session.add(
        EconomicEvent(
            event_key="cpi",
            source="test",
            event_time=now,
            category="inflation",
            title="CPI release",
            actual_value="3.1",
            forecast_value="3.0",
            previous_value="3.2",
            raw_json=json.dumps({"source": "test"}),
            created_at=now,
        )
    )
    session.add(
        EconomicFeature(
            event_key="cpi",
            generated_at=now,
            category="inflation",
            surprise_score="0.1",
            direction="UP",
            confidence_score="70",
            raw_json=json.dumps({"source": "test"}),
            created_at=now,
        )
    )
    session.flush()


def _seed_news_item(session) -> NewsItem:
    now = utc_now()
    item = NewsItem(
        source="test",
        source_url="https://example.com/news",
        published_at=now,
        ingested_at=now,
        title="Federal Reserve news item",
        summary="Fed policy update.",
        body="Fed policy update.",
        author=None,
        category="economic",
        entities_json=json.dumps(["Fed"]),
        sentiment_score="0.1",
        importance_score="0.8",
        freshness_score="0.9",
        raw_json=json.dumps({"source": "test"}),
    )
    session.add(item)
    session.flush()
    return item


def _seed_news_link_and_feature(session, *, news_item_id: int, ticker: str) -> None:
    now = utc_now()
    session.add(
        NewsMarketLink(
            created_at=now,
            news_item_id=news_item_id,
            ticker=ticker,
            link_confidence="0.8",
            link_reason="test link",
            matched_terms_json=json.dumps(["Fed"]),
            raw_json=json.dumps({"source": "test"}),
        )
    )
    session.add(
        NewsFeature(
            created_at=now,
            ticker=ticker,
            feature_window_minutes=360,
            news_count=1,
            high_importance_count=1,
            avg_sentiment="0.1",
            max_importance="0.8",
            freshness_score="0.9",
            category_counts_json=json.dumps({"economic": 1}),
            entity_counts_json=json.dumps({"Fed": 1}),
            linked_news_json=json.dumps([{"news_item_id": news_item_id}]),
            raw_json=json.dumps({"source": "test"}),
        )
    )
    session.flush()


def _domain(payload: dict, domain: str) -> dict:
    return next(row for row in payload["domain_rows"] if row["domain"] == domain)


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
