import csv
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market, upsert_settlement
from kalshi_predictor.data.schema import MarketLeg, PaperOrder, PaperPnl
from kalshi_predictor.paper.models import BUY_YES, ORDER_FILLED
from kalshi_predictor.phase3am import (
    build_phase3ay_due_settlement_diagnostic,
    build_phase3ay_settle_due_paper,
    write_phase3am_gap_burndown_report,
)
from kalshi_predictor.phase3bb import (
    write_phase3bb_apply_group_source_review,
    write_phase3bb_group_source_review,
)
from kalshi_predictor.utils.time import utc_now


def test_exact_settlement_dry_run_writes_nothing_and_computes_pnl(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_due_exact(session, ticker="KXEXACT-26JUL04-Y", side=BUY_YES, quantity=2)

        payload = build_phase3ay_settle_due_paper(
            session,
            exact_only=True,
            dry_run=True,
            apply=False,
            max_records=5,
            output_dir=Path(tmp_path),
        )

        assert session.scalar(select(PaperPnl)) is None

    row = payload["rows"][0]
    assert payload["summary"]["safe_to_apply_count"] == 1
    assert row["safe_to_apply"] is True
    assert Decimal(row["payout"]) == Decimal("2")
    assert Decimal(row["realized_pnl"]) == Decimal("1.20")
    assert Decimal(row["roi"]) == Decimal("1.5")
    assert row["proposed_ledger_mutation"] == "INSERT paper_pnl settled-market row"


def test_exact_settlement_apply_requires_exact_and_backup(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_due_exact(session)

        with pytest.raises(ValueError, match="exact-only"):
            build_phase3ay_settle_due_paper(
                session,
                exact_only=False,
                dry_run=True,
                apply=False,
                output_dir=Path(tmp_path),
            )
        with pytest.raises(ValueError, match="backup-first"):
            build_phase3ay_settle_due_paper(
                session,
                exact_only=True,
                dry_run=False,
                apply=True,
                backup_first=False,
                output_dir=Path(tmp_path),
            )


def test_exact_settlement_apply_is_bounded_and_idempotent(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_due_exact(session, ticker="KXAPPLY-26JUL04-Y")

        payload = build_phase3ay_settle_due_paper(
            session,
            exact_only=True,
            dry_run=False,
            apply=True,
            backup_first=True,
            max_records=1,
            output_dir=Path(tmp_path),
        )
        after_first = build_phase3ay_due_settlement_diagnostic(session)

        second = build_phase3ay_settle_due_paper(
            session,
            exact_only=True,
            dry_run=True,
            apply=False,
            max_records=1,
            output_dir=Path(tmp_path),
        )

    assert payload["summary"]["rows_applied"] == 1
    assert payload["backup_path"]
    assert after_first["summary"]["already_settled_trades"] == 1
    assert second["summary"]["safe_to_apply_count"] == 0


def test_settlement_diagnostic_rejects_sibling_and_fuzzy_title_matches(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_due_order(session, "KXSIB-26JUL04-A", title="Will the test market resolve?")
        _seed_market(session, "KXSIB-26JUL04-B", title="Will the test market resolve?")
        upsert_settlement(
            session,
            {
                "ticker": "KXSIB-26JUL04-B",
                "result": "yes",
                "settlement_ts": utc_now().isoformat(),
            },
        )
        _seed_due_order(session, "KXFUZZY-26JUL04-A", title="Same market title")
        _seed_market(session, "KXUNRELATED-26JUL04-B", title="Same market title")
        upsert_settlement(
            session,
            {
                "ticker": "KXUNRELATED-26JUL04-B",
                "result": "yes",
                "settlement_ts": utc_now().isoformat(),
            },
        )

        payload = build_phase3ay_due_settlement_diagnostic(session)

    rows = {row["ticker"]: row for row in payload["rows"]}
    assert rows["KXSIB-26JUL04-A"]["primary_state"] == "SIBLING_TICKER_REJECTED"
    assert rows["KXSIB-26JUL04-A"]["safe_to_apply"] is False
    assert rows["KXFUZZY-26JUL04-A"]["primary_state"] == "AWAITING_EXACT_MARKET_SETTLEMENT"
    assert rows["KXFUZZY-26JUL04-A"]["safe_to_apply"] is False


def test_settlement_diagnostic_rejects_ambiguous_outcomes_and_composites(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_due_order(session, "KXBADOUTCOME-26JUL04-Y")
        upsert_settlement(
            session,
            {
                "ticker": "KXBADOUTCOME-26JUL04-Y",
                "result": "maybe",
                "settlement_ts": utc_now().isoformat(),
            },
        )
        _seed_due_order(session, "KXMVECROSSCATEGORY-LOCAL-1")

        payload = build_phase3ay_due_settlement_diagnostic(session)

    rows = {row["ticker"]: row for row in payload["rows"]}
    assert rows["KXBADOUTCOME-26JUL04-Y"]["primary_state"] == "MARKET_OUTCOME_MISSING"
    assert rows["KXMVECROSSCATEGORY-LOCAL-1"]["primary_state"] == (
        "COMPOSITE_LOCAL_REQUIRES_RESOLVER"
    )
    assert rows["KXMVECROSSCATEGORY-LOCAL-1"]["safe_to_apply"] is False


def test_grouped_source_review_collapses_and_applies_operator_values(tmp_path) -> None:
    template = Path(tmp_path) / "template.csv"
    group_review = Path(tmp_path) / "group_review.csv"
    filled = Path(tmp_path) / "filled.csv"
    _write_source_template(template)

    group_artifacts = write_phase3bb_group_source_review(
        input_path=template,
        output_path=group_review,
    )
    rows = _read_csv(group_review)
    rows[0]["observed_value"] = "1.24"
    rows[0]["source_name"] = "Operator source"
    rows[0]["source_url"] = "https://example.com/source"
    rows[0]["retrieved_at"] = "2026-07-04T12:00:00Z"
    _write_csv(group_review, rows)

    apply_artifacts = write_phase3bb_apply_group_source_review(
        group_review_path=group_review,
        template_path=template,
        output_path=filled,
    )
    filled_rows = _read_csv(filled)

    assert group_artifacts.row_count == 25
    assert group_artifacts.group_count == 3
    assert apply_artifacts.rows_updated == 7
    assert sum(1 for row in filled_rows if row["source_url"]) == 7
    assert sum(1 for row in filled_rows if row["price_usd_each"] == "1.24") == 7
    assert sum(1 for row in filled_rows if row["cancellation_count"]) == 0


def test_phase3am_gap_burndown_defaults_to_dry_run(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_minimal_reports(Path("reports"))
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_due_exact(session, ticker="KXBURN-26JUL04-Y")

        artifacts = write_phase3am_gap_burndown_report(
            session,
            output_dir=Path("reports/phase3am"),
            reports_dir=Path("reports"),
            settlement_dry_run=True,
            settlement_apply_exact_only=False,
        )

        assert session.scalar(select(PaperPnl)) is None

    burn = json.loads(artifacts.burn_down_path.read_text(encoding="utf-8"))
    assert artifacts.manifest_path.exists()
    assert burn["settlement"]["dry_run"]["safe_to_apply_count"] == 1
    assert burn["settlement"]["newly_exact_settled_count"] == 0
    assert not (Path("reports/phase3am") / "due_settlement_apply.json").exists()


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3am.db'}")
    return get_session_factory(engine)


def _seed_due_exact(
    session,
    *,
    ticker: str = "KXEXACT-26JUL04-Y",
    side: str = BUY_YES,
    quantity: int = 1,
) -> None:
    _seed_due_order(session, ticker, side=side, quantity=quantity)
    upsert_settlement(
        session,
        {
            "ticker": ticker,
            "result": "yes",
            "settlement_ts": utc_now().isoformat(),
        },
    )
    session.flush()


def _seed_due_order(
    session,
    ticker: str,
    *,
    side: str = BUY_YES,
    quantity: int = 1,
    title: str = "Due paper market",
) -> PaperOrder:
    _seed_market(session, ticker, title=title)
    order = PaperOrder(
        ticker=ticker,
        forecast_id=None,
        created_at=utc_now(),
        model_name=f"phase3am_{ticker}",
        side=side,
        probability="0.60",
        market_price="0.40" if side == BUY_YES else "0.30",
        limit_price="0.40" if side == BUY_YES else "0.30",
        edge="0.20",
        quantity=quantity,
        status=ORDER_FILLED,
        reason="phase3am test",
        raw_decision_json="{}",
    )
    session.add(order)
    session.flush()
    return order


def _seed_market(session, ticker: str, *, title: str = "Due paper market") -> None:
    upsert_market(
        session,
        {
            "ticker": ticker,
            "event_ticker": ticker.rsplit("-", 1)[0],
            "series_ticker": ticker.split("-", 1)[0],
            "status": "finalized",
            "title": title,
            "close_time": (utc_now() - timedelta(hours=2)).isoformat(),
        },
    )
    session.add(
        MarketLeg(
            ticker=ticker,
            leg_index=0,
            parsed_at=utc_now(),
            side="yes",
            category="general",
            market_type="BINARY",
            entity_name=ticker,
            operator="eq",
            threshold_value=None,
            unit=None,
            confidence="0.90",
            raw_text=title,
            reason="test",
            raw_json="{}",
        )
    )
    session.flush()


def _write_source_template(path: Path) -> None:
    rows = []
    for index in range(7):
        rows.append(
            {
                "ticker": f"KXAMSAVO-{index}",
                "source_adapter_key": "commodity_advertised_price_source",
                "source_subject": "Avocados, Hass",
                "commodity": "Avocados",
                "variety": "Hass",
                "metric": "weighted_average_advertised_price",
                "price_usd_each": "",
                "as_of_date": "July 3, 2026",
                "region": "",
                "period_start": "",
                "period_end": "",
                "cancellation_count": "",
                "measurement_year": "",
                "capacity_gw": "",
                "threshold": "1.20",
                "threshold_unit": "USD_EACH",
                "direction": "above",
                "time_window": "July 3, 2026",
                "source_name": "",
                "source_url": "",
                "verification_status": "",
                "retrieved_at": "",
                "evidence_notes": "",
            }
        )
    for index in range(9):
        rows.append(
            {
                **rows[0],
                "ticker": f"KXFLIGHT-{index}",
                "source_adapter_key": "transportation_flight_cancellation_source",
                "source_subject": "",
                "metric": "total_flight_cancellations",
                "price_usd_each": "",
                "as_of_date": "",
                "region": "United States",
                "period_start": "2026-06-27",
                "period_end": "2026-07-03",
                "cancellation_count": "",
                "threshold": "3000",
                "threshold_unit": "CANCELLATIONS",
            }
        )
    for index in range(9):
        rows.append(
            {
                **rows[0],
                "ticker": f"KXCAPACITY-{index}",
                "source_adapter_key": "infrastructure_data_center_capacity_source",
                "source_subject": "",
                "metric": "operational_data_center_capacity",
                "price_usd_each": "",
                "as_of_date": "",
                "region": "Americas",
                "measurement_year": "2026",
                "capacity_gw": "",
                "threshold": "55.0",
                "threshold_unit": "GW",
            }
        )
    _write_csv(path, rows)


def _write_minimal_reports(reports_dir: Path) -> None:
    _write_json(
        reports_dir / "phase3az" / "phase3az_gap_analysis.json",
        {"summary": {"gap_count": 6, "implementation_needed_count": 1}, "gaps": []},
    )
    _write_json(
        reports_dir / "phase3bb" / "phase3bb_domain_readiness.json",
        {
            "domain_rows": [
                {
                    "domain": "general",
                    "actionable_now": False,
                    "counts": {"parsed_markets": 0},
                }
            ]
        },
    )
    _write_json(
        reports_dir / "market_coverage" / "market_coverage_doctor.json",
        {"coverage_rows": [{"scope": "general", "health": "HEALTHY"}]},
    )
    _write_json(
        reports_dir / "phase3z_r2" / "phase3z_r2_sports_provenance_repair.json",
        {"summary": {"rows_reviewed": 1, "rows_safe_to_repair": 0}},
    )
    _write_json(
        reports_dir / "phase3ah_sports" / "phase3ah_round_placeholder_resolution_report.json",
        {"summary": {"rows_reviewed": 0}, "rows": []},
    )
    _write_json(
        reports_dir / "phase3aa_r2" / "phase3aa_r2_exact_settlement_harvest.json",
        {"summary": {"exact_settlements_written": 0, "fetch_errors": 0}},
    )
    _write_json(
        reports_dir / "phase3aa" / "phase3aa_outcome_realizer.json",
        {"eligible_after_realize": 0, "eta_schedule": {"summary": {"due_or_overdue": 0}}},
    )
    _write_json(
        reports_dir / "phase3aa_r3" / "phase3aa_r3_residual_settlement_audit.json",
        {"summary": {"residue_cleared": True, "residual_rows": 0}},
    )
    _write_json(
        reports_dir / "paper_settlement_reconciliation" / "paper_settlement_reconciliation.json",
        {"summary": {"eligible_to_settle_now": 0}},
    )
    _write_json(reports_dir / "phase_orchestrator.json", {"evidence": {"sports_provenance": {}}})


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    headers: list[str] = []
    for row in rows:
        for key in row:
            if key not in headers:
                headers.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
