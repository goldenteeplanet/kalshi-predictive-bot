import json
from datetime import timedelta

import pytest
from test_multiasset_capture import NOW, TARGET, setup

from kalshi_predictor.crypto import multiasset_capture as C
from kalshi_predictor.crypto import multiasset_outcomes as O
from kalshi_predictor.crypto import multiasset_tournament_evidence as E
from kalshi_predictor.crypto.registered_outcome_window import outcome_window


@pytest.mark.parametrize("delay", [None, True, "40", 40.0, 9, 181])
def test_invalid_registered_delay_fails_closed(delay):
    with pytest.raises(ValueError, match="REGISTERED_OUTCOME_DELAY"):
        outcome_window({"target_at": TARGET.isoformat(), "outcome_delay_minutes": delay})


def test_legacy_window_and_explicit_future_window():
    assert outcome_window({"target_at": TARGET.isoformat()}) == (
        TARGET + timedelta(minutes=10), TARGET + timedelta(minutes=11)
    )
    assert outcome_window({"target_at": TARGET.isoformat(), "outcome_delay_minutes": 40}) == (
        TARGET + timedelta(minutes=40), TARGET + timedelta(minutes=41)
    )
    with pytest.raises(ValueError, match="AWARE_OUTCOME_TARGET"):
        outcome_window({"target_at": "2026-09-12T06:00:00"})


def capture_late_plan(tmp_path):
    plan, transport, _ = setup()
    plan["outcome_delay_minutes"] = 40
    root = tmp_path / "capture"
    C.capture(root, plan, transport, clock=lambda: NOW)
    sha = C.digest((root / "protocol.json").read_bytes())
    pin = C.encode({
        "event": plan["event"], "target_at": plan["target_at"], "at": NOW.isoformat(),
        "protocol_sha256": sha,
        "completion_sha256": C.digest((root / "completion.json").read_bytes()),
    })
    receipt = C.encode({"pin_sha256": C.digest(pin), "at": NOW.isoformat()})
    return root, pin, receipt, sha


@pytest.mark.parametrize("minutes", [10, 39, 41])
def test_preregistered_late_plan_does_not_fetch_outside_window(tmp_path, minutes):
    root, pin, receipt, sha = capture_late_plan(tmp_path)
    calls = []
    with pytest.raises(ValueError, match="EXACT_OUTCOME_WINDOW"):
        O.collect(tmp_path / "attempt", root, pin, receipt, sha,
                  lambda *args: calls.append(args),
                  clock=lambda: TARGET + timedelta(minutes=minutes))
    assert calls == []
    assert (tmp_path / "attempt/failure.json").is_file()


def test_late_first_attempt_verified_once_with_original_unknown_costs(tmp_path):
    root, pin, receipt, sha = capture_late_plan(tmp_path)
    markets = json.loads((root / "catalog.original.json").read_bytes())["markets"]
    calls = []

    def transport(url, timeout):
        calls.append(url)
        market = next(m for m in markets if url.endswith("/" + m["ticker"]))
        return 200, C.encode({"market": {
            **market, "status": "finalized", "result": "yes",
            "settlement_value_dollars": "1",
            "settlement_ts": (TARGET + timedelta(minutes=32)).isoformat(),
        }})

    output = tmp_path / "outcome"
    def clock():
        return TARGET + timedelta(minutes=40)
    assert O.collect(output, root, pin, receipt, sha, transport, clock=clock) == {
        "rows": 20, "requests": 2,
    }
    E.verify_outcomes(root, output, pin, receipt, sha, as_of=clock())
    rows = json.loads((output / "evaluation.json").read_bytes())["rows"]
    assert all(row["full_net_ev"] is None and row["hypothetical_after_cost_pnl"] is None
               for row in rows)
    with pytest.raises(FileExistsError):
        O.collect(output, root, pin, receipt, sha, transport, clock=clock)
    assert len(calls) == 2

    # Even a rehashed receipt cannot move an HTTP read outside its registered window.
    original = json.loads((output / "0.receipt.json").read_bytes())
    original["requested_at"] = (TARGET + timedelta(minutes=10)).isoformat()
    raw = C.encode(original)
    (output / "0.receipt.json").write_bytes(raw)
    complete = json.loads((output / "completion.json").read_bytes())
    complete["files"]["0.receipt.json"] = C.digest(raw)
    (output / "completion.json").write_bytes(C.encode(complete))
    with pytest.raises(ValueError, match="REGISTERED_OUTCOME_RECEIPT_CLOCKS"):
        E.verify_outcomes(root, output, pin, receipt, sha, as_of=clock())


def test_later_time_does_not_relax_finality(tmp_path):
    root, pin, receipt, sha = capture_late_plan(tmp_path)
    market = json.loads((root / "catalog.original.json").read_bytes())["markets"][0]
    calls = []

    def transport(url, timeout):
        calls.append(url)
        return 200, C.encode({"market": {**market, "status": "determined", "result": "yes"}})

    with pytest.raises(ValueError, match="STRICT_OFFICIAL_FINAL_REQUIRED"):
        O.collect(tmp_path / "outcome", root, pin, receipt, sha, transport,
                  clock=lambda: TARGET + timedelta(minutes=40))
    assert len(calls) == 1
    assert not (tmp_path / "outcome/evaluation.json").exists()
