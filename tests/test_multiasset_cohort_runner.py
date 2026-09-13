import importlib.util
import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
from test_multiasset_capture import NOW, setup

from kalshi_predictor.crypto import multiasset_capture as C
from kalshi_predictor.crypto.multiasset_outcomes import verify_capture

path = Path(__file__).parents[1] / "scripts/multiasset_cohort_runner.py"
spec = importlib.util.spec_from_file_location("cohort_runner", path)
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def test_fixed_get_surface_rejects_other_events_and_orders():
    plan = {"event": "KXSOLE-26SEP1123", "benchmark": "SOLUSD_RTI"}
    assert R.allowed_url(C.BASE + "/cfbenchmarks/values?id=SOLUSD_RTI", plan, "capture")
    assert R.allowed_url(
        C.BASE + "/markets/KXSOLE-26SEP1123-B100/orderbook?depth=10", plan, "capture"
    )
    assert not R.allowed_url(C.BASE + "/portfolio/orders", plan, "capture")
    assert not R.allowed_url(C.BASE + "/markets/KXSOLE-26SEP1124-B100", plan, "outcome")
    assert not R.allowed_url(C.BASE + "/markets/KXSOLE-26SEP1123-B100?x=1", plan, "outcome")
    assert not R.allowed_url("https://example.com/markets/KXSOLE-26SEP1123-B100", plan, "outcome")


def test_root_pin_and_registration_binding(tmp_path, monkeypatch):
    plan, transport, _ = setup()
    root = tmp_path / "data"
    root.mkdir()
    capture = root / "slot-0"
    C.capture(capture, plan, transport, clock=lambda: NOW)
    control = tmp_path / "control"
    control.mkdir(mode=0o755)
    # Model root-owned fixture metadata without requiring the test runner to be root.
    original_stat = Path.stat

    def root_control_stat(path, *args, **kwargs):
        actual = original_stat(path, *args, **kwargs)
        if path == control or control in path.parents:
            fields = list(actual)
            fields[4] = 0
            return os.stat_result(fields)
        return actual

    monkeypatch.setattr(Path, "stat", root_control_stat)
    monkeypatch.setattr(R.os, "geteuid", lambda: 0, raising=False)
    raw = C.encode(plan)
    (control / "slot-0.json").write_bytes(raw)
    item = {"slot": 0, "plan_sha256": C.digest(raw), "capture_path": str(capture)}
    registration = {
        "registered_at": (NOW - timedelta(hours=1)).isoformat(),
        "slots": [item],
        "documents": {},
        "data_root": str(root),
    }
    (control / "registration.json").write_bytes(C.encode(registration))

    class Clock:
        @staticmethod
        def now(tz):
            return NOW + timedelta(seconds=2)

    monkeypatch.setattr(R, "datetime", Clock)
    loaded, _, _ = R.registered(control, 0)
    R.pin_capture(control, 0, loaded, item)
    pin = (control / "slot-0.pin.json").read_bytes()
    receipt = (control / "slot-0.pin-receipt.json").read_bytes()
    assert len(verify_capture(capture, pin, receipt, item["plan_sha256"])[1]) == 4
    with pytest.raises(FileExistsError):
        R.pin_capture(control, 0, loaded, item)
    changed = json.loads(raw)
    changed["seed"] += 1
    (control / "slot-0.json").write_bytes(C.encode(changed))
    with pytest.raises(ValueError, match="HASH"):
        R.registered(control, 0)


def test_nonroot_process_cannot_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(R.os, "geteuid", lambda: 1000, raising=False)
    with pytest.raises(ValueError, match="ROOT_PIN_PROCESS_REQUIRED"):
        R.pin_capture(tmp_path, 0, {}, {})


@pytest.mark.parametrize("uid,mode", [(1000, 0o755), (0, 0o777), (0, 0o775)])
def test_untrusted_control_is_rejected(tmp_path, monkeypatch, uid, mode):
    original_stat = Path.stat

    def untrusted_stat(path, *args, **kwargs):
        fields = list(original_stat(path, *args, **kwargs))
        if path == tmp_path:
            fields[4] = uid
            fields[0] = (fields[0] & ~0o777) | mode
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "stat", untrusted_stat)
    with pytest.raises(ValueError, match="TRUSTED_CONTROL_DIRECTORY_REQUIRED"):
        R.registered(tmp_path, 0)
