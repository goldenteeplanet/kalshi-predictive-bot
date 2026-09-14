import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.data.schema import Forecast, MarketSnapshot, MicrostructureFeature
from kalshi_predictor.microstructure import research_capture as capture


@pytest.fixture
def harness(tmp_path, monkeypatch):
    clock_value = [datetime(2026, 9, 10, 23, tzinfo=UTC)]
    elapsed = [0.0]
    urls = []
    repo = Path(__file__).resolve().parents[1]

    def clock():
        clock_value[0] += timedelta(milliseconds=1)
        return clock_value[0]

    def sleep(seconds):
        elapsed[0] += seconds
        clock_value[0] += timedelta(seconds=seconds)

    def freeze(repo, output, paths):
        output.mkdir()
        for relative in paths:
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((repo / relative).read_bytes())
        return {
            "source_commit": "fixture-only",
            "commit_recorded_at": "2026-09-10T22:00:00+00:00",
            "files": [
                {"path": p, "sha256": hashlib.sha256((repo / p).read_bytes()).hexdigest()}
                for p in paths
            ],
        }

    monkeypatch.setattr(capture, "freeze_code", freeze)
    monkeypatch.setattr("kalshi_predictor.microstructure.orderbook_features.utc_now", clock)
    monkeypatch.setattr("kalshi_predictor.forecasting.microstructure_v1.utc_now", clock)

    def forbidden(*args, **kwargs):
        raise AssertionError("canonical database access")

    monkeypatch.setattr("kalshi_predictor.data.db.get_session_factory", forbidden)
    monkeypatch.setattr("kalshi_predictor.data.repositories.get_session_factory", forbidden)

    def get(url):
        urls.append(url)
        if "/orderbook?" in url:
            value = {"orderbook_fp": {"yes_dollars": [["0.4", "12"]], "no_dollars": [["0.5", "8"]]}}
        else:
            value = {
                "market": {
                    "ticker": "KXSOLE-FUTURE",
                    "status": "open",
                    "close_time": "2026-09-11T00:00:00Z",
                    "title": "SOL future",
                    "yes_bid_dollars": "0.4",
                    "yes_ask_dollars": "0.5",
                }
            }
        return json.dumps(value).encode(), 200

    kwargs = {
        "repo": repo,
        "output": tmp_path / "capture",
        "ticker": "KXSOLE-FUTURE",
        "settings": Settings(),
        "get": get,
        "clock": clock,
        "monotonic": lambda: elapsed[0],
        "sleep": sleep,
    }
    return kwargs, urls, clock_value, elapsed


def test_actual_models_real_persisted_ids_and_clock_chain(harness):
    kwargs, urls, _, _ = harness
    result = capture.run(**kwargs)
    assert len(urls) == 6 and result["requests"] == 6
    assert result["scope"] == "MARKET_ONLY_ENSEMBLE_RESEARCH"
    assert result["market_implied_probability"] == result["ensemble_probability"] == "0.45"
    assert not result["execution_authority"] and not result["independent_alpha"]
    output = kwargs["output"]
    with Session(create_engine("sqlite:///" + str(output / "research.sqlite"))) as session:
        snapshots = list(session.scalars(select(MarketSnapshot)))
        forecasts = list(session.scalars(select(Forecast)))
        features = list(session.scalars(select(MicrostructureFeature)))
        assert len(snapshots) == 3 and len({s.id for s in snapshots}) == 3
        assert {f.model_name for f in forecasts} == {"market_implied_v1", "ensemble_v2"}
        assert len(features) == 1
    decision = json.loads((output / "frozen/decision.json").read_bytes())
    prediction = json.loads((output / "frozen/prediction.json").read_bytes())
    assert (
        hashlib.sha256((output / "frozen/prediction.json").read_bytes()).hexdigest()
        == decision["prediction_sha256"]
    )
    assert (
        decision["input_received_at"]
        <= decision["model_input_as_of"]
        <= decision["prediction_recorded_at"]
        <= decision["decision_at"]
        < prediction["target_at"]
    )
    for index in range(3):
        for kind in ("market", "book"):
            path = output / f"sample-{index}-{kind}.json"
            receipt = json.loads((output / (path.name + ".receipt.json")).read_bytes())
            assert hashlib.sha256(path.read_bytes()).hexdigest() == receipt["sha256"]
            assert receipt["received_at"] <= receipt["recorded_at"]
    assert json.loads((output / "weights.json").read_bytes())["rows"] == []


