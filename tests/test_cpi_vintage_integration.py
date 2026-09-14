import json
import logging

import httpx
import pytest
from typer.testing import CliRunner

from kalshi_predictor.economic.cpi_vintages import capture_cpi_vintage_pair
from kalshi_predictor.research.fred import FREDError, FREDResearchClient

KEY = "a" * 32
WINDOW = {"observation_start": "2026-07-01", "observation_end": "2026-08-01",
          "as_of": "2026-09-12"}


def body(mode):
    return {"output_type": mode, "units": "lin", "count": 2, "offset": 0,
            "observations": [
                {"date": "2026-07-01", "value": "300" if mode == 4 else "301",
                 "realtime_start": "2026-08-12", "realtime_end": "9999-12-31"},
                {"date": "2026-08-01", "value": ".",
                 "realtime_start": "2026-09-11", "realtime_end": "9999-12-31"}]}


@pytest.fixture(autouse=True)
def no_pacing_wait(monkeypatch):
    monkeypatch.setattr("kalshi_predictor.research.fred.time.sleep", lambda _: None)


def test_real_client_pair_preserves_distinct_vintages_and_no_key(tmp_path, caplog):
    calls = []

    def handler(request):
        calls.append(request.url.params)
        assert request.url.params["api_key"] == KEY
        return httpx.Response(200, json=body(int(request.url.params["output_type"])))

    caplog.set_level(logging.INFO, logger="httpx")
    out = tmp_path / "vintages"
    with FREDResearchClient(
        KEY, request_budget=2, transport=httpx.MockTransport(handler)
    ) as client:
        report = capture_cpi_vintage_pair(client, out, **WINDOW)
        assert client.remaining_requests == 0
    assert report["status"] == "CAPTURED_NOT_BLS_RELEASE_CERTIFICATION"
    assert [p["output_type"] for p in calls] == ["4", "1"]
    assert calls[0]["realtime_start"] == "1776-07-04"
    assert calls[1]["realtime_start"] == calls[1]["realtime_end"] == WINDOW["as_of"]
    initial = json.loads((out / "initial.normalized.json").read_text())
    revised = json.loads((out / "asof.normalized.json").read_text())
    assert initial[0]["value"] == "300" and revised[0]["value"] == "301"
    assert initial[1]["value"] is None
    assert initial[0]["revision_status"] == "INITIAL_IN_FRED_HISTORY"
    assert revised[0]["revision_status"] == "REVISION_STATUS_NOT_INFERRED"
    for row in initial + revised:
        assert row["release_timestamp"] is None
        assert row["available_at"] == row["received_at"]
        assert not row["bls_original_first_release_certified"]
    assert not report["paper_authority"] and not report["forecast_mutated"]
    assert KEY not in caplog.text
    assert all(KEY.encode() not in p.read_bytes() for p in out.iterdir())
    with pytest.raises(FileExistsError):
        capture_cpi_vintage_pair(client, out, **WINDOW)
    assert len(calls) == 2


@pytest.mark.parametrize("defect", ["partial", "mode", "duplicate", "nonfinite", "future"])
def test_rejected_original_is_retained_without_second_request(tmp_path, defect):
    calls = []

    def handler(request):
        calls.append(request)
        data = body(4)
        if defect == "partial":
            data["count"] = 3
        elif defect == "mode":
            data["output_type"] = 1
        elif defect == "duplicate":
            data["observations"][1]["date"] = "2026-07-01"
        elif defect == "nonfinite":
            data["observations"][0]["value"] = "NaN"
        else:
            data["observations"][0]["realtime_start"] = "2027-01-01"
        return httpx.Response(200, json=data)

    out = tmp_path / "rejected"
    with FREDResearchClient(KEY, transport=httpx.MockTransport(handler)) as client:
        report = capture_cpi_vintage_pair(client, out, **WINDOW)
    assert report["status"] == "INCOMPLETE" and len(calls) == 1
    assert (out / "initial.original.json").exists()
    assert not (out / "initial.normalized.json").exists()
    assert not (out / "asof.original.json").exists()


def test_authorization_failure_is_bounded_and_redacted(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403, text=KEY)

    out = tmp_path / "denied"
    with FREDResearchClient(KEY, transport=httpx.MockTransport(handler)) as client:
        report = capture_cpi_vintage_pair(client, out, **WINDOW)
    assert len(calls) == 1 and report["status"] == "INCOMPLETE"
    assert all(KEY.encode() not in p.read_bytes() for p in out.iterdir())


@pytest.mark.parametrize("mode", [True, 2, 3, "4"])
def test_invalid_modes_do_not_use_quota(mode):
    with FREDResearchClient(KEY, transport=httpx.MockTransport(lambda _: pytest.fail("HTTP"))) as c:
        with pytest.raises(FREDError):
            c.cpi_vintage(**WINDOW, output_type=mode)
        assert c.remaining_requests == 10


def test_application_command_missing_key_does_not_create_output(tmp_path, monkeypatch):
    from kalshi_predictor.cli import app

    monkeypatch.delenv("FRED_API_KEY", raising=False)
    out = tmp_path / "missing"
    result = CliRunner().invoke(app, ["capture-cpi-vintages", str(out), *WINDOW.values()])
    assert result.exit_code == 1 and "FRED_API_KEY_NOT_CONFIGURED" in result.stdout
    assert not out.exists()


def test_application_command_uses_bounded_real_client_without_database(tmp_path, monkeypatch):
    from kalshi_predictor import cli

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=body(int(request.url.params["output_type"])))

    def factory(key, *, request_budget):
        assert key == KEY and request_budget == 2
        return FREDResearchClient(key, request_budget=2, transport=httpx.MockTransport(handler))

    monkeypatch.setenv("FRED_API_KEY", KEY)
    monkeypatch.setattr("kalshi_predictor.research.fred.FREDResearchClient", factory)
    monkeypatch.setattr(cli, "init_db", lambda: pytest.fail("DATABASE_ACCESS"))
    out = tmp_path / "command"
    result = CliRunner().invoke(cli.app, ["capture-cpi-vintages", str(out), *WINDOW.values()])
    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    assert "CAPTURED_NOT_BLS_RELEASE_CERTIFICATION" in result.stdout
    assert KEY not in result.stdout
    assert (out / "initial.original.json").exists() and (out / "asof.original.json").exists()


def test_expired_as_of_vintage_keeps_initial_original_and_marks_pair_incomplete(tmp_path):
    def handler(request):
        mode = int(request.url.params["output_type"])
        data = body(mode)
        if mode == 1:
            data["observations"][0]["realtime_end"] = "2026-09-01"
        return httpx.Response(200, json=data)

    out = tmp_path / "expired"
    with FREDResearchClient(KEY, request_budget=2, transport=httpx.MockTransport(handler)) as c:
        result = capture_cpi_vintage_pair(c, out, **WINDOW)
    assert result["status"] == "INCOMPLETE" and result["attempted_modes"] == 2
    assert (out / "initial.normalized.json").exists()
    assert (out / "asof.original.json").exists()
    assert not (out / "asof.normalized.json").exists()
