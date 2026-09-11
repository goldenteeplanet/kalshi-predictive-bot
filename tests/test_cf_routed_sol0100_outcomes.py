import importlib.util
import json
from datetime import timedelta
from pathlib import Path

import pytest
import test_cf_average_shadow_capture as H
import test_cf_process_inputs as P
from test_cf_average_routed_capture import official, routed_harness

from kalshi_predictor.crypto import research_shadow as S
from kalshi_predictor.ui import routed_single_event as U

SPEC = importlib.util.spec_from_file_location(
    "sol0100", Path(__file__).parents[1] / "scripts/cf_routed_sol0100_outcomes.py"
)
C = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(C)


def save(path, data):
    path.write_bytes(data if type(data) is bytes else S.encode(data))


@pytest.fixture
def captured(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "NOW", U.TARGET - timedelta(hours=1))
    monkeypatch.setattr(P, "NOW", U.START)
    run, out, calls, catalog, _, current, plan = routed_harness(tmp_path, monkeypatch)
    current[0] = U.START
    plan.update(not_before=U.START.isoformat(), not_after=U.END.isoformat())
    run()
    assert len(calls) == 6
    base, control = tmp_path / "cohort", tmp_path / "control"
    base.mkdir()
    control.mkdir()
    capture = out.rename(base / "slot-0")
    raw = (capture / "plan.original.json").read_bytes()
    save(control / "slot-0.protocol.json", raw)
    method = dict(
        source_sha256={k: S.sha(v) for k, v in C.sources().items()},
        frozen_at=(U.START - timedelta(seconds=2)).isoformat(),
        max_gets=2,
        retries=0,
        execution_authority=False,
    )
    save(control / "outcome-source-pins.json", method)
    reg = dict(
        schema="cf-routed-single-event-registration-v1",
        status="REGISTERED_BEFORE_CAPTURE",
        event_ticker=U.EVENT,
        target_at=U.TARGET.isoformat(),
        execution_authority=False,
        registered_at=(U.START - timedelta(seconds=1)).isoformat(),
        slots=[dict(slot=0, protocol_sha256=S.sha(raw))],
        outcome_source_pins_sha256=S.sha(S.encode(method)),
    )
    save(control / "registration.json", reg)
    save(
        control / "registration.receipt.json",
        dict(
            registration_sha256=S.sha(S.encode(reg)),
            recorded_after_registration=reg["registered_at"],
        ),
    )
    save(
        control / "slot-0.completion-pin.json",
        dict(
            schema="cf-cohort-external-completion-pin-v1",
            slot=0,
            protocol_sha256=S.sha(raw),
            completion_sha256=S.sha((capture / "completion.json").read_bytes()),
            event_ticker=U.EVENT,
            target_at=U.TARGET.isoformat(),
            execution_authority=False,
            capture_path=str(capture),
            observed_at=(U.START + timedelta(seconds=1)).isoformat(),
        ),
    )
    selected = json.loads((capture / "selection.json").read_bytes())["selected"]
    originals = official(catalog, selected, U.TARGET)
    current[0] = U.TARGET + timedelta(minutes=5, seconds=1)
    requests = []

    def transport(url, timeout):
        requests.append(url)
        current[0] += timedelta(milliseconds=100)
        return 200, originals[url.rsplit("/", 1)[-1]][0]

    monkeypatch.setattr(S, "build_decision", lambda *a, **k: pytest.fail("MODEL_RERUN"))
    output = base / "slot-0-official-outcome"

    def collect():
        return C.collect(
            capture / "research.db",
            capture,
            output,
            control=control,
            transport=transport,
            clock=lambda: current[0],
        )

    return collect, base, control, capture, output, current, requests


def test_actual_routed_capture_to_official_and_separate_ui(captured):
    collect, base, control, capture, output, current, requests = captured
    assert collect()["status"] == "SCORED"
    assert len(requests) == 2 and (output / "completion.json").exists()
    view = U.read_single_event(base, control, now=current[0])
    assert view["events"] == 1 and view["decisions"] == 4
    assert view["slots"][0]["outcome"]["status"] == "SCORED_OFFICIAL_RESEARCH"
    html = U.render_single_event(view)
    assert "Separate routed research event" in html and "crypto_v3/settlement_average" in html
    assert "paper eligible 0" in html
    original = (output / "result.json").read_bytes()
    with pytest.raises(FileExistsError):
        collect()
    assert len(requests) == 2 and (output / "result.json").read_bytes() == original
    assert not (output / "failure.json").exists()


