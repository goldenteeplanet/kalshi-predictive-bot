import json
from datetime import timedelta
from decimal import Decimal

import pytest
from test_crypto_research_shadow import artifact, request
from test_settlement_target_research_router import END, NOW

from kalshi_predictor.crypto import research_shadow as S
from kalshi_predictor.crypto import research_shadow_evaluation as E


def cohort(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "now", lambda: NOW)
    journal = tmp_path / "research.db"
    S.initialize_journal(journal)
    files = {}
    decisions = []
    officials = {}
    hypotheses = [
        dict(name="LEFT_CLOSED_RIGHT_OPEN", include_start=True, include_end=False),
        dict(name="LEFT_OPEN_RIGHT_CLOSED", include_start=False, include_end=True),
    ]
    for i in range(2):
        for h in hypotheses:
            r = request()
            r["hypothesis"] = h["name"]
            ticker = f"KXBTC-E-B{i}"
            r["target"].update(
                comparator="RANGE_CLOSED", threshold=None, lower=str(99 + i), upper=str(100 + i)
            )
            r["target"]["rules"].update(market_ticker=ticker)
            r["target"]["rules"]["closing"].update(
                include_start=h["include_start"], include_end=h["include_end"]
            )
            market = json.loads(S.original(r["market"]))
            market["market"].update(
                ticker=ticker,
                strike_type="between",
                floor_strike=str(99 + i),
                cap_strike=str(100 + i),
            )
            r["market"] = artifact(S.encode(market))
            for key in ("market_receipt", "book_receipt"):
                rec = json.loads(S.original(r[key]))
                rec["url"] = rec["url"].replace("KXBTC-E-T100", ticker)
                if key == "market_receipt":
                    rec["source_sha256"] = r["market"]["sha256"]
                r[key] = artifact(S.encode(rec))
            raw = S.encode(r)
            d = S.append_decision(journal, raw)
            with S.connect(journal, readonly=True) as db:
                payload_sha = db.execute(
                    "SELECT payload_sha FROM research_shadow WHERE id=?", (d["decision_id"],)
                ).fetchone()[0]
                completion_sha = db.execute(
                    "SELECT payload_sha FROM research_completion WHERE id=?", (d["decision_id"],)
                ).fetchone()[0]
            decisions.append(
                dict(
                    decision_id=d["decision_id"],
                    payload_sha256=payload_sha,
                    completion_sha256=completion_sha,
                    ticker=ticker,
                    hypothesis=h["name"],
                    request_sha256=S.sha(raw),
                    rule_version=d["rule_version"],
                )
            )
            files[f"request-{i}-{h['name']}.json"] = raw
            final = market["market"] | dict(
                status="finalized",
                result="yes" if i else "no",
                settlement_ts=(END + timedelta(minutes=1)).isoformat(),
                settlement_value_dollars="1.00" if i else "0.00",
                is_provisional=False,
            )
            final_raw = S.encode(dict(market=final))
            receipt = S.encode(
                dict(
                    method="GET",
                    url=f"https://external-api.kalshi.com/trade-api/v2/markets/{ticker}",
                    http_status=200,
                    original_complete=True,
                    source_sha256=S.sha(final_raw),
                    requested_at=(END + timedelta(minutes=5)).isoformat(),
                    received_at=(END + timedelta(minutes=5, seconds=1)).isoformat(),
                )
            )
            officials[ticker] = (final_raw, receipt)
    plan = dict(
        schema="cf-average-prospective-slot-v1",
        symbol="BTC",
        benchmark="BRTI",
        target_at=END.isoformat(),
        event_ticker="KXBTC-E",
        not_before=NOW.isoformat(),
        not_after=(NOW + timedelta(minutes=1)).isoformat(),
        hypotheses=hypotheses,
        rounding="HALF_UP",
        decimal_places=2,
    )
    files["plan.original.json"] = S.encode(plan)
    files["shadow-pins.json"] = S.encode(
        dict(
            schema="cf-shadow-pins-v1", event="KXBTC-E", target=END.isoformat(), decisions=decisions
        )
    )
    files["shadow-pins.recorded.json"] = S.encode(
        dict(recorded_at=NOW.isoformat(), sha256=S.sha(files["shadow-pins.json"]))
    )
    files["selection.json"] = S.encode(dict(selected=list(officials)))
    files["result.json"] = S.encode(dict(status="RESEARCH_COMPLETE"))
    files["completion.json"] = S.encode(
        dict(
            status="COMPLETE",
            recorded_after_result=NOW.isoformat(),
            result_sha256=S.sha(files["result.json"]),
            files={k: S.sha(v) for k, v in files.items()},
        )
    )
    return journal, files, officials


