import json
import sqlite3
from datetime import timedelta

import pytest
from test_crypto_research_shadow import request
from test_settlement_target_research_router import NOW

from kalshi_predictor.crypto import research_shadow as S
from kalshi_predictor.forecasting import crypto_average_shadow_route as A
from kalshi_predictor.forecasting.crypto_research_router import (
    crypto_average_research_status,
    record_crypto_average_research,
)
from kalshi_predictor.forecasting.model_roles import ModelRole, research_model_role


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "now", lambda: NOW)
    monkeypatch.setattr(A, "now", lambda: NOW)
    journal = tmp_path / "research.db"
    S.initialize_journal(journal)
    return journal


def test_actual_public_route_cf_average_durable_read_and_idempotence(journal, monkeypatch):
    actual_forecast = S.forecast_benchmark_average
    calls = []

    def observed_real_forecast(*args, **kwargs):
        result = actual_forecast(*args, **kwargs)
        calls.append(result)
        return result

    monkeypatch.setattr(S, "forecast_benchmark_average", observed_real_forecast)
    raw = S.encode(request())
    result = record_crypto_average_research(journal=journal, request_raw=raw)
    assert len(calls) == 1  # Executed actual estimator and actual average math.
    decision = result["decision"]
    assert decision["forecast"] == json.loads(S.encode(calls[0]))
    assert decision["forecast"]["model"] == A.MODEL
    assert research_model_role(A.MODEL) is ModelRole.RESEARCH_CHALLENGER
    assert decision["cf_evidence"]["prices"] == 3600
    assert decision["journal_completion"]["status"] == "COMPLETE_RESEARCH"
    assert not result["paper_eligible"] and not result["execution_authority"]
    assert all(row["exchange_fee"] is None and row["net_ev"] is None for row in decision["rows"])
    assert result["route"] == "crypto_v3/settlement_average"
    assert set(result["route_receipt"]["route_source_originals"]) == {
        "crypto_average_shadow_route",
        "crypto_research_router",
        "model_roles",
    }
    with sqlite3.connect(journal) as db:
        assert set(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()) == {
            ("research_shadow",),
            ("research_completion",),
        }
        payload, digest = db.execute("SELECT payload,payload_sha FROM research_shadow").fetchone()
        assert S.sha(payload) == digest == result["route_receipt"]["payload_sha256"]
        original = json.loads(payload)
        assert bytes.fromhex(original["request_hex"]) == raw
        assert "crypto.settlement_average_model" in original["source_originals"]
    monkeypatch.setattr(S, "now", lambda: NOW + timedelta(days=2))
    monkeypatch.setattr(A, "now", lambda: NOW + timedelta(days=2))
    assert record_crypto_average_research(journal=journal, request_raw=raw) == result
    assert len(calls) == 1  # Historical read, never recompute a frozen forecast.
    status = crypto_average_research_status(journal=journal)
    assert status["historical_routed_decisions"] == status["completed_routed_decisions"] == 1
    assert status["historical_events"] == 1 and status["positive_complete_net_ev"] == 0


@pytest.mark.parametrize("mutation", ["cf", "market", "fee", "clock"])
def test_invalid_originals_or_costs_never_write(journal, mutation):
    value = request()
    if mutation in {"cf", "market"}:
        value[mutation]["sha256"] = "0" * 64
    elif mutation == "fee":
        value["fee_quotes"] = {"YES": {"hex": b"{}".hex(), "sha256": S.sha(b"{}")}}
    else:
        receipt = json.loads(S.original(value["cf_receipt"]))
        receipt["received_at"] = (NOW + timedelta(seconds=1)).isoformat()
        raw = S.encode(receipt)
        value["cf_receipt"] = {"hex": raw.hex(), "sha256": S.sha(raw)}
    with pytest.raises((ValueError, KeyError)):
        record_crypto_average_research(journal=journal, request_raw=S.encode(value))
    assert S.journal_status(journal)["historical_decisions"] == 0
    assert not A._root(journal).exists()


