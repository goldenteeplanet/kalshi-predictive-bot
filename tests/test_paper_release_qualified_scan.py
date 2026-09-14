from dataclasses import replace
from pathlib import Path

import pytest
from test_paper_release_rules_timing import NOW, fixture

from kalshi_predictor.overnight_paper import qualified_scan, rule_verifier
from kalshi_predictor.overnight_paper.gate_context import QualificationContext
from kalshi_predictor.overnight_paper.qualified_scan import (
    OUTPUT_FIELDS,
    PreparedScanCandidate,
    run_qualified_scan,
    shortlist_candidates,
)


def prepared():
    decision, policy, document = fixture()
    context = QualificationContext(
        repository=Path(__file__).resolve().parents[1], rule_documents=(document,)
    )
    return PreparedScanCandidate(decision, context), policy


def test_empty_registry_never_initializes_network_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(rule_verifier, "CERTIFIED_RULE_POLICIES", ())

    def forbidden(*args, **kwargs):
        pytest.fail("network/archive must not be initialized for no supported family")

    monkeypatch.setattr(qualified_scan, "PublicArchive", forbidden)
    result = run_qualified_scan(tmp_path / "not-created", now=NOW)
    assert result["status"] == "NO_CERTIFIED_FAMILY"
    assert result["coverage"] == "PINNED_POLICY_TICKERS_ONLY"
    assert result["network_requests"] == result["qualified_candidates"] == 0
    assert result["rows"] == []
    assert not (tmp_path / "not-created").exists()


def test_unprepared_registry_reports_null_fields_and_zero_selection():
    _, policy = prepared()
    result = shortlist_candidates((), now=NOW, registry=(policy,))
    assert result["selected"] == []
    row = result["rows"][0]
    assert set(OUTPUT_FIELDS) <= row.keys()
    assert row["first_blocker"] == "FORECAST_NOT_PREPARED"
    assert row["probability"] is row["net_ev"] is row["risk"] is None


def test_unknown_model_is_not_readiness():
    item, policy = prepared()
    result = shortlist_candidates((item,), now=NOW, registry=(policy,))
    assert result["rows"][0]["first_blocker"] == "MODEL_NOT_READY"
    assert result["selected"] == []


def test_existing_model_without_originals_skips_books(monkeypatch):
    item, policy = prepared()
    item.decision["model_name"] = "weather_v2"
    monkeypatch.setitem(qualified_scan.SUPPORTED_MODELS, policy.series, "weather_v2")
    result = shortlist_candidates((item,), now=NOW, registry=(policy,))
    assert result["rows"][0]["first_blocker"] == "FORECAST_NOT_PREPARED"
    assert result["selected"] == []


def test_policy_identity_mismatch_fails_before_model():
    item, policy = prepared()
    item.decision["event_id"] = "another-event"
    result = shortlist_candidates((item,), now=NOW, registry=(policy,))
    assert result["rows"][0]["first_blocker"] == "RULE_IDENTITY_MISMATCH"


def test_wrong_document_hash_fails_before_model():
    item, policy = prepared()
    item = replace(item, context=replace(item.context, rule_documents=()))
    result = shortlist_candidates((item,), now=NOW, registry=(policy,))
    assert result["rows"][0]["first_blocker"] == "RULE_ORIGINAL_DOCUMENT_MISMATCH"


def test_duplicate_preparation_is_ambiguous():
    item, policy = prepared()
    result = shortlist_candidates((item, item), now=NOW, registry=(policy,))
    assert result["rows"][0]["first_blocker"] == "AMBIGUOUS_PREPARATION"
    assert result["selected"] == []


def test_stale_or_unknown_deadline_never_selects():
    item, policy = prepared()
    item.decision.pop("settlement_deadline")
    result = shortlist_candidates((item,), now=NOW, registry=(policy,))
    assert result["rows"][0]["first_blocker"] == "EXPLICIT_RULE_SUPPORTED_SETTLEMENT_TIMES_REQUIRED"


@pytest.mark.parametrize("budget", [0, 21])
def test_bounded_selection(budget):
    with pytest.raises(ValueError):
        shortlist_candidates((), now=NOW, max_candidates=budget)


def test_public_refresh_uses_exact_paths_and_requires_new_decision(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    item, _ = prepared()
    current = datetime.now(UTC)
    item.decision["market_close_time"] = (current + timedelta(hours=1)).isoformat()
    row = dict.fromkeys(OUTPUT_FIELDS)
    row.update(ticker="TEST-E-M", event="TEST-E")
    monkeypatch.setattr(
        qualified_scan,
        "shortlist_candidates",
        lambda *a, **k: {
            "status": "PREPARATION_REVIEWED",
            "coverage": "PINNED_POLICY_TICKERS_ONLY",
            "rows": [row],
            "selected": [(item, row)],
        },
    )
    calls = []

    class FakeArchive:
        def __init__(self, root, **kwargs):
            assert kwargs["max_requests"] == 30
            root.mkdir()
            self.receipts = []
            self.rate_limited = False

        def get(self, path, params=None):
            calls.append(path)
            self.receipts.append({"received_at": datetime.now(UTC).isoformat()})
            if path.endswith("/orderbook"):
                return {"orderbook": {"yes": [[45, 10000]], "no": [[50, 10000]]}}
            if path.startswith("/events/"):
                return {"event": {"event_ticker": "TEST-E", "series_ticker": "TEST"}}
            return {
                "market": {
                    "ticker": "TEST-E-M",
                    "event_ticker": "TEST-E",
                    "status": "active",
                    "close_time": item.decision["market_close_time"],
                    "volume_fp": "10000",
                    "open_interest_fp": "10000",
                    "liquidity_dollars": "10000",
                    "price_ranges": [{"start": "0", "end": "1", "step": "0.01"}],
                }
            }

    monkeypatch.setattr(qualified_scan, "PublicArchive", FakeArchive)
    result = run_qualified_scan(tmp_path / "archive", now=current)
    assert calls == ["/markets/TEST-E-M", "/events/TEST-E", "/markets/TEST-E-M/orderbook"]
    assert result["network_requests"] == 3
    assert row["book_status"] == "EXECUTABLE"
    assert row["first_blocker"] == "NEW_DECISION_REQUIRED"
    assert result["qualified_candidates"] == result["orders_created"] == 0
    assert row["net_ev"] is row["size"] is row["risk"] is None