def run(data):
    journal, files, official = data
    return E.evaluate(
        journal,
        files,
        completion_sha256=S.sha(files["completion.json"]),
        official=official,
        as_of=END + timedelta(minutes=6),
    )


def test_all_real_frozen_average_rows_same_contract_labels_no_model_rerun(tmp_path, monkeypatch):
    data = cohort(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "build_decision", lambda *a, **k: pytest.fail("MODEL_RERUN"))
    result = run(data)
    assert len(result["rows"]) == 4 and result["event_count"] == 1
    for row in result["rows"]:
        p = Decimal(row["average"]["probability"])
        y = row["average"]["outcome"]
        assert Decimal(row["average"]["brier"]) == (p - y) ** 2
        assert row["market_midpoint"]["probability"] == "0.45"
    summary = E.aggregate([result])
    assert len(summary["scenarios"]) == 2
    assert all(s["events"] == 1 and s["contracts"] == 2 for s in summary["scenarios"])
    assert summary["independent_n"] is None
    with pytest.raises(ValueError, match="DUPLICATE"):
        E.aggregate([result, result])


@pytest.mark.parametrize(
    "kind",
    [
        "provisional0",
        "provisionaltrue",
        "nonfinal",
        "wrongclose",
        "future",
        "wrongstrike",
        "duplicatejson",
        "wrongpayout",
        "changedrules",
    ],
)
def test_invalid_official_no_scores(tmp_path, monkeypatch, kind):
    data = cohort(tmp_path, monkeypatch)
    official = data[2]
    ticker = next(iter(official))
    raw, rr = official[ticker]
    final = json.loads(raw)
    changes = {
        "provisional0": ("is_provisional", 0),
        "provisionaltrue": ("is_provisional", True),
        "nonfinal": ("status", "closed"),
        "wrongclose": ("close_time", NOW.isoformat()),
        "future": ("settlement_ts", (END + timedelta(days=1)).isoformat()),
        "wrongstrike": ("floor_strike", "80"),
        "wrongpayout": ("settlement_value_dollars", "1.00"),
        "changedrules": ("rules_primary", "changed original rules"),
    }
    if kind == "duplicatejson":
        raw = raw.replace(b'"market":', b'"market":{},"market":', 1)
    else:
        key, value = changes[kind]
        final["market"][key] = value
        raw = S.encode(final)
    rec = json.loads(rr)
    rec["source_sha256"] = S.sha(raw)
    official[ticker] = (raw, S.encode(rec))
    with pytest.raises(ValueError):
        run(data)


def test_pinned_prediction_tamper_and_missing_scenario_reject(tmp_path, monkeypatch):
    data = cohort(tmp_path, monkeypatch)
    journal, files, _ = data
    with S.connect(journal) as db:
        db.execute("DROP TRIGGER no_update")
        raw = db.execute("SELECT payload FROM research_shadow LIMIT 1").fetchone()[0]
        changed = json.loads(raw)
        changed["decision"]["forecast"]["probability"] = 0.9
        new = S.encode(changed)
        db.execute(
            "UPDATE research_shadow SET payload=?,payload_sha=? WHERE payload=?",
            (new, S.sha(new), raw),
        )
        db.commit()
    with pytest.raises(ValueError, match="PIN"):
        run(data)


def test_zero_one_logloss_no_clipping():
    assert E.scores(Decimal(0), 1)["log_loss"] == "POSITIVE_INFINITY"
    assert E.scores(Decimal(1), 0)["log_loss"] == "POSITIVE_INFINITY"
    assert E.scores(Decimal(0), 0)["log_loss"] == 0
    assert E.scores(Decimal("1e-999"), 1)["log_loss"] > 2000