def test_no_adoption_of_old_standalone_decision(journal):
    raw = S.encode(request())
    S.append_decision(journal, raw)
    with pytest.raises(FileNotFoundError):
        record_crypto_average_research(journal=journal, request_raw=raw)
    assert crypto_average_research_status(journal=journal)["historical_routed_decisions"] == 0


def test_competing_standalone_append_cannot_be_adopted(journal, monkeypatch):
    original = S.append_decision

    def competing(path, raw, *, require_new=False):
        original(path, raw)  # Actual standalone writer after route SELECT saw empty.
        return original(path, raw, require_new=require_new)

    monkeypatch.setattr(S, "append_decision", competing)
    with pytest.raises(ValueError, match="RESEARCH_REQUEST_ALREADY_EXISTS"):
        record_crypto_average_research(journal=journal, request_raw=S.encode(request()))
    assert S.journal_status(journal)["historical_decisions"] == 1
    assert crypto_average_research_status(journal=journal)["historical_routed_decisions"] == 0
    assert not A._root(journal).exists()


@pytest.mark.parametrize("flag", [None, 0, 1, "true"])
def test_require_new_flag_is_exact_bool(journal, flag):
    with pytest.raises(ValueError, match="EXACT_REQUIRE_NEW_FLAG"):
        S.append_decision(journal, S.encode(request()), require_new=flag)
    assert S.journal_status(journal)["historical_decisions"] == 0


@pytest.mark.parametrize(
    "mutation",
    ["route", "payload", "authority", "source", "source_rehashed", "completion", "invoked"],
)
def test_route_tamper_cannot_be_status(journal, mutation):
    result = record_crypto_average_research(journal=journal, request_raw=S.encode(request()))
    directory = A._root(journal) / result["decision"]["decision_id"]
    value = json.loads((directory / "route.json").read_bytes())
    receipt = json.loads((directory / "completion.json").read_bytes())
    if mutation == "route":
        value["route"] = "crypto_v3/terminal_proxy"
    elif mutation == "payload":
        value["payload_sha256"] = "0" * 64
    elif mutation == "authority":
        value["paper_eligible"] = 0  # Must be exact False, not numeric equality.
    elif mutation in {"source", "source_rehashed"}:
        value["route_source_originals"]["model_roles"]["hex"] = b"changed".hex()
        if mutation == "source_rehashed":
            value["route_source_originals"]["model_roles"]["sha256"] = S.sha(b"changed")
    elif mutation == "invoked":
        value["invoked_at"] = (NOW + timedelta(seconds=1)).isoformat()
    else:
        receipt["recorded_after_route"] = (NOW + timedelta(seconds=1)).isoformat()
    raw = S.encode(value)
    receipt["route_sha256"] = S.sha(raw)
    (directory / "route.json").write_bytes(raw)
    (directory / "completion.json").write_bytes(S.encode(receipt))
    with pytest.raises(ValueError):
        crypto_average_research_status(journal=journal)


def test_source_change_after_actual_write_remains_unlinked(journal, monkeypatch):
    original = A.route_sources
    calls = 0

    def changing():
        nonlocal calls
        calls += 1
        return original() if calls == 1 else {}

    monkeypatch.setattr(A, "route_sources", changing)
    with pytest.raises(ValueError, match="SOURCE_OR_JOURNAL_CHANGED"):
        record_crypto_average_research(journal=journal, request_raw=S.encode(request()))
    assert S.journal_status(journal)["historical_decisions"] == 1
    assert crypto_average_research_status(journal=journal)["historical_routed_decisions"] == 0


def test_role_mismatch_refuses_before_average_or_db_write(journal, monkeypatch):
    monkeypatch.setattr(A, "research_model_role", lambda _: ModelRole.PAPER_ELIGIBLE)
    with pytest.raises(ValueError, match="RESEARCH_ROLE_REQUIRED"):
        record_crypto_average_research(journal=journal, request_raw=S.encode(request()))
    assert S.journal_status(journal)["historical_decisions"] == 0
