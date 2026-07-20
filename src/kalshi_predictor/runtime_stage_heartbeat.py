from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from kalshi_predictor.utils.time import utc_now


class AtomicStageHeartbeat:
    """Publish stage transitions and periodic liveness without touching the database."""

    def __init__(
        self,
        output_path: Path,
        *,
        phase: str,
        interval_seconds: float = 15.0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.output_path = output_path
        self.phase = phase
        self.interval_seconds = max(float(interval_seconds), 0.05)
        self.metadata = dict(metadata or {})
        self.timings: list[dict[str, Any]] = []
        self._current_stage: str | None = None
        self._current_started_at: Any | None = None
        self._sequence = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"{phase.lower()}-stage-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def mark(self, stage: str) -> None:
        now = utc_now()
        with self._lock:
            if self._current_stage is not None and self._current_started_at is not None:
                self.timings.append(
                    {
                        "stage": self._current_stage,
                        "started_at": self._current_started_at.isoformat(),
                        "completed_at": now.isoformat(),
                        "duration_seconds": round(
                            (now - self._current_started_at).total_seconds(), 3
                        ),
                    }
                )
            self._current_stage = stage
            self._current_started_at = now
            self._sequence += 1
            self._write_locked(now=now, event="stage_transition")
        print(f"Phase {self.phase} stage: {stage}", flush=True)
        if stage == "complete":
            self.close()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=min(self.interval_seconds, 1.0) + 0.25)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            with self._lock:
                if self._current_stage is None:
                    continue
                self._sequence += 1
                self._write_locked(now=utc_now(), event="heartbeat")

    def _write_locked(self, *, now: Any, event: str) -> None:
        payload = {
            "generated_at": now.isoformat(),
            "heartbeat_at": now.isoformat(),
            "heartbeat_sequence": self._sequence,
            "heartbeat_interval_seconds": self.interval_seconds,
            "event": event,
            "phase": self.phase,
            "stage": self._current_stage,
            "stage_started_at": (
                self._current_started_at.isoformat()
                if self._current_started_at is not None
                else None
            ),
            "pid": os.getpid(),
            "completed_stage_timings": list(self.timings),
            **self.metadata,
        }
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.output_path.with_name(
            f".{self.output_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        os.replace(temporary, self.output_path)
