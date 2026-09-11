"""Real isolated routed harness to bounded display; never rerun a model on read."""

import json
from datetime import timedelta

import pytest
from test_cf_average_routed_capture import routed_harness

from kalshi_predictor.crypto import research_shadow as S
from kalshi_predictor.forecasting import crypto_average_shadow_route as A
from kalshi_predictor.ui import research_journals as R


@pytest.fixture
def routed(tmp_path, monkeypatch):
    run, out, _, _, _, current, plan = routed_harness(tmp_path, monkeypatch)
    run()
    base, control = tmp_path / "cohort", tmp_path / "control"
    base.mkdir()
    control.mkdir()
    capture = out.rename(base / "slot-0")
    plan_raw = (capture / "plan.original.json").read_bytes()
    (control / "slot-0.protocol.json").write_bytes(plan_raw)
    registered = dict(protocol_sha256=R.digest(plan_raw))
    external = dict(
        schema="cf-cohort-external-completion-pin-v1",
        slot=0,
        protocol_sha256=R.digest(plan_raw),
        target_at=plan["target_at"],
        execution_authority=False,
        observed_at=(current[0] + timedelta(seconds=1)).isoformat(),
    )

    def bind():
        completion = json.loads((capture / "completion.json").read_bytes())
        for name in completion["files"]:
            if (capture / name).exists():
                completion["files"][name] = R.digest((capture / name).read_bytes())
        raw = S.encode(completion)
        (capture / "completion.json").write_bytes(raw)
        external["completion_sha256"] = R.digest(raw)
        (control / "slot-0.completion-pin.json").write_bytes(S.encode(external))

    bind()
    monkeypatch.setattr(S, "build_decision", lambda *a, **k: pytest.fail("MODEL_RERUN"))
    monkeypatch.setattr(A, "route_sources", lambda: pytest.fail("CURRENT_SOURCE_SUBSTITUTED"))
    return (
        (lambda: R.slot_view(base, control, 0, registered, current[0] + timedelta(seconds=2))),
        capture,
        bind,
    )


def test_real_v2_route_display_preserves_four_rows_one_event(routed):
    view, _, _ = routed
    result = view()
    assert result["status"] == "COMPLETE_PIN_BOUND_DISPLAY"
    assert len(result["rows"]) == 4
    assert all(
        row["research_route"] == A.ROUTE and len(row["route_sha256"]) == 64
        for row in result["rows"]
    )
    html = R.render_cohort(dict(slots=[result], decisions=4, events=1))
    assert "Verified research route: crypto_v3/settlement_average" in html
    assert "paper eligible 0" in html


@pytest.mark.parametrize("change", ["missing", "tampered", "rehashed_role", "rehashed_source"])
def test_invalid_route_or_unanchored_source_never_counts(routed, change):
    view, capture, bind = routed
    pins = json.loads((capture / "shadow-pins.json").read_bytes())
    pin = pins["decisions"][0]
    path = capture / f"route-{pin['decision_id']}.json"
    if change == "missing":
        path.unlink()
    elif change == "tampered":
        path.write_bytes(b"{}")
    elif change == "rehashed_role":
        value = json.loads(path.read_bytes())
        value["model_role"] = "PAPER_APPROVED"
        raw = S.encode(value)
        path.write_bytes(raw)
        pin["route_sha256"] = R.digest(raw)
        completion_path = capture / f"route_completion-{pin['decision_id']}.json"
        receipt = json.loads(completion_path.read_bytes())
        receipt["route_sha256"] = R.digest(raw)
        completion_path.write_bytes(S.encode(receipt))
        pin["route_completion_sha256"] = R.digest(completion_path.read_bytes())
        (capture / "shadow-pins.json").write_bytes(S.encode(pins))
        recorded = json.loads((capture / "shadow-pins.recorded.json").read_bytes())
        recorded["sha256"] = R.digest((capture / "shadow-pins.json").read_bytes())
        (capture / "shadow-pins.recorded.json").write_bytes(S.encode(recorded))
        bind()
    else:
        path = capture / "source.originals.json"
        proof = json.loads(path.read_bytes())
        proof["route.model_roles"] = dict(payload_hex=b"forged".hex(), sha256=R.digest(b"forged"))
        path.write_bytes(S.encode(proof))
        bind()
    result = view()
    assert result["status"] == "UNVERIFIED" and result["rows"] == []
