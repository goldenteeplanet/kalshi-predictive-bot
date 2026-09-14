import json
import sqlite3
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal

import pytest
from test_cf_process_inputs import fixture
from test_settlement_target_research_router import NOW, target

from kalshi_predictor.crypto import research_shadow as S
from kalshi_predictor.paper import fees


def artifact(raw):
    return {"hex": raw.hex(), "sha256": S.sha(raw)}


def request():
    t = target()
    market = json.loads(t.market_original)
    market["market"].update(
        status="active",
        price_level_structure="linear_cent",
        strike_type="greater",
        floor_strike="100",
        cap_strike=None,
        price_ranges=[dict(start="0", end="1", step="0.01")],
    )
    raw = S.encode(market)
    cf, rec = fixture()
    cf_raw = S.encode(cf)
    rec["source_sha256"] = S.sha(cf_raw)
    book = S.encode(
        {"orderbook_fp": {"yes_dollars": [["0.30", "5.00"]], "no_dollars": [["0.40", "6.00"]]}}
    )

    def receipt(body, suffix):
        return artifact(
            S.encode(
                dict(
                    method="GET",
                    url=f"https://api.elections.kalshi.com/trade-api/v2/markets/{t.rules.market_ticker}{suffix}",
                    http_status=200,
                    original_complete=True,
                    source_sha256=S.sha(body),
                    requested_at=NOW.isoformat(),
                    received_at=NOW.isoformat(),
                )
            )
        )

    return dict(
        schema="crypto-average-shadow-request-v1",
        target=dict(
            symbol=t.symbol,
            event_ticker=t.event_ticker,
            rules=asdict(t.rules),
            comparator=t.comparator,
            threshold="100",
            lower=None,
            upper=None,
            rule_received_at=NOW.isoformat(),
            market_received_at=NOW.isoformat(),
            finality_deadline=None,
            finality_basis="UNRESOLVED",
        ),
        rule=artifact(t.rule_original),
        market=artifact(raw),
        market_receipt=receipt(raw, ""),
        book=artifact(book),
        book_receipt=receipt(book, "/orderbook?depth=5"),
        cf=artifact(cf_raw),
        cf_receipt=artifact(S.encode(rec)),
    )


def test_actual_cf_average_persist_read_idempotent_and_no_paper(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "now", lambda: NOW)
    path = tmp_path / "research.db"
    S.initialize_journal(path)
    raw = S.encode(request())
    decision = S.append_decision(path, raw)
    assert decision["forecast"]["model"] == "crypto_settlement_average_research_v1"
    assert decision["cf_evidence"]["prices"] == 3600
    assert decision["rows"][0]["executable_price"] == "0.60"
    assert decision["rows"][0]["net_ev"] is None
    monkeypatch.setattr(S, "now", lambda: NOW + timedelta(days=2))
    assert S.append_decision(path, raw) == decision
    status = S.journal_status(path)
    assert status["historical_decisions"] == status["historical_events"] == 1
    assert status["eligible"] == status["positive_complete_net_ev"] == 0
    with sqlite3.connect(path) as db:
        assert set(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()) == {
            ("research_shadow",),
            ("research_completion",),
        }
        stored = json.loads(db.execute("SELECT payload FROM research_shadow").fetchone()[0])
        assert bytes.fromhex(stored["request_hex"]) == raw
        assert "crypto.settlement_average_model" in stored["source_originals"]
        with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE"):
            db.execute("DELETE FROM research_shadow")


@pytest.mark.parametrize(
    "mutation",
    [
        "hash",
        "cf_receipt",
        "book_status",
        "book_bool",
        "wrong_url",
        "target_receipt",
        "unknown_ticks",
        "duplicate_price",
        "forged_fee",
    ],
)
def test_bad_evidence_never_writes(tmp_path, monkeypatch, mutation):
    monkeypatch.setattr(S, "now", lambda: NOW)
    r = request()
    if mutation == "hash":
        r["cf"]["sha256"] = "0" * 64
    elif mutation == "cf_receipt":
        rec = json.loads(S.original(r["cf_receipt"]))
        rec["received_at"] = (NOW + timedelta(seconds=1)).isoformat()
        r["cf_receipt"] = artifact(S.encode(rec))
    elif mutation in {"book_status", "wrong_url"}:
        rec = json.loads(S.original(r["book_receipt"]))
        rec["http_status" if mutation == "book_status" else "url"] = (
            500 if mutation == "book_status" else "https://evil.invalid/"
        )
        r["book_receipt"] = artifact(S.encode(rec))
    elif mutation == "target_receipt":
        r["target"]["market_received_at"] = (NOW - timedelta(seconds=1)).isoformat()
    elif mutation == "forged_fee":
        r["fee_quotes"] = {"YES": artifact(b'{"quantity":1,"simulator_floor":"0.02"}')}
    else:
        if mutation == "unknown_ticks":
            market = json.loads(S.original(r["market"]))
            del market["market"]["price_level_structure"]
            r["market"] = artifact(S.encode(market))
            key = "market"
        else:
            book = json.loads(S.original(r["book"]))
            if mutation == "book_bool":
                book["orderbook_fp"]["yes_dollars"][0][1] = True
            else:
                book["orderbook_fp"]["yes_dollars"] *= 2
            r["book"] = artifact(S.encode(book))
            key = "book"
        rec = json.loads(S.original(r[key + "_receipt"]))
        rec["source_sha256"] = r[key]["sha256"]
        r[key + "_receipt"] = artifact(S.encode(rec))
    path = tmp_path / "research.db"
    S.initialize_journal(path)
    with pytest.raises((ValueError, KeyError)):
        S.append_decision(path, S.encode(r))
    assert S.journal_status(path)["historical_decisions"] == 0


