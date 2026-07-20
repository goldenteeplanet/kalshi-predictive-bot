from __future__ import annotations

import json
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.ui.evidence_viewer import build_evidence_catalog, get_cached_evidence_catalog
from kalshi_predictor.ui.performance_cache import BoundedSingleFlightCache
from kalshi_predictor.ui.progress import build_progress_dashboard
from kalshi_predictor.ui.progress_history import load_progress_timeline


ROOT = Path(__file__).resolve().parents[1]


def _reports(root: Path, count: int = 25) -> None:
    directory=root/"ui_obs_load"; directory.mkdir(parents=True)
    for index in range(count):
        (directory/f"report-{index:03}.json").write_text(json.dumps({"phase":f"LOAD-{index}","status":"PASSED"}),encoding="utf-8")


def test_single_flight_serves_concurrent_callers_with_one_load() -> None:
    cache=BoundedSingleFlightCache[dict](ttl_seconds=30,max_entries=2)
    calls=0; lock=threading.Lock()
    def loader():
        nonlocal calls
        with lock: calls+=1
        time.sleep(0.05)
        return {"status":"PASSED"}
    with ThreadPoolExecutor(max_workers=16) as pool:
        results=list(pool.map(lambda _:cache.get("same",loader),range(32)))
    assert calls==1
    assert all(result["status"]=="PASSED" for result in results)
    assert cache.metrics()["waits"]>=1


def test_cache_is_bounded_and_returns_stale_on_refresh_error() -> None:
    cache=BoundedSingleFlightCache[int](ttl_seconds=30,max_entries=2)
    assert cache.get("a",lambda:1)==1
    assert cache.get("a",lambda:(_ for _ in ()).throw(RuntimeError()),force=True)==1
    cache.get("b",lambda:2); cache.get("c",lambda:3)
    assert cache.metrics()["entries"]==2
    assert cache.metrics()["stale_fallbacks"]==1


def test_large_history_is_bounded_to_twenty_visible_entries(tmp_path: Path) -> None:
    entries=[{"generated_at":f"2026-07-18T00:{index%60:02}:00Z","process":{},"writer":{},"scheduler":{},"incidents":[]} for index in range(1000)]
    path=tmp_path/"history.json"; path.write_text(json.dumps({"entries":entries}))
    timeline=load_progress_timeline(path)
    assert timeline["count"]==20


def test_partial_data_fails_closed_without_unbounded_work(monkeypatch,tmp_path: Path) -> None:
    monkeypatch.setenv("KALSHI_CERTIFICATION_REPORTS_ROOT",str(tmp_path/"empty"))
    result=build_progress_dashboard(tmp_path/"missing.json")
    assert result["active_process"]["state"]=="BLOCKED"
    assert result["writer"]["safe_to_start_write"] is False
    assert "STATUS_SNAPSHOT_MISSING" in result["diagnostics"]


def test_cached_catalog_has_bounded_memory_and_warm_latency(tmp_path: Path) -> None:
    _reports(tmp_path)
    first=get_cached_evidence_catalog(tmp_path,force=True)
    assert first["count"]==25
    tracemalloc.start(); started=time.perf_counter()
    for _ in range(500): get_cached_evidence_catalog(tmp_path)
    elapsed=time.perf_counter()-started; _,peak=tracemalloc.get_traced_memory(); tracemalloc.stop()
    assert elapsed<1.0
    assert peak<5_000_000


def test_concurrent_read_only_api_sessions_share_cached_catalog(monkeypatch,tmp_path: Path) -> None:
    _reports(tmp_path,5); monkeypatch.setenv("KALSHI_EVIDENCE_ROOT",str(tmp_path))
    engine=init_db(f"sqlite:///{tmp_path/'ui.db'}")
    client=TestClient(create_app(session_factory=get_session_factory(engine),settings=Settings()))
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses=list(pool.map(lambda _:client.get("/api/system/evidence"),range(16)))
    assert all(response.status_code==200 and response.json()["count"]==5 for response in responses)


def test_client_polling_has_bounded_timeout_and_failure_pause() -> None:
    script=(ROOT/"src/kalshi_predictor/ui/static/app.js").read_text(encoding="utf-8")
    assert "new AbortController()" in script
    assert "controller.abort()" in script
    assert "failures >= maxFailures" in script
    assert "POLLING PAUSED" in script
