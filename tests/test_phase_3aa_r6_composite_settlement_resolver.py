from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import decode_json, upsert_market, upsert_settlement
from kalshi_predictor.data.schema import PaperOrder, Settlement
from kalshi_predictor.paper.models import ORDER_FILLED
from kalshi_predictor.phase3aa_r6 import (
    build_phase3aa_r6_composite_settlement_resolver,
    write_phase3aa_r6_composite_settlement_resolver_report,
)
from kalshi_predictor.utils.time import utc_now


def test_phase3aa_r6_dry_run_reports_ready_without_writing(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    ticker = "KXMVESPORTSMULTIGAMEEXTENDED-S2026LOCAL-ABC123"
    with session_factory() as session:
        _seed_composite(session, ticker)
        _seed_component_settlement(session, "KXCOMP-A", "yes")
        _seed_component_settlement(session, "KXCOMP-B", "no")

        payload = build_phase3aa_r6_composite_settlement_resolver(session)
        settlement = session.get(Settlement, ticker)

    row = payload["rows"][0]
    assert payload["summary"]["composite_rows_reviewed"] == 1
    assert payload["summary"]["ready_to_write_rows"] == 1
    assert payload["summary"]["settlements_written"] == 0
    assert payload["summary"]["dry_run"] is True
    assert row["classification"] == "READY"
    assert row["derived_result"] == "yes"
    assert row["derived_yes_settlement_value"] == "1"
    assert row["local_settlement_written"] is False
    assert settlement is None


def test_phase3aa_r6_write_creates_auditable_exact_local_settlement(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    ticker = "KXMVESPORTSMULTIGAMEEXTENDED-S2026LOCAL-ABC123"
    with session_factory() as session:
        _seed_composite(session, ticker)
        _seed_component_settlement(session, "KXCOMP-A", "yes")
        _seed_component_settlement(session, "KXCOMP-B", "no")

        payload = build_phase3aa_r6_composite_settlement_resolver(
            session,
            write_settlements=True,
        )
        settlement = session.get(Settlement, ticker)

    assert payload["summary"]["settlements_written"] == 1
    assert settlement is not None
    assert settlement.result == "yes"
    assert settlement.yes_settlement_value == "1"
    raw = decode_json(settlement.raw_json)
    assert raw["source"] == "phase3aa_r6_local_composite_settlement_resolver"
    assert raw["local_composite_settlement"]["same_composite_ticker_required"] is True
    assert raw["local_composite_settlement"]["component_exact_settlements_required"] is True
    assert raw["local_composite_settlement"]["paper_pnl_realized"] is False


def test_phase3aa_r6_derives_no_when_any_selected_component_side_loses(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    ticker = "KXMVECROSSCATEGORY-S2026LOCAL-DEF456"
    with session_factory() as session:
        _seed_composite(session, ticker)
        _seed_component_settlement(session, "KXCOMP-A", "yes")
        _seed_component_settlement(session, "KXCOMP-B", "yes")

        payload = build_phase3aa_r6_composite_settlement_resolver(
            session,
            write_settlements=True,
        )
        settlement = session.get(Settlement, ticker)

    assert payload["summary"]["derived_no_rows"] == 1
    assert settlement is not None
    assert settlement.result == "no"
    assert settlement.yes_settlement_value == "0"


def test_phase3aa_r6_blocks_missing_component_settlement(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    ticker = "KXMVESPORTSMULTIGAMEEXTENDED-S2026LOCAL-ABC123"
    with session_factory() as session:
        _seed_composite(session, ticker)
        _seed_component_settlement(session, "KXCOMP-A", "yes")

        payload = build_phase3aa_r6_composite_settlement_resolver(
            session,
            write_settlements=True,
        )
        settlement = session.get(Settlement, ticker)

    row = payload["rows"][0]
    assert payload["summary"]["ready_to_write_rows"] == 0
    assert payload["summary"]["settlements_written"] == 0
    assert payload["summary"]["missing_component_settlements"] == 1
    assert row["classification"] == "BLOCKED"
    assert row["blocked_reason"] == "MISSING_COMPONENT_SETTLEMENTS"
    assert settlement is None


def test_phase3aa_r6_refreshes_exact_components_before_composite_write(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    ticker = "KXMVESPORTSMULTIGAMEEXTENDED-S2026LOCAL-ABC123"
    client = _FakeExactMarketClient(
        {
            "KXCOMP-A": {
                "ticker": "KXCOMP-A",
                "status": "settled",
                "result": "yes",
                "settlement_ts": "2026-07-03T00:00:00Z",
            },
            "KXCOMP-B": {
                "ticker": "KXCOMP-B",
                "status": "settled",
                "result": "no",
                "settlement_ts": "2026-07-03T00:00:00Z",
            },
        }
    )
    with session_factory() as session:
        _seed_composite(session, ticker)

        payload = build_phase3aa_r6_composite_settlement_resolver(
            session,
            refresh_components=True,
            write_settlements=True,
            client=client,
        )
        component_a = session.get(Settlement, "KXCOMP-A")
        component_b = session.get(Settlement, "KXCOMP-B")
        composite = session.get(Settlement, ticker)

    assert client.requests == ["KXCOMP-A", "KXCOMP-B"]
    assert payload["summary"]["component_exact_settlement_rows_written"] == 2
    assert payload["summary"]["settlements_written"] == 1
    assert payload["rows"][0]["classification"] == "LOCAL_SETTLEMENT_WRITTEN"
    assert component_a is not None
    assert component_b is not None
    assert composite is not None
    assert composite.result == "yes"


def test_phase3aa_r6_writer_and_cli_help(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output_dir = Path(tmp_path) / "phase3aa_r6"
    with session_factory() as session:
        _seed_composite(session, "KXMVESPORTSMULTIGAMEEXTENDED-S2026LOCAL-ABC123")
        artifacts = write_phase3aa_r6_composite_settlement_resolver_report(
            session,
            output_dir=output_dir,
        )
    result = CliRunner().invoke(app, ["phase3aa-r6-composite-settlement-resolver", "--help"])

    assert artifacts.json_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.rows_path.exists()
    assert "Composite Settlement Resolver" in artifacts.markdown_path.read_text(
        encoding="utf-8"
    )
    assert result.exit_code == 0
    assert "phase3aa-r6-composite-settlement-resolver" in result.output
    assert "--refresh-components" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3aa_r6.db'}")
    return get_session_factory(engine)


def _seed_composite(session, ticker: str) -> None:
    session.add(_paper_order(ticker))
    upsert_market(
        session,
        {
            "ticker": ticker,
            "event_ticker": ticker.rsplit("-", 1)[0],
            "status": "active",
            "title": "yes Component A,no Component B",
            "close_time": utc_now() - timedelta(hours=2),
            "mve_selected_legs": [
                {
                    "event_ticker": "KXEVENT-A",
                    "market_ticker": "KXCOMP-A",
                    "side": "yes",
                },
                {
                    "event_ticker": "KXEVENT-B",
                    "market_ticker": "KXCOMP-B",
                    "side": "no",
                },
            ],
        },
    )
    session.flush()


def _seed_component_settlement(session, ticker: str, result: str) -> None:
    upsert_market(session, {"ticker": ticker, "status": "settled", "result": result})
    upsert_settlement(
        session,
        {
            "ticker": ticker,
            "status": "settled",
            "result": result,
            "settlement_ts": "2026-07-03T00:00:00Z",
        },
    )


def _paper_order(ticker: str) -> PaperOrder:
    return PaperOrder(
        ticker=ticker,
        forecast_id=None,
        created_at=utc_now(),
        model_name="ensemble_v2",
        side="BUY_YES",
        probability="0.55",
        market_price="0.50",
        limit_price="0.50",
        edge="0.05",
        quantity=1,
        status=ORDER_FILLED,
        reason="phase3aa-r6 test",
        raw_decision_json="{}",
    )


class _FakeExactMarketClient:
    def __init__(self, markets: dict[str, dict]) -> None:
        self.markets = markets
        self.requests: list[str] = []

    def get_market(self, ticker: str) -> dict:
        self.requests.append(ticker)
        return self.markets[ticker]