def test_equal_event_weighting_fixed_ece_and_no_window_selection():
    rows = []
    for i, (event, p, y) in enumerate((("E1", ".8", 1), ("E1", ".6", 0), ("E2", ".2", 0))):
        score = E.scores(Decimal(p), y)
        rows.append(
            dict(
                decision_id=str(i),
                ticker=str(i),
                event=event,
                hypothesis="A",
                semantic_scenario_id="semantic-A",
                asset_hour=event,
                correlation_cluster="SOL:2026-09-11",
                average=score,
                market_midpoint=score,
            )
        )
    result = E.aggregate([dict(schema="crypto-shadow-evaluation-v1", rows=rows)])
    group = result["scenarios"][0]
    assert group["events"] == 2 and group["contracts"] == 3
    assert Decimal(group["models"]["average"]["brier"]) == Decimal(".12")
    assert Decimal(group["models"]["average"]["ece"]) == Decimal(".30")
    assert group["correlation_clusters"] == ["SOL:2026-09-11"]
    changed = json.loads(json.dumps(rows[0]))
    changed["decision_id"] = "another-id"
    with pytest.raises(ValueError, match="DUPLICATE_CONTRACT"):
        E.aggregate([dict(schema="crypto-shadow-evaluation-v1", rows=rows + [changed])])
    rows[0]["average"] = E.scores(Decimal("1"), 1)
    rows[0]["average"]["log_loss"] = False
    with pytest.raises(ValueError, match="ALTERED"):
        E.aggregate([dict(schema="crypto-shadow-evaluation-v1", rows=rows)])


def test_missing_contract_and_failed_capture_refuse(tmp_path, monkeypatch):
    data = cohort(tmp_path, monkeypatch)
    official = data[2].pop(next(iter(data[2])))
    with pytest.raises(ValueError, match="COHORT"):
        run(data)
    assert official
    data[1]["failure.json"] = b"{}"
    with pytest.raises(ValueError, match="FAILED_CAPTURE"):
        run(data)


def test_exclusive_original_publication_and_postartifact_receipt(tmp_path, monkeypatch):
    data = cohort(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "now", lambda: END + timedelta(minutes=6))
    journal, files, official = data
    output = tmp_path / "out"
    E.write_evaluation(
        output, journal, files, completion_sha256=S.sha(files["completion.json"]), official=official
    )
    receipt = json.loads((output / "recording_receipt.json").read_bytes())
    for name, digest in receipt["files"].items():
        assert S.sha((output / name).read_bytes()) == digest
    assert (output / "journal").is_dir()
    with pytest.raises(FileExistsError):
        E.write_evaluation(
            output,
            journal,
            files,
            completion_sha256=S.sha(files["completion.json"]),
            official=official,
        )


def test_publication_failure_cannot_present_complete_receipt(tmp_path, monkeypatch):
    data = cohort(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "now", lambda: END + timedelta(minutes=6))
    journal, files, official = data
    output = tmp_path / "failed-out"
    original = E.os.fsync
    calls = 0

    def fail_once(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected fsync failure")
        return original(fd)

    monkeypatch.setattr(E.os, "fsync", fail_once)
    with pytest.raises(OSError):
        E.write_evaluation(
            output,
            journal,
            files,
            completion_sha256=S.sha(files["completion.json"]),
            official=official,
        )
    assert (output / "failure.json").exists()
    assert not (output / "recording_receipt.json").exists()


def test_actual_capture_harness_original_schema_end_to_end(tmp_path, monkeypatch):
    from test_cf_average_shadow_capture import harness

    capture, out, calls, catalog, _, _, plan = harness(tmp_path, monkeypatch)
    capture()
    completion = (out / "completion.json").read_bytes()
    manifest = json.loads(completion)
    files = {name: (out / name).read_bytes() for name in manifest["files"]}
    files["completion.json"] = completion
    selected = json.loads(files["selection.json"])["selected"]
    target = S.at(plan["target_at"])
    official = {}
    for ticker in selected:
        market = next(m for m in catalog["markets"] if m["ticker"] == ticker)
        final = market | dict(
            status="finalized",
            result="no",
            is_provisional=False,
            settlement_ts=(target + timedelta(seconds=20)).isoformat(),
            settlement_value_dollars="0.00",
        )
        raw = S.encode(dict(market=final))
        receipt = S.encode(
            dict(
                method="GET",
                url=f"https://external-api.kalshi.com/trade-api/v2/markets/{ticker}",
                http_status=200,
                original_complete=True,
                source_sha256=S.sha(raw),
                requested_at=(target + timedelta(minutes=5)).isoformat(),
                received_at=(target + timedelta(minutes=5)).isoformat(),
            )
        )
        official[ticker] = (raw, receipt)
    monkeypatch.setattr(S, "build_decision", lambda *a, **k: pytest.fail("MODEL_RERUN"))
    result = E.evaluate(
        out / "research.db",
        files,
        completion_sha256=S.sha(completion),
        official=official,
        as_of=target + timedelta(minutes=6),
    )
    assert len(result["rows"]) == 4 and len(calls) == 6
    assert {r["symbol"] for r in result["rows"]} == {"SOL"}
