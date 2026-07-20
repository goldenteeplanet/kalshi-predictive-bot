from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from kalshi_predictor.ui.cloud_status_adapter import adapt_cloud_status_bundle
from kalshi_predictor.ui.progress_history import history_path_for, record_progress_snapshot


REQUIRED_SOURCES = (
    "db_writer_monitor", "db_locks", "backup_report", "scheduler", "execution", "process"
)


@dataclass(frozen=True)
class SyntheticSourceResult:
    captured_at: str
    payload: Mapping[str, Any]


SourceRunner = Callable[[str, int], SyntheticSourceResult]
OwnerAlive = Callable[[int], bool]


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(UTC)


def _recover_stale_lock(
    lock_path: Path, *, now: datetime, stale_after_seconds: int, owner_alive: OwnerAlive,
) -> bool:
    if not lock_path.exists():
        return False
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        age = (now - _parse(lock["started_at"])).total_seconds()
        pid = int(lock["pid"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False
    if age <= stale_after_seconds or owner_alive(pid):
        return False
    lock_path.unlink()
    return True


def _acquire_lock(lock_path: Path, *, pid: int, now: datetime) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"pid": pid, "started_at": now.isoformat().replace("+00:00", "Z")}, handle)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_publish(payload: Mapping[str, Any], destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
    return hashlib.sha256(serialized.encode()).hexdigest()


def run_status_collector(
    spec: Mapping[str, Any], runner: SourceRunner, destination: Path, *,
    now: datetime, pid: int, owner_alive: OwnerAlive = lambda _pid: True,
    source_timeout_seconds: int = 5, stale_lock_seconds: int = 120,
) -> dict[str, Any]:
    lock_path = destination.with_suffix(destination.suffix + ".collector.lock")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    recovered_lock = _recover_stale_lock(
        lock_path, now=now, stale_after_seconds=stale_lock_seconds, owner_alive=owner_alive
    )
    if lock_path.exists():
        return {
            "phase": "UI-OBS-2B", "status": "BLOCKED", "published": False,
            "diagnostics": ["COLLECTOR_OVERLAP_BLOCKED"], "recovered_stale_lock": False,
        }
    if temporary.exists():
        temporary.unlink()
    _acquire_lock(lock_path, pid=pid, now=now)
    diagnostics: list[str] = []
    sources: dict[str, Any] = {}
    timestamps: dict[str, str] = {}
    try:
        for name in REQUIRED_SOURCES:
            try:
                result = runner(name, source_timeout_seconds)
            except TimeoutError:
                diagnostics.append(f"SOURCE_TIMEOUT:{name}")
                continue
            except Exception as exc:  # noqa: BLE001 - collector must isolate source failure.
                diagnostics.append(f"SOURCE_FAILURE:{name}:{type(exc).__name__}")
                continue
            sources[name] = dict(result.payload)
            timestamps[name] = result.captured_at
        missing = [name for name in REQUIRED_SOURCES if name not in sources]
        diagnostics.extend(f"SOURCE_MISSING:{name}" for name in missing)
        adapter = None
        if not diagnostics:
            bundle = {
                "collected_at": spec["collected_at"],
                "source_timestamps": timestamps,
                "sources": sources,
                "alerts": list(spec.get("alerts") or []),
                "reports": list(spec.get("reports") or []),
                "workstreams": list(spec.get("workstreams") or []),
            }
            adapter = adapt_cloud_status_bundle(bundle)
            diagnostics.extend(adapter["diagnostics"])
        published = not diagnostics and adapter is not None and adapter["adapter_passed"]
        digest = _atomic_publish(adapter["snapshot"], destination) if published else None
        history = record_progress_snapshot(adapter["snapshot"], history_path_for(destination)) if published else None
        return {
            "phase": "UI-OBS-2B",
            "mode": "LOCAL_SYNTHETIC_READ_ONLY_COLLECTOR_RESILIENCE_PREVIEW",
            "status": "PASSED" if published else "FAILED",
            "published": published,
            "destination": str(destination),
            "snapshot_sha256": digest,
            "history_appended": history["appended"] if history else False,
            "history_entries": len(history["entries"]) if history else 0,
            "sources_collected": sorted(sources),
            "diagnostics": diagnostics,
            "recovered_stale_lock": recovered_lock,
            "atomic_temporary_absent": not temporary.exists(),
            "database_access": False,
            "database_writes": 0,
            "cloud_access": False,
            "execution_changed": False,
        }
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


class ScriptedSyntheticRunner:
    def __init__(self, results: Mapping[str, Mapping[str, Any]]) -> None:
        self.results = results

    def __call__(self, name: str, timeout_seconds: int) -> SyntheticSourceResult:
        del timeout_seconds
        result = self.results[name]
        behavior = result.get("behavior")
        if behavior == "timeout":
            raise TimeoutError(name)
        if behavior == "failure":
            raise RuntimeError(name)
        return SyntheticSourceResult(
            captured_at=str(result["captured_at"]), payload=result["payload"]
        )


def write_collector_resilience_preview(fixture_path: Path, output_dir: Path) -> Path:
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if fixture.get("bundle_path"):
        bundle_path = (fixture_path.parent / fixture["bundle_path"]).resolve()
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        fixture["spec"] = {
            "collected_at": bundle["collected_at"], "alerts": bundle.get("alerts", []),
            "reports": bundle.get("reports", []), "workstreams": bundle.get("workstreams", []),
        }
        fixture["source_results"] = {
            name: {"captured_at": bundle["source_timestamps"].get(name, bundle["collected_at"]), "payload": payload}
            for name, payload in bundle["sources"].items()
        }
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot = output_dir / "ui_obs2b_published_progress_snapshot.json"
    result = run_status_collector(
        fixture["spec"], ScriptedSyntheticRunner(fixture["source_results"]), snapshot,
        now=_parse(fixture["now"]), pid=int(fixture["pid"]), owner_alive=lambda _pid: False,
    )
    result["destination"] = snapshot.name
    report_path = output_dir / "ui_obs2b_collector_resilience_preview.json"
    report_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report_path