@pytest.mark.parametrize("change", ["registration", "receipt_clock", "root_pin", "source", "route"])
def test_tamper_refused_before_any_request(captured, change):
    collect, base, control, capture, output, current, requests = captured
    if change == "route":
        pin = json.loads((capture / "shadow-pins.json").read_bytes())["decisions"][0]
        save(capture / f"route-{pin['decision_id']}.json", {})
    else:
        name = {
            "registration": "registration.json",
            "receipt_clock": "registration.receipt.json",
            "root_pin": "slot-0.completion-pin.json",
            "source": "outcome-source-pins.json",
        }[change]
        path = control / name
        value = json.loads(path.read_bytes())
        if change == "registration":
            value["event_ticker"] = "WRONG"
        elif change == "receipt_clock":
            value["recorded_after_registration"] = U.START.isoformat()
        elif change == "root_pin":
            value["completion_sha256"] = "0" * 64
        else:
            value["source_sha256"]["scripts/cf_average_shadow_outcomes.py"] = "0" * 64
        save(path, value)
    with pytest.raises((ValueError, KeyError)):
        collect()
    assert requests == []
    if change != "source":
        assert U.read_single_event(base, control, now=current[0])["events"] == 0


def test_old_collector_still_refuses_new_target(captured):
    _, _, _, capture, output, current, requests = captured
    with pytest.raises(ValueError, match="EXACT_FIVE_SOL"):
        C._O.collect(
            capture / "research.db",
            capture,
            output,
            completion_sha256=S.sha((capture / "completion.json").read_bytes()),
            transport=lambda *a: pytest.fail("REQUEST"),
            clock=lambda: current[0],
        )
    assert not output.exists()


def test_early_and_late_new_collector_zero_get(captured):
    collect, _, _, _, _, current, requests = captured
    current[0] = U.TARGET + timedelta(minutes=4)
    with pytest.raises(ValueError, match="WINDOW"):
        collect()
    current[0] = U.TARGET + timedelta(minutes=6)
    with pytest.raises(ValueError, match="WINDOW"):
        collect()
    assert requests == []


def test_http_error_originals_retained_without_retry(captured):
    _, _, control, capture, output, current, _ = captured
    calls = []

    def failed(url, timeout):
        calls.append(url)
        return 503, b'{"error":"upstream unavailable"}'

    result = C.collect(
        capture / "research.db",
        capture,
        output,
        control=control,
        transport=failed,
        clock=lambda: current[0],
    )
    assert result["status"] == "PENDING_OR_UNAVAILABLE_NO_RETRY"
    assert len(calls) == 2
    assert (output / "0.original.json").read_bytes() == b'{"error":"upstream unavailable"}'
    assert json.loads((output / "0.receipt.json").read_bytes())["http_status"] == 503


def test_final_output_after_deadline_has_failure_marker(captured, monkeypatch):
    collect, _, _, _, output, current, _ = captured
    original = S.encode

    def encode(value):
        if isinstance(value, dict) and value.get("status") == "SCORED":
            current[0] = U.TARGET + timedelta(minutes=6)
        return original(value)

    monkeypatch.setattr(S, "encode", encode)
    with pytest.raises(ValueError, match="COMPLETION_DEADLINE"):
        collect()
    assert (output / "failure.json").exists()


def test_control_changed_during_actual_evaluation_refuses_publication(captured, monkeypatch):
    collect, _, control, _, output, _, requests = captured
    original = C.E.write_evaluation

    def changed(*args, **kwargs):
        value = original(*args, **kwargs)
        path = control / "registration.receipt.json"
        path.write_bytes(path.read_bytes() + b" ")
        return value

    monkeypatch.setattr(C.E, "write_evaluation", changed)
    with pytest.raises(ValueError, match="PUBLICATION_SOURCE_OR_CAPTURE_CHANGED"):
        collect()
    assert len(requests) == 2 and (output / "failure.json").exists()
    assert not (output / "completion.json").exists()
