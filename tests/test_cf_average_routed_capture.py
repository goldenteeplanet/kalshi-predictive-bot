import json
from datetime import timedelta
from pathlib import Path

import pytest
from test_cf_average_shadow_capture import C, harness

from kalshi_predictor.crypto import research_shadow as S
from kalshi_predictor.crypto import research_shadow_evaluation as E
from kalshi_predictor.forecasting import crypto_average_shadow_route as A
from kalshi_predictor.forecasting import crypto_research_router as R


def routed_harness(tmp_path, monkeypatch):
    run, out, calls, catalog, mutator, current, plan = harness(tmp_path, monkeypatch)
    monkeypatch.setattr(A, "now", lambda: current[0])
    plan.update(
        schema="cf-average-prospective-slot-v2",
        research_route=A.ROUTE,
        source_sha256={k: v["sha256"] for k, v in C.source_proof(routed=True).items()},
    )
    return run, out, calls, catalog, mutator, current, plan


def official(catalog, selected, target):
    result = {}
    for ticker in selected:
        row = next(r for r in catalog["markets"] if r["ticker"] == ticker)
        raw = S.encode(
            {
                "market": row
                | {
                    "status": "finalized",
                    "result": "no",
                    "is_provisional": False,
                    "settlement_value_dollars": "0.00",
                    "settlement_ts": (target + timedelta(minutes=1)).isoformat(),
                }
            }
        )
        receipt = S.encode(
            {
                "method": "GET",
                "url": C.BASE + "/markets/" + ticker,
                "http_status": 200,
                "original_complete": True,
                "source_sha256": S.sha(raw),
                "requested_at": (target + timedelta(minutes=5)).isoformat(),
                "received_at": (target + timedelta(minutes=5, seconds=1)).isoformat(),
            }
        )
        result[ticker] = raw, receipt
    return result


def test_genuine_harness_router_average_journal_archived_evaluation(tmp_path, monkeypatch):
    run, out, calls, catalog, _, _, plan = routed_harness(tmp_path, monkeypatch)
    original_route = R.record_crypto_average_research
    routed_calls = []

    def observed_route(**kwargs):
        result = original_route(**kwargs)
        routed_calls.append(result)
        return result

    monkeypatch.setattr(R, "record_crypto_average_research", observed_route)
    result = run()
    assert len(calls) == 6 and len(routed_calls) == len(result["decisions"]) == 4
    files = E.load_capture(out)
    pins = json.loads(files["shadow-pins.json"])
    assert pins["schema"] == "cf-shadow-pins-v2"
    for pin in pins["decisions"]:
        assert S.sha(files[f"route-{pin['decision_id']}.json"]) == pin["route_sha256"]
        assert (
            S.sha(files[f"route_completion-{pin['decision_id']}.json"])
            == pin["route_completion_sha256"]
        )
    selected = json.loads(files["selection.json"])["selected"]
    target = S.at(plan["target_at"])
    monkeypatch.setattr(S, "build_decision", lambda *a, **k: pytest.fail("MODEL_RERUN"))
    monkeypatch.setattr(A, "route_sources", lambda: pytest.fail("CURRENT_SOURCE_SUBSTITUTION"))
    # The evaluated capture carries its own externally pinned original code.
    evaluated = E.evaluate(
        out / "research.db",
        files,
        completion_sha256=S.sha(files["completion.json"]),
        official=official(catalog, selected, target),
        as_of=target + timedelta(minutes=6),
    )
    assert len(evaluated["rows"]) == 4 and evaluated["event_count"] == 1
    assert all(
        row["research_route"] == A.ROUTE and row["average"]["outcome"] == 0
        for row in evaluated["rows"]
    )
    assert evaluated["paper_pnl"] is None and evaluated["execution_authority"] is False


