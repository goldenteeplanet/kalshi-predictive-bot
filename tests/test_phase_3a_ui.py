from pathlib import Path

from fastapi.testclient import TestClient

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot
from kalshi_predictor.data.schema import MarketLeg
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.ui.routes import create_router
from kalshi_predictor.utils.time import utc_now


def test_ui_route_smoke(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert "Professional cockpit" in response.text
    assert 'id="today-title">Today</h1>' in response.text
    assert "DEMO ONLY" in response.text


def test_dashboard_renders_with_no_opportunities(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.get("/opportunities")

    assert response.status_code == 200
    assert "No current candidate clears the visible ranking and liquidity filters" in response.text


def test_opportunity_detail_handles_missing_ticker(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.get("/opportunities/MISSING")

    assert response.status_code == 404


def test_fixed_routes_are_registered_before_parameterized_routes(tmp_path) -> None:
    router = create_router(session_factory=_session_factory(tmp_path), settings=Settings())
    route_paths = [route.path for route in router.routes]

    assert route_paths.index("/opportunities") < route_paths.index(
        "/opportunities/{ticker}"
    )
    assert route_paths.index("/opportunities/best-payouts") < route_paths.index(
        "/opportunities/{ticker}"
    )
    assert route_paths.index("/reports/best-payouts") < route_paths.index(
        "/reports/{report_name}"
    )
    assert route_paths.index("/research") < route_paths.index("/research/opportunity/{ticker}")
    assert route_paths.index("/signals") < route_paths.index("/signals/{signal_name}")


def test_opportunity_routes_render_expected_pages(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_opportunity(session_factory)
    client = _client_from_factory(session_factory, Settings())

    dashboard = client.get("/opportunities")
    best_payouts = client.get("/opportunities/best-payouts")
    detail = client.get("/opportunities/UI-TEST")

    assert dashboard.status_code == 200
    assert "Best Opportunities Right Now" in dashboard.text
    assert "Fast bounded view for page navigation" in dashboard.text
    assert best_payouts.status_code == 200
    assert "Best Payouts" in best_payouts.text
    assert "Opportunity not found" not in best_payouts.text
    assert detail.status_code == 200
    assert "UI-TEST" in detail.text
    assert "Hypothetical Payout Calculator" in detail.text
    assert "Profit if correct" in detail.text


def test_execution_review_is_read_only_and_uses_shell_context(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_opportunity(session_factory)
    client = _client_from_factory(
        session_factory,
        Settings(execution_enabled=False, execution_dry_run=True),
    )

    response = client.get("/execution/review/UI-TEST")

    assert response.status_code == 200
    assert "EXECUTION_ENABLED=false" in response.text
    assert "Read-only Safety Boundary" in response.text
    assert "Copy exact ticker" in response.text
    assert "Confirm demo execution" not in response.text
    assert "Demo dry-run" not in response.text
    assert "UNK Unknown" not in response.text


def test_execution_review_renders_unsupported_multileg_as_structure(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_multileg_opportunity(session_factory)
    client = _client_from_factory(
        session_factory,
        Settings(execution_enabled=False, execution_dry_run=True),
    )

    response = client.get("/execution/review/UI-MULTI")

    assert response.status_code == 200
    assert (
        "Sports multi-leg: Mexico wins by more than 1.5 goals; "
        "Morocco wins by more than 1.5 goals"
    ) in response.text
    assert "Unsupported multi-leg market (2 options)" in response.text
    assert "Kalshi lookup title" in response.text
    assert "Copy search text" in response.text
    assert "Open combo event API" in response.text
    assert "Copy exact ticker" in response.text
    assert "Copy component tickers" in response.text
    assert "Copy exact ticker; open Kalshi search" not in response.text
    assert (
        "If the exact API returns 404, this local ticker is not currently exposed"
        in response.text
    )
    assert "Direct Kalshi web-search links are disabled" in response.text
    assert (
        "https://external-api.kalshi.com/trade-api/v2/events/KXMVESPORTSMULTIGAMEEXTENDED"
        in response.text
    )
    assert 'href="https://kalshi.com/search"' not in response.text
    assert 'data-copy-text="UI-MULTI"' in response.text
    assert "https://kalshi.com/search?query=UI-MULTI" not in response.text
    expected_headline = (
        "<h1>Mexico wins by more than 1.5 goals "
        "and Morocco wins by more than 1.5 goals</h1>"
    )
    assert expected_headline in response.text
    assert "Contract YES Side" in response.text
    assert "YES wins only if all 2 selected components are true" in response.text
    assert "YES on Mexico wins by more than 1.5 goals" in response.text
    assert "YES on Morocco wins by more than 1.5 goals" in response.text
    assert "Contract NO Side" in response.text
    assert "NO wins if at least one selected component is false." in response.text
    assert "Market Structure" in response.text
    assert "Readable option" in response.text
    assert "Mexico wins by more than 1.5 goals" in response.text
    assert "Morocco wins by more than 1.5 goals" in response.text
    assert "KXMEXICO-WINBY15" in response.text
    assert "KXMOROCCO-WINBY15" in response.text
    assert "Copy component ticker" in response.text
    assert "VERIFIED_COMPONENT" in response.text
    assert "Copy exact ticker" in response.text
    assert "Check exact API status" in response.text
    assert "Confirm demo execution" not in response.text


def test_opportunity_list_shows_multileg_kalshi_lookup_text(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_multileg_opportunity(session_factory)
    client = _client_from_factory(session_factory, Settings())

    response = client.get("/opportunities")

    assert response.status_code == 200
    assert "Blocked Research" in response.text
    assert "Rows kept out of the ready list" in response.text
    assert "UI-MULTI" in response.text
    assert "Kalshi lookup title" not in response.text
    assert "Copy search text" not in response.text
    assert "Open combo event API" not in response.text
    assert "Copy exact ticker; open Kalshi search" not in response.text
    assert "Direct Kalshi web-search links are disabled" not in response.text
    assert 'href="https://kalshi.com/search"' not in response.text
    assert "https://kalshi.com/search?query=UI-MULTI" not in response.text
    assert "2 selected component legs" not in response.text


def test_demo_execute_route_performs_dry_run_when_execution_dry_run_true(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    session_factory = _session_factory(tmp_path)
    _seed_opportunity(session_factory)
    client = _client_from_factory(
        session_factory,
        Settings(learning_mode=False, execution_enabled=True, execution_dry_run=True),
    )

    response = client.post("/demo-execute/UI-TEST?confirmation=DEMO%20ONLY")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "DRY_RUN"
    assert payload["dry_run"] is True
    assert "no demo order was placed" in payload["message"]


def _client(tmp_path) -> TestClient:
    return _client_from_factory(_session_factory(tmp_path), Settings())


def _client_from_factory(session_factory, settings: Settings) -> TestClient:
    return TestClient(create_app(session_factory=session_factory, settings=settings))


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'ui.db'}")
    return get_session_factory(engine)


def _seed_opportunity(session_factory) -> None:
    with session_factory() as session:
        captured_at = utc_now()
        insert_market_snapshot(
            session,
            {
                "ticker": "UI-TEST",
                "status": "open",
                "title": "Will this UI market resolve yes?",
                "rules_primary": "This is a local UI test market.",
                "yes_bid_dollars": "0.40",
                "yes_ask_dollars": "0.50",
                "liquidity_dollars": "100",
            },
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.40", "10"]],
                    "no_dollars": [["0.50", "10"]],
                }
            },
            captured_at,
        )
        insert_market_ranking(
            session,
            {
                "ticker": "UI-TEST",
                "ranked_at": captured_at,
                "title": "Will this UI market resolve yes?",
                "status": "open",
                "forecast_model": "market_implied_v1",
                "forecast_probability": "0.60",
                "best_side": "BUY_YES",
                "best_price": "0.50",
                "estimated_edge": "0.10",
                "liquidity_score": "80",
                "spread_score": "90",
                "time_score": "70",
                "model_confidence_score": "60",
                "opportunity_score": "75",
                "spread": "0.10",
                "time_to_close_minutes": "120",
                "reason": "Seeded UI test opportunity.",
            },
        )
        session.commit()


def _seed_multileg_opportunity(session_factory) -> None:
    with session_factory() as session:
        captured_at = utc_now()
        title = (
            "yes Mexico wins by more than 1.5 goals,yes Morocco wins by more "
            "than 1.5 goals"
        )
        insert_market_snapshot(
            session,
            {
                "ticker": "UI-MULTI",
                "event_ticker": "KXMVESPORTSMULTIGAMEEXTENDED",
                "series_ticker": "KXMVESPORTS",
                "market_type": "multi_binary",
                "status": "open",
                "title": title,
                "rules_primary": "This is a local UI multi-leg market.",
                "yes_bid_dollars": "0.01",
                "yes_ask_dollars": "0.02",
                "liquidity_dollars": "0",
                "custom_strike": {
                    "Associated Events": "KXMEXICO,KXMOROCCO",
                    "Associated Markets": "KXMEXICO-WINBY15,KXMOROCCO-WINBY15",
                    "Associated Market Sides": "yes,yes",
                },
            },
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.01", "1"]],
                    "no_dollars": [["0.98", "1"]],
                }
            },
            captured_at,
        )
        insert_market_ranking(
            session,
            {
                "ticker": "UI-MULTI",
                "ranked_at": captured_at,
                "title": title,
                "status": "open",
                "series_ticker": "KXMVESPORTS",
                "event_ticker": "KXMVESPORTSMULTIGAMEEXTENDED",
                "forecast_model": "ensemble_v2",
                "forecast_probability": "0.50",
                "best_side": "BUY_YES",
                "best_price": "0.017",
                "estimated_edge": "0.483",
                "liquidity_score": "0",
                "spread_score": "0",
                "time_score": "70",
                "model_confidence_score": "57.2",
                "opportunity_score": "57.2",
                "spread": "1.00",
                "time_to_close_minutes": "21312",
                "reason": "Seeded UI multi-leg test opportunity.",
            },
        )
        session.add_all(
            [
                MarketLeg(
                    ticker="UI-MULTI",
                    leg_index=0,
                    parsed_at=captured_at,
                    side="yes",
                    category="sports",
                    market_type="TEAM_PROP",
                    entity_name="Mexico",
                    operator="MORE_THAN",
                    threshold_value="1.5",
                    unit="goals",
                    confidence="high",
                    raw_text="yes Mexico wins by more than 1.5 goals",
                    reason="Seeded parsed team leg.",
                    raw_json="{}",
                ),
                MarketLeg(
                    ticker="UI-MULTI",
                    leg_index=1,
                    parsed_at=captured_at,
                    side="yes",
                    category="sports",
                    market_type="TEAM_PROP",
                    entity_name="Morocco",
                    operator="MORE_THAN",
                    threshold_value="1.5",
                    unit="goals",
                    confidence="high",
                    raw_text="yes Morocco wins by more than 1.5 goals",
                    reason="Seeded parsed team leg.",
                    raw_json="{}",
                ),
            ]
        )
        session.commit()
