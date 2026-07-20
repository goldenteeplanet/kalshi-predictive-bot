from __future__ import annotations

import json
import time
import tracemalloc
from pathlib import Path

import pytest

from kalshi_predictor import cli, phase3bc
from kalshi_predictor.phase3bc_r5 import _StageTimer as R5StageTimer
from kalshi_predictor.runtime_stage_heartbeat import AtomicStageHeartbeat


def test_atomic_stage_heartbeat_advances_during_long_stage(tmp_path: Path) -> None:
    path = tmp_path / "stage.json"
    heartbeat = AtomicStageHeartbeat(
        path,
        phase="TEST",
        interval_seconds=0.05,
        metadata={"live_or_demo_execution": False},
    )
    heartbeat.mark("long_query")
    first = json.loads(path.read_text(encoding="utf-8"))
    time.sleep(0.13)
    second = json.loads(path.read_text(encoding="utf-8"))
    heartbeat.mark("complete")

    assert second["heartbeat_sequence"] > first["heartbeat_sequence"]
    assert second["event"] == "heartbeat"
    assert second["stage"] == "long_query"
    assert second["stage_started_at"] == first["stage_started_at"]
    assert second["live_or_demo_execution"] is False
    assert not list(tmp_path.glob("*.tmp"))


def test_r5_heartbeat_records_exact_cycle_identity(tmp_path: Path) -> None:
    timer = R5StageTimer(tmp_path, cycle_number=4, total_cycles=32)
    timer.mark("phase3bc_r3_refresh")
    timer.mark("complete")

    payload = json.loads(
        (tmp_path / "phase3bc_r5_heartbeat.json").read_text(encoding="utf-8")
    )
    assert payload["cycle_number"] == 4
    assert payload["total_cycles"] == 32
    assert payload["pid"] > 0
    assert payload["stage"] == "complete"
    assert payload["live_or_demo_execution"] is False


def test_router_ticker_batches_are_bounded_exact_and_deduplicated() -> None:
    tickers = [f"TICKER-{index:04d}" for index in range(205)] + ["TICKER-0001"]
    batches = phase3bc._ticker_batches(tickers)

    assert [len(batch) for batch in batches] == [100, 100, 5]
    assert [ticker for batch in batches for ticker in batch] == tickers[:-1]
    assert all(len(batch) <= phase3bc.ROUTER_QUERY_BATCH_SIZE for batch in batches)


def test_router_ticker_batches_reject_unbounded_zero_size() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        phase3bc._ticker_batches(["A"], batch_size=0)


def test_batch_planning_has_bounded_memory_and_runtime() -> None:
    tickers = [f"TICKER-{index:06d}" for index in range(50_000)]
    tracemalloc.start()
    started = time.perf_counter()
    batches = phase3bc._ticker_batches(tickers)
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(batches) == 500
    assert max(map(len, batches)) == phase3bc.ROUTER_QUERY_BATCH_SIZE
    assert elapsed < 2.0
    assert peak < 16 * 1024 * 1024


def test_cycle_resource_release_clears_session_and_engine(monkeypatch) -> None:
    calls: list[str] = []

    class Session:
        def expunge_all(self) -> None:
            calls.append("expunge_all")

    class Engine:
        def dispose(self) -> None:
            calls.append("dispose")

    monkeypatch.setattr(cli.gc, "collect", lambda: calls.append("gc_collect"))
    cli._release_phase3bc_r5_cycle_resources(Session(), Engine())

    assert calls[:3] == ["expunge_all", "dispose", "gc_collect"]


def test_one_cycle_service_preview_is_inert_and_bounded() -> None:
    path = Path("deploy/systemd/kalshi-r5-watcher-one-cycle.service.preview")
    text = path.read_text(encoding="utf-8")

    assert path.suffix == ".preview"
    assert "--cycles 1" in text
    assert "--interval-minutes 0" in text
    assert "MemoryHigh=1800M" in text
    assert "MemoryMax=2200M" in text
    assert "Restart=no" in text
    assert "EXECUTION_ENABLED=false" in text
    assert "[Install]" not in text
