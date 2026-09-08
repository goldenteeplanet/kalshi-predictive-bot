"""Stress atomic snapshot publication inside an internally disposable directory."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

ARTIFACT_SCHEMA = "phase4cr.snapshot.v1"
REPORT_SCHEMA = "phase4cr.atomicity-stress.v1"
MAX_GENERATIONS = 1_000
MAX_READERS = 64


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def snapshot(generation: int) -> dict[str, Any]:
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise ValueError("PHASE4CR_GENERATION_INVALID")
    payload: dict[str, Any] = {
        "schema": ARTIFACT_SCHEMA,
        "generation": generation,
        "payload": {"generation_marker": f"complete-{generation}", "values": list(range(16))},
    }
    payload["artifact_hash"] = _hash(payload)
    return payload


def validate(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PHASE4CR_ARTIFACT_UNREADABLE") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != ARTIFACT_SCHEMA
        or payload.get("artifact_hash") != _hash(payload)
        or payload.get("payload", {}).get("generation_marker")
        != f"complete-{payload.get('generation')}"
    ):
        raise ValueError("PHASE4CR_ARTIFACT_INVALID")
    return payload


def atomic_publish(path: Path, payload: dict[str, Any]) -> None:
    if payload.get("schema") != ARTIFACT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CR_PUBLICATION_PAYLOAD_INVALID")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _simultaneous_reads(path: Path, readers: int) -> tuple[list[int], list[str]]:
    barrier = threading.Barrier(readers)
    generations: list[int] = []
    errors: list[str] = []
    lock = threading.Lock()

    def read_once() -> None:
        try:
            barrier.wait()
            generation = validate(path)["generation"]
            with lock:
                generations.append(generation)
        except (ValueError, threading.BrokenBarrierError) as exc:
            with lock:
                errors.append(type(exc).__name__)

    threads = [threading.Thread(target=read_once) for _ in range(readers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return generations, errors


def run_stress(generations: int, readers: int) -> dict[str, Any]:
    if (
        isinstance(generations, bool)
        or not isinstance(generations, int)
        or not 1 <= generations <= MAX_GENERATIONS
    ):
        raise ValueError("PHASE4CR_GENERATIONS_INVALID")
    if isinstance(readers, bool) or not isinstance(readers, int) or not 1 <= readers <= MAX_READERS:
        raise ValueError("PHASE4CR_READERS_INVALID")
    read_errors: list[str] = []
    boundary_violations = 0
    total_reads = 0
    with tempfile.TemporaryDirectory(prefix="phase4cr-") as temporary:
        root = Path(temporary)
        target = root / "snapshot.json"
        atomic_publish(target, snapshot(0))

        interrupted = root / f".{target.name}.interrupted"
        interrupted.write_text('{"schema":"phase4cr.snapshot.v1"', encoding="utf-8")
        try:
            validate(interrupted)
        except ValueError:
            pass
        else:
            boundary_violations += 1
        if validate(target)["generation"] != 0:
            boundary_violations += 1

        for generation in range(1, generations + 1):
            before, errors = _simultaneous_reads(target, readers)
            total_reads += len(before)
            read_errors.extend(errors)
            if any(value != generation - 1 for value in before):
                boundary_violations += 1
            atomic_publish(target, snapshot(generation))
            after, errors = _simultaneous_reads(target, readers)
            total_reads += len(after)
            read_errors.extend(errors)
            if any(value != generation for value in after):
                boundary_violations += 1

        stale_temporaries = list(root.glob(f".{target.name}.*"))
        for stale in stale_temporaries:
            stale.unlink()
        recovered = validate(target)
        report: dict[str, Any] = {
            "schema": REPORT_SCHEMA,
            "phase": "4CR",
            "generations": generations,
            "readers_per_boundary": readers,
            "total_successful_reads": total_reads,
            "read_errors": sorted(read_errors),
            "boundary_violations": boundary_violations,
            "interrupted_write_refused": True,
            "final_generation": recovered["generation"],
            "stale_temporaries_after_recovery": len(list(root.glob(f".{target.name}.*"))),
            "disposable_directory": True,
            "production_paths_touched": 0,
            "execution_authorized": False,
        }
    report["artifact_hash"] = _hash(report)
    return report


def publish_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generations", type=int, default=100)
    parser.add_argument("--readers", type=int, default=8)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run_stress(args.generations, args.readers)
    publish_report(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