def test_cannot_adopt_application_db(tmp_path):
    path = tmp_path / "app.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE paper_orders(id INTEGER)")
    with pytest.raises(FileExistsError):
        S.initialize_journal(path)
    with pytest.raises(ValueError, match="DEDICATED"):
        S.journal_status(path)


def test_slow_append_rolls_back(tmp_path, monkeypatch):
    clocks = iter((NOW, NOW + timedelta(seconds=61)))
    monkeypatch.setattr(S, "now", lambda: next(clocks))
    path = tmp_path / "research.db"
    S.initialize_journal(path)
    with pytest.raises(ValueError, match="CLOCK"):
        S.append_decision(path, S.encode(request()))
    assert S.journal_status(path)["historical_decisions"] == 0


def test_actual_fee_policy_recomputation_does_not_claim_total_ev(monkeypatch):
    from test_guarded_fee_contract import synthetic_evidence

    r = request()
    ticker = target().rules.market_ticker
    evidence = synthetic_evidence(monkeypatch, now=NOW, ticker=ticker)
    for item in evidence["captures"]:
        row = json.loads(bytes.fromhex(item["payload_hex"]))
        if "/markets/" in row["url"]:
            row["body"] = json.loads(S.original(r["market"]))
        if "/events/" in row["url"]:
            row["url"] = fees.PUBLIC_BASE + "/events/KXBTC-E"
            row["body"]["event"]["event_ticker"] = "KXBTC-E"
        raw = S.encode(row)
        item.update(payload_hex=raw.hex(), sha256=S.sha(raw))
    q = fees.build_fee_quote(
        evidence=evidence,
        ticker=ticker,
        side="BUY_YES",
        price=Decimal(".60"),
        simulator_floor=Decimal("0"),
        now=NOW,
    )
    r["fee_quotes"] = {"YES": artifact(q.payload)}
    result = S.build_decision(S.encode(r), as_of=NOW)
    assert result["rows"][0]["exchange_fee"] == "0.02"
    assert result["rows"][0]["exchange_fee_only_ev"] is not None
    assert result["rows"][0]["net_ev"] is None
    assert result["paper_eligible"] is False


def test_cli_initialization_and_status(tmp_path, monkeypatch, capsys):
    import importlib.util
    import sys
    from pathlib import Path

    source = Path(__file__).parents[1] / "scripts/crypto_research_shadow.py"
    spec = importlib.util.spec_from_file_location("shadow_cli", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "cli.db"
    request_path = tmp_path / "request.json"
    request_path.write_bytes(S.encode(request()))
    monkeypatch.setattr(S, "now", lambda: NOW)
    for action in ("initialize", "append", "status"):
        argv = [str(source), action, "--journal", str(path)]
        if action == "append":
            argv += ["--request", str(request_path)]
        monkeypatch.setattr(sys, "argv", argv)
        module.main()
        result = json.loads(capsys.readouterr().out)
        assert result["execution_authority"] is False
    assert result["completed_decisions"] == 1


def test_fractional_top_crossed_book_refused():
    book = S.encode(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.30", "5"], ["0.70", "0.5"]],
                "no_dollars": [["0.40", "5"]],
            }
        }
    )
    with pytest.raises(ValueError, match="CROSSED"):
        S.book_prices(book, json.loads(S.original(request()["market"]))["market"])


def test_external_original_hosts_and_exact_payoff(monkeypatch):
    r = request()
    for key in ("market_receipt", "book_receipt"):
        rec = json.loads(S.original(r[key]))
        rec["url"] = rec["url"].replace("api.elections.kalshi.com", "external-api.kalshi.com")
        r[key] = artifact(S.encode(rec))
    S.build_decision(S.encode(r), as_of=NOW)
    r["target"]["threshold"] = "101"
    with pytest.raises(ValueError, match="PAYOFF"):
        S.build_decision(S.encode(r), as_of=NOW)


def test_commit_late_is_preserved_but_not_completed(tmp_path, monkeypatch):
    clocks = iter((NOW, NOW, NOW + timedelta(seconds=61)))
    monkeypatch.setattr(S, "now", lambda: next(clocks))
    path = tmp_path / "late.db"
    S.initialize_journal(path)
    result = S.append_decision(path, S.encode(request()))
    assert result["journal_completion"]["status"] == "LATE_OR_CHANGED_RESEARCH_ONLY"
    assert result["forecast"]["prediction_recorded_at"] is None
    assert S.journal_status(path)["completed_decisions"] == 0


def test_original_commit_receipt_is_honest_and_bound(tmp_path, monkeypatch):
    clocks = iter((NOW, NOW, NOW + timedelta(seconds=2)))
    monkeypatch.setattr(S, "now", lambda: next(clocks))
    path = tmp_path / "honest.db"
    S.initialize_journal(path)
    result = S.append_decision(path, S.encode(request()))
    assert (
        result["journal_completion"]["original_committed_before"]
        == (NOW + timedelta(seconds=2)).isoformat()
    )
    assert result["journal_completion"]["status"] == "COMPLETE_RESEARCH"


def test_interrupted_after_commit_never_reports_complete(tmp_path, monkeypatch):
    clocks = iter((NOW, NOW))
    monkeypatch.setattr(S, "now", lambda: next(clocks))
    path = tmp_path / "interrupted.db"
    S.initialize_journal(path)
    raw = S.encode(request())
    with pytest.raises(StopIteration):
        S.append_decision(path, raw)
    assert S.journal_status(path)["completed_decisions"] == 0
    assert S.append_decision(path, raw)["journal_completion"]["status"] == "INCOMPLETE_RESEARCH"