@pytest.mark.parametrize("change", ["unknown_schema", "route", "source", "missing_route"])
def test_invalid_opt_in_protocol_zero_get(tmp_path, monkeypatch, change):
    run, out, calls, _, _, _, plan = routed_harness(tmp_path, monkeypatch)
    if change == "unknown_schema":
        plan["schema"] = "cf-average-prospective-slot-v99"
    elif change == "route":
        plan["research_route"] = "terminal_proxy"
    elif change == "source":
        plan["source_sha256"].pop("route.model_roles")
    else:
        del plan["research_route"]
    with pytest.raises((ValueError, KeyError)):
        run()
    assert not calls and not out.exists()


def reanchor(files):
    completion = json.loads(files["completion.json"])
    completion["files"] = {k: S.sha(files[k]) for k in completion["files"]}
    completion["result_sha256"] = S.sha(files["result.json"])
    files["completion.json"] = S.encode(completion)


@pytest.mark.parametrize(
    "change", ["route_hash", "source_rehashed", "writer_source", "clock", "legacy_claim"]
)
def test_archived_route_tamper_refuses(tmp_path, monkeypatch, change):
    run, out, _, _, _, _, plan = routed_harness(tmp_path, monkeypatch)
    run()
    files = E.load_capture(out)
    pins = json.loads(files["shadow-pins.json"])
    pin = pins["decisions"][0]
    name = f"route-{pin['decision_id']}.json"
    receipt_name = f"route_completion-{pin['decision_id']}.json"
    value = json.loads(files[name])
    if change == "route_hash":
        files[name] += b" "
    elif change == "source_rehashed":
        value["route_source_originals"]["model_roles"] = {
            "hex": b"forged".hex(),
            "sha256": S.sha(b"forged"),
        }
        files[name] = S.encode(value)
    elif change == "clock":
        value["invoked_at"] = plan["target_at"]
        files[name] = S.encode(value)
    elif change == "writer_source":
        proof = json.loads(files["source.originals.json"])
        key = "crypto.settlement_average_model"
        proof[key] = {
            "hex": b"substituted model source".hex(),
            "sha256": S.sha(b"substituted model source"),
        }
        plan["source_sha256"][key] = proof[key]["sha256"]
        files["source.originals.json"] = S.encode(proof)
        files["plan.original.json"] = S.encode(plan)
    else:
        plan["schema"] = "cf-average-prospective-slot-v1"
        files["plan.original.json"] = S.encode(plan)
        pins["schema"] = "cf-shadow-pins-v1"
    if change in {"source_rehashed", "clock"}:
        receipt = json.loads(files[receipt_name])
        receipt["route_sha256"] = S.sha(files[name])
        files[receipt_name] = S.encode(receipt)
        pin.update(
            route_sha256=S.sha(files[name]), route_completion_sha256=S.sha(files[receipt_name])
        )
    files["shadow-pins.json"] = S.encode(pins)
    r = json.loads(files["shadow-pins.recorded.json"])
    r["sha256"] = S.sha(files["shadow-pins.json"])
    files["shadow-pins.recorded.json"] = S.encode(r)
    reanchor(files)  # Even a substituted outer pin cannot hide semantic/source inconsistency.
    with pytest.raises(ValueError):
        E.validate_capture(
            out / "research.db",
            files,
            completion_sha256=S.sha(files["completion.json"]),
            as_of=S.at(plan["target_at"]) + timedelta(minutes=6),
        )


def test_route_failure_never_publishes_capture_completion(tmp_path, monkeypatch):
    run, out, calls, _, _, _, _ = routed_harness(tmp_path, monkeypatch)
    write = A._write

    def fail_completion(path, raw):
        if path.name == "completion.json":
            raise OSError("synthetic route receipt failure")
        write(path, raw)

    monkeypatch.setattr(A, "_write", fail_completion)
    with pytest.raises(OSError):
        run()
    assert len(calls) == 6
    assert (out / "failure.json").exists() and not (out / "completion.json").exists()
    assert S.journal_status(out / "research.db")["historical_decisions"] == 1