def test_wrong_ticker_aborts_before_book(harness):
    kwargs, urls, _, _ = harness
    original = kwargs["get"]

    def bad(url):
        raw, status = original(url)
        value = json.loads(raw)
        value["market"]["ticker"] = "WRONG"
        return json.dumps(value).encode(), status

    kwargs["get"] = bad
    with pytest.raises(ValueError, match="EXACT_MARKET"):
        capture.run(**kwargs)
    assert len(urls) == 1 and not (kwargs["output"] / "frozen").exists()


def test_expiry_during_acquisition_aborts(harness):
    kwargs, urls, clock, _ = harness
    original = kwargs["get"]

    def expired(url):
        raw, status = original(url)
        if len(urls) == 2:
            clock[0] = datetime(2026, 9, 11, 0, tzinfo=UTC)
        return raw, status

    kwargs["get"] = expired
    with pytest.raises(ValueError, match="TARGET_EXPIRED"):
        capture.run(**kwargs)
    assert len(urls) == 2 and not (kwargs["output"] / "frozen").exists()


def test_failed_get_not_retried(harness):
    kwargs, urls, _, _ = harness

    def fail(url):
        urls.append(url)
        raise TimeoutError("bounded timeout")

    kwargs["get"] = fail
    with pytest.raises(TimeoutError):
        capture.run(**kwargs)
    assert len(urls) == 1
    assert json.loads((kwargs["output"] / "failure.json").read_bytes())["requests"] == 1


def test_clock_regression_aborts(harness):
    kwargs, urls, clock, _ = harness
    original = kwargs["get"]

    def regress(url):
        value = original(url)
        clock[0] -= timedelta(seconds=30)
        return value

    kwargs["get"] = regress
    with pytest.raises(ValueError, match="CLOCK_REGRESSION"):
        capture.run(**kwargs)
    assert len(urls) == 1


def test_source_change_aborts_before_prediction(harness, monkeypatch):
    kwargs, _, _, _ = harness
    verify = capture.verify_unchanged
    count = [0]

    def changed(repo, proof):
        count[0] += 1
        if count[0] > 1:
            raise ValueError("MODEL_CHANGED_DURING_CAPTURE")
        verify(repo, proof)

    monkeypatch.setattr(capture, "verify_unchanged", changed)
    with pytest.raises(ValueError, match="MODEL_CHANGED"):
        capture.run(**kwargs)
    assert not (kwargs["output"] / "frozen").exists()


def test_no_shortcut_for_configured_quorum(harness):
    kwargs, urls, _, _ = harness
    kwargs["settings"].microstructure_min_snapshots = 4
    with pytest.raises(ValueError, match="QUORUM"):
        capture.run(**kwargs)
    assert not urls


def test_missing_naive_close_rejected(harness):
    kwargs, urls, _, _ = harness
    original = kwargs["get"]

    def bad(url):
        raw, status = original(url)
        value = json.loads(raw)
        value["market"]["close_time"] = "2026-09-11T00:00:00"
        return json.dumps(value).encode(), status

    kwargs["get"] = bad
    with pytest.raises(ValueError, match="AWARE"):
        capture.run(**kwargs)
    assert len(urls) == 1


def test_displaced_imported_module_rejected_before_get(harness, monkeypatch):
    from kalshi_predictor.forecasting import ensemble_v2

    kwargs, urls, _, _ = harness
    monkeypatch.setattr(
        ensemble_v2, "__file__", str(kwargs["repo"] / "src/kalshi_predictor/config.py")
    )
    with pytest.raises(ValueError, match="MODULE_IDENTITY"):
        capture.run(**kwargs)
    assert not urls


def test_original_tamper_rejected_before_prediction(harness):
    kwargs, _, _, _ = harness
    original = kwargs["sleep"]

    def tamper(seconds):
        original(seconds)
        (kwargs["output"] / "sample-0-book.json").write_bytes(b"{}")

    kwargs["sleep"] = tamper
    with pytest.raises(ValueError, match="ORIGINAL_HASH"):
        capture.run(**kwargs)
    assert not (kwargs["output"] / "frozen").exists()


def test_record_disk_failure_never_produces_decision(harness, monkeypatch):
    kwargs, urls, _, _ = harness
    original = capture.write_new

    def fail(path, raw):
        if path.name == "snapshot-0.json":
            raise OSError("disk full")
        return original(path, raw)

    monkeypatch.setattr(capture, "write_new", fail)
    with pytest.raises(OSError, match="disk full"):
        capture.run(**kwargs)
    assert len(urls) == 2 and not (kwargs["output"] / "frozen").exists()


def test_time_budget_failure_no_retry(harness):
    kwargs, urls, _, elapsed = harness
    original = kwargs["get"]

    def slow(url):
        result = original(url)
        elapsed[0] = 91
        return result

    kwargs["get"] = slow
    with pytest.raises(ValueError, match="TIME_CAP"):
        capture.run(**kwargs)
    assert len(urls) == 1
