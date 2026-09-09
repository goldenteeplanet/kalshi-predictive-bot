"""Synthetic isolated databases; actual local ledger/P&L, no market requests."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import test_overnight_activation as activation_fixtures
from sqlalchemy import text

from kalshi_predictor.overnight_paper import activation, watcher
from kalshi_predictor.overnight_paper.watcher import PublicMarketObservation

baseline_template = activation_fixtures.baseline_template
prepared = activation_fixtures.prepared


def observation(prepared, **overrides):
    now = prepared["now"] + timedelta(hours=3)
    market = dict(
        ticker="BTC-TEST",
        event_ticker="E",
        series_ticker="S",
        status="finalized",
        result="yes",
        close_time=prepared["shadow_payload"]["close_time"],
        settlement_ts=(now - timedelta(minutes=30)).isoformat(),
        settlement_value_dollars="1.00",
    )
    market.update(overrides)
    raw = json.dumps({"market": market}).encode()
    return PublicMarketObservation(
        "BTC-TEST",
        "https://external-api.kalshi.com/trade-api/v2/markets/BTC-TEST",
        now,
        hashlib.sha256(raw).hexdigest(),
        raw,
    )


def run(prepared, item=None):
    item = item or observation(prepared)
    return watcher.reconcile_public_settlements(
        session_factory=prepared["session_factory"],
        database_path=prepared["database_path"],
        observations=(item,),
        now=item.captured_at,
    )


def counts(prepared):
    with prepared["session_factory"]() as session:
        return {
            table: session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in ("paper_orders", "paper_fills", "paper_pnl", "settlements")
        }


def test_existing_real_local_fill_settles_and_evaluates_exactly_once(prepared):
    activated = activation.activate_local_paper(**prepared)
    report = run(prepared)
    assert report.paper_evaluations_created == report.shadow_evaluations_created == 1
    assert report.realized_paper_pnl == Decimal("0.79") - activated.actual_simulated_fee
    row = report.rows[0]
    assert row["state"] == "PAPER_EVALUATED"
    assert row["evaluation"]["brier"] == "0.09"
    assert Decimal(row["evaluation"]["log_loss"]) > 0
    assert row["evaluation"]["forecast_correct"] is True
    assert counts(prepared) == dict(paper_orders=1, paper_fills=1, paper_pnl=1, settlements=1)
    again = run(prepared)
    assert again.paper_evaluations_created == again.shadow_evaluations_created == 0
    assert again.realized_paper_pnl == 0
    assert counts(prepared) == dict(paper_orders=1, paper_fills=1, paper_pnl=1, settlements=1)
    with prepared["session_factory"]() as session:
        assert watcher.verified_settled_tickers(
            session, now=observation(prepared).captured_at
        ) == frozenset({"BTC-TEST"})


@pytest.mark.parametrize("status", ["closed", "determined", "disputed", "amended"])
def test_close_or_provisional_result_never_settles(prepared, status):
    activation.activate_local_paper(**prepared)
    result = run(prepared, observation(prepared, status=status))
    assert result.paper_evaluations_created == 0
    assert counts(prepared)["settlements"] == counts(prepared)["paper_pnl"] == 0


def test_shadow_only_evaluation_does_not_create_paper_order(prepared):
    result = run(prepared)
    assert result.shadow_evaluations_created == 1
    assert result.paper_evaluations_created == 0
    assert result.rows[0]["state"] == "SHADOW_EVALUATED"
    assert counts(prepared)["paper_orders"] == counts(prepared)["paper_fills"] == 0


@pytest.mark.parametrize(
    "override",
    [
        dict(event_ticker="OTHER"),
        dict(series_ticker="OTHER"),
        dict(is_provisional=True),
        dict(settlement_value_dollars="0.5"),
    ],
)
def test_identity_or_finality_conflict_rolls_back(prepared, override):
    activation.activate_local_paper(**prepared)
    with pytest.raises(ValueError):
        run(prepared, observation(prepared, **override))
    assert counts(prepared)["settlements"] == counts(prepared)["paper_pnl"] == 0


def test_final_correction_requires_review_without_rewriting_pnl(prepared):
    activation.activate_local_paper(**prepared)
    run(prepared)
    with pytest.raises(ValueError, match="CORRECTION"):
        run(prepared, observation(prepared, result="no", settlement_value_dollars="0"))
    with prepared["session_factory"]() as session:
        assert session.execute(text("SELECT result FROM settlements")).scalar_one() == "yes"
        assert session.execute(text("SELECT count(*) FROM paper_pnl")).scalar_one() == 1


def test_harmless_final_metadata_change_preserves_initial_lineage(prepared):
    activation.activate_local_paper(**prepared)
    first = run(prepared)
    second = run(prepared, observation(prepared, title="updated display title"))
    assert second.rows[0]["final"] == first.rows[0]["final"]
    assert second.paper_evaluations_created == 0


def test_pnl_failure_rolls_back_settlement_and_shadow_evaluation(prepared, monkeypatch):
    activation.activate_local_paper(**prepared)

    def fail(*args, **kwargs):
        raise ValueError("simulated P&L failure")

    monkeypatch.setattr(watcher, "calculate_settled_pnl", fail)
    with pytest.raises(ValueError, match="P&L failure"):
        run(prepared)
    assert counts(prepared)["settlements"] == counts(prepared)["paper_pnl"] == 0
    with prepared["session_factory"]() as session:
        assert (
            session.execute(text("SELECT evaluation_json FROM overnight_shadow")).scalar_one()
            is None
        )


def test_untrusted_or_tampered_public_receipt_is_rejected(prepared):
    original = observation(prepared)
    for item in (
        replace(original, source_url="https://example.com/orders"),
        replace(original, payload=b"{}"),
        replace(original, source_url=original.source_url + "?token=secret"),
    ):
        with pytest.raises(ValueError):
            run(prepared, item)
    assert counts(prepared)["settlements"] == 0


def test_only_verified_final_marker_releases_open_capacity(prepared):
    activation.activate_local_paper(**prepared)
    with prepared["session_factory"]() as session:
        assert activation._open_position_count(session, now=prepared["now"]) == 1
    run(prepared)
    with prepared["session_factory"]() as session:
        assert activation._open_position_count(session, now=observation(prepared).captured_at) == 0
        # Tampering/removing final proof cannot leave a convenient P&L-only capacity pass.
        session.execute(
            text("DELETE FROM overnight_sprint_cycles WHERE id LIKE 'paper-evaluation:%'")
        )
        assert activation._open_position_count(session, now=observation(prepared).captured_at) == 1
        session.rollback()


def test_real_losing_position_books_negative_pnl(prepared):
    activated = activation.activate_local_paper(**prepared)
    report = run(prepared, observation(prepared, result="no", settlement_value_dollars="0"))
    assert report.realized_paper_pnl == -Decimal("0.21") - activated.actual_simulated_fee
    assert report.rows[0]["evaluation"]["brier"] == "0.49"
    assert report.rows[0]["evaluation"]["forecast_correct"] is False


def test_guarded_fill_fee_tamper_blocks_settlement(prepared):
    activation.activate_local_paper(**prepared)
    with prepared["session_factory"]() as session:
        session.execute(text("UPDATE paper_fills SET fee='0'"))
        session.commit()
    before = counts(prepared)
    with pytest.raises(ValueError, match="PAPER_FEE_FILL_LINEAGE_MISMATCH"):
        run(prepared)
    assert counts(prepared) == before
    with prepared["session_factory"]() as session:
        assert (
            session.execute(
                text(
                    "SELECT count(*) FROM overnight_sprint_cycles "
                    "WHERE id LIKE 'paper-evaluation:%'"
                )
            ).scalar_one()
            == 0
        )


def test_settlement_rejects_guarded_fill_provenance_label_tamper(prepared):
    activation.activate_local_paper(**prepared)
    with prepared["session_factory"]() as session:
        raw = json.loads(
            session.execute(text("SELECT raw_fill_json FROM paper_fills")).scalar_one()
        )
        raw["fee_provenance"] = "LEGACY_CONFIGURED_NONCERTIFIED"
        session.execute(text("UPDATE paper_fills SET raw_fill_json=:raw"), {"raw": json.dumps(raw)})
        session.commit()
        marker_count = session.execute(
            text("SELECT count(*) FROM overnight_sprint_cycles")
        ).scalar_one()
    before = counts(prepared)
    with pytest.raises(ValueError, match="PAPER_FEE_FILL_LINEAGE_MISMATCH"):
        run(prepared)
    assert counts(prepared) == before
    with prepared["session_factory"]() as session:
        assert (
            session.execute(text("SELECT count(*) FROM overnight_sprint_cycles")).scalar_one()
            == marker_count
        )
