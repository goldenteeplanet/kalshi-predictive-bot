import importlib.util
import json
from datetime import timedelta
from pathlib import Path

import pytest
from test_cf_average_shadow_capture import harness

from kalshi_predictor.crypto import research_shadow as S

SPEC = importlib.util.spec_from_file_location(
    "outcomes", Path(__file__).parents[1] / "scripts/cf_average_shadow_outcomes.py"
)
C = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(C)


def case(tmp_path, monkeypatch):
    run, out, _, catalog, _, _, plan = harness(tmp_path, monkeypatch)
    run()
    target = S.at(plan["target_at"])
    monkeypatch.setattr(C, "TARGETS", (target.isoformat(),))
    current = [target + timedelta(minutes=5)]
    monkeypatch.setattr(S, "now", lambda: current[0])
    calls = []
    final = [False]

    def get(url, timeout):
        calls.append(url)
        row = next(r for r in catalog["markets"] if r["ticker"] == url.rsplit("/", 1)[-1])
        body = row | dict(status="closed")
        if final[0]:
            body.update(
                status="finalized",
                result="no",
                is_provisional=False,
                settlement_ts=(target + timedelta(minutes=1)).isoformat(),
                settlement_value_dollars="0.00",
            )
        return 200, S.encode(dict(market=body))

    def collect():
        return C.collect(
            out / "research.db",
            out,
            out.with_name(out.name + "-official-outcome"),
            completion_sha256=S.sha((out / "completion.json").read_bytes()),
            transport=get,
            clock=lambda: current[0],
        )

    return collect, out, calls, current, final


def test_nonfinal_archived_two_gets_no_retry(tmp_path, monkeypatch):
    collect, out, calls, _, _ = case(tmp_path, monkeypatch)
    result = collect()
    assert len(calls) == 2 and result["status"] == "PENDING_OR_UNAVAILABLE_NO_RETRY"
    saved = out.with_name(out.name + "-official-outcome")
    assert json.loads((saved / "0.original.json").read_bytes())["market"]["status"] == "closed"
    with pytest.raises(FileExistsError):
        collect()
    assert len(calls) == 2


def test_early_and_tampered_capture_zero_get(tmp_path, monkeypatch):
    collect, out, calls, current, _ = case(tmp_path, monkeypatch)
    current[0] -= timedelta(seconds=1)
    with pytest.raises(ValueError, match="WINDOW"):
        collect()
    assert not calls
    current[0] += timedelta(seconds=1)
    (out / "failure.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="FAILED_CAPTURE"):
        collect()
    assert not calls


def test_actual_final_two_gets_evaluator_no_model_rerun(tmp_path, monkeypatch):
    collect, _, calls, _, final = case(tmp_path, monkeypatch)
    final[0] = True
    monkeypatch.setattr(S, "build_decision", lambda *a, **k: pytest.fail("MODEL_RERUN"))
    assert collect()["status"] == "SCORED"
    assert len(calls) == 2


def test_clock_rollback_after_request_reservation_zero_get(tmp_path, monkeypatch):
    collect, _, calls, current, _ = case(tmp_path, monkeypatch)
    original = C.os.fsync
    n = 0

    def fsync(fd):
        nonlocal n
        n += 1
        original(fd)
        # reservation + two source originals + first request reservation.
        if n == 4:
            current[0] -= timedelta(microseconds=1)

    monkeypatch.setattr(C.os, "fsync", fsync)
    with pytest.raises(ValueError, match="ROLLBACK"):
        collect()
    assert not calls


@pytest.mark.parametrize("http_error", [False, True])
def test_public_get_mocked_read1_socket_and_http_error(monkeypatch, http_error):
    from types import SimpleNamespace
    from urllib.error import HTTPError

    deadlines = []

    class Response:
        status = 200

        def __init__(self):
            self.fp = SimpleNamespace(
                raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=deadlines.append))
            )

        def read1(self, size):
            assert 0 < size <= 65536
            self.fp = None
            return b"actual mocked body"

        def close(self):
            self.fp = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    response = Response()
    calls = []

    def open_url(url, timeout):
        calls.append((url, timeout))
        if http_error:
            raise HTTPError(url, 503, "unavailable", {}, response)
        return response

    monkeypatch.setattr(C, "build_opener", lambda *handlers: SimpleNamespace(open=open_url))
    monkeypatch.setattr(C.time, "monotonic", lambda: 100)
    status, body = C.public_get("https://external-api.kalshi.com/trade-api/v2/markets/M", 10)
    assert status == (503 if http_error else 200) and body == b"actual mocked body"
    assert deadlines == [10] and len(calls) == 1