def test_original_route_completion_changed_before_archive_refuses(tmp_path, monkeypatch):
    run, out, calls, _, _, _, _ = routed_harness(tmp_path, monkeypatch)
    original_route = R.record_crypto_average_research

    def changed(**kwargs):
        result = original_route(**kwargs)
        path = (
            out
            / "research.db.crypto-v3-route"
            / result["decision"]["decision_id"]
            / "completion.json"
        )
        with path.open("ab") as stream:
            stream.write(b" ")
        return result

    monkeypatch.setattr(R, "record_crypto_average_research", changed)
    with pytest.raises(ValueError, match="ORIGINAL_CHANGED_BEFORE_ARCHIVE"):
        run()
    assert len(calls) == 6
    assert (out / "failure.json").exists() and not (out / "completion.json").exists()


def test_v1_default_never_calls_router(tmp_path, monkeypatch):
    run, out, calls, _, _, _, _ = harness(tmp_path, monkeypatch)
    monkeypatch.setattr(R, "record_crypto_average_research", lambda **kw: pytest.fail("V1_ROUTED"))
    run()
    assert len(calls) == 6
    assert json.loads((out / "shadow-pins.json").read_bytes())["schema"] == "cf-shadow-pins-v1"
    assert not (out / "research.db.crypto-v3-route").exists()


def test_v2_collector_and_publication_archive_executed_validator(tmp_path, monkeypatch):
    from test_cf_average_shadow_outcomes import C as O

    run, out, _, catalog, _, _, plan = routed_harness(tmp_path, monkeypatch)
    run()
    files = E.load_capture(out)
    target = S.at(plan["target_at"])
    finals = official(catalog, json.loads(files["selection.json"])["selected"], target)
    monkeypatch.setattr(O, "TARGETS", (target.isoformat(),))
    monkeypatch.setattr(S, "now", lambda: target + timedelta(minutes=5))
    monkeypatch.setattr(S, "build_decision", lambda *a, **kw: pytest.fail("MODEL_RERUN"))
    calls = []

    def get(url, timeout):
        calls.append(url)
        return 200, finals[url.rsplit("/", 1)[-1]][0]

    output = out.with_name(out.name + "-official-outcome")
    result = O.collect(
        out / "research.db",
        out,
        output,
        completion_sha256=S.sha(files["completion.json"]),
        transport=get,
        clock=lambda: target + timedelta(minutes=5),
    )
    assert result["status"] == "SCORED" and len(calls) == 2
    validator = Path(A.__file__).read_bytes()
    assert (output / "route-validator.original.py").read_bytes() == validator
    assert (output / "evaluation/route-validator.original.py").read_bytes() == validator
    reservation = json.loads((output / "reservation.json").read_bytes())
    assert reservation["sources"][str(Path(A.__file__).resolve())] == S.sha(validator)
    receipt = json.loads((output / "evaluation/recording_receipt.json").read_bytes())
    assert receipt["files"]["route-validator.original.py"] == S.sha(validator)


def test_changed_executed_validator_refuses_publication(tmp_path, monkeypatch):
    run, out, _, catalog, _, _, plan = routed_harness(tmp_path, monkeypatch)
    run()
    files = E.load_capture(out)
    target = S.at(plan["target_at"])
    finals = official(catalog, json.loads(files["selection.json"])["selected"], target)
    monkeypatch.setattr(S, "now", lambda: target + timedelta(minutes=6))
    changed = [False]
    original_evaluate, original_read = E.evaluate, Path.read_bytes

    def evaluate(*args, **kwargs):
        result = original_evaluate(*args, **kwargs)
        changed[0] = True
        return result

    def read(path):
        raw = original_read(path)
        return (
            raw + b"changed" if changed[0] and path.resolve() == Path(A.__file__).resolve() else raw
        )

    monkeypatch.setattr(E, "evaluate", evaluate)
    monkeypatch.setattr(Path, "read_bytes", read)
    output = tmp_path / "evaluation"
    with pytest.raises(ValueError, match="VALIDATOR_CHANGED_DURING_EVALUATION"):
        E.write_evaluation(
            output,
            out / "research.db",
            files,
            completion_sha256=S.sha(files["completion.json"]),
            official=finals,
        )
    assert not output.exists()
