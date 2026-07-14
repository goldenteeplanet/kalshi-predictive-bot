from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.utils.time import parse_datetime, utc_now

DEFAULT_HEARTBEAT_DIR = Path("reports/phase3au")
DEFAULT_HEARTBEAT_FILE = "link_remediate_heartbeat.json"
DEFAULT_EVENTS_FILE = "link_remediate_events.jsonl"
DEFAULT_CHECKPOINT_FILE = "link_remediate_checkpoint.json"
DEFAULT_REPORT_FILE = "phase3au_long_job_status.md"


@dataclass
class LongJobHeartbeat:
    job_name: str
    output_dir: Path = DEFAULT_HEARTBEAT_DIR
    checkpoint_every: int = 100
    started_at: datetime = field(default_factory=utc_now)
    processed: int = 0
    total: int | None = None
    current_stage: str = "STARTING"
    current_item: str | None = None
    last_message: str = "Starting long job."

    @property
    def heartbeat_path(self) -> Path:
        return self.output_dir / DEFAULT_HEARTBEAT_FILE

    @property
    def events_path(self) -> Path:
        return self.output_dir / DEFAULT_EVENTS_FILE

    @property
    def checkpoint_path(self) -> Path:
        return self.output_dir / DEFAULT_CHECKPOINT_FILE

    def emit(
        self,
        *,
        stage: str | None = None,
        processed: int | None = None,
        total: int | None = None,
        current_item: str | None = None,
        message: str | None = None,
        force_checkpoint: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if stage is not None:
            self.current_stage = stage
        if processed is not None:
            self.processed = processed
        if total is not None:
            self.total = total
        if current_item is not None:
            self.current_item = current_item
        if message is not None:
            self.last_message = message

        payload = self.snapshot(extra=extra)
        self.heartbeat_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
        if force_checkpoint or _should_checkpoint(self.processed, self.checkpoint_every):
            self.checkpoint_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        return payload

    def snapshot(self, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        now = utc_now()
        elapsed = max(0, int((now - self.started_at).total_seconds()))
        payload: dict[str, Any] = {
            "job_name": self.job_name,
            "pid": os.getpid(),
            "started_at": self.started_at.isoformat(),
            "heartbeat_at": now.isoformat(),
            "elapsed_seconds": elapsed,
            "elapsed": format_elapsed(elapsed),
            "stage": self.current_stage,
            "processed": self.processed,
            "total": self.total,
            "current_item": self.current_item,
            "message": self.last_message,
        }
        if self.total:
            payload["progress_percent"] = round((self.processed / max(self.total, 1)) * 100, 2)
        if extra:
            payload["extra"] = extra
        return payload


def load_latest_long_job_status(
    *,
    output_dir: Path = DEFAULT_HEARTBEAT_DIR,
    stale_after_seconds: int = 300,
) -> dict[str, Any]:
    heartbeat_path = output_dir / DEFAULT_HEARTBEAT_FILE
    checkpoint_path = output_dir / DEFAULT_CHECKPOINT_FILE
    heartbeat = _load_json(heartbeat_path)
    checkpoint = _load_json(checkpoint_path)
    now = utc_now()
    age_seconds: int | None = None
    stale = True
    heartbeat_at = parse_datetime(str(heartbeat.get("heartbeat_at"))) if heartbeat else None
    if heartbeat_at is not None:
        age_seconds = max(0, int((now - heartbeat_at).total_seconds()))
        stale = age_seconds > stale_after_seconds
    return {
        "status": "STALE" if stale else "ACTIVE",
        "heartbeat_path": str(heartbeat_path),
        "checkpoint_path": str(checkpoint_path),
        "heartbeat": heartbeat,
        "checkpoint": checkpoint,
        "heartbeat_age_seconds": age_seconds,
        "heartbeat_age": format_elapsed(age_seconds),
        "stale_after_seconds": stale_after_seconds,
        "recommended_next_action": _recommended_status_action(heartbeat, stale=stale),
    }


def write_phase3au_report(
    *,
    output_dir: Path = DEFAULT_HEARTBEAT_DIR,
    stale_after_seconds: int = 300,
) -> Path:
    status = load_latest_long_job_status(
        output_dir=output_dir,
        stale_after_seconds=stale_after_seconds,
    )
    heartbeat = status.get("heartbeat") or {}
    lines = [
        "# Phase 3AU Long Job Heartbeat",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        "- Mode: PAPER ONLY diagnostics",
        "- Live/demo execution: blocked",
        "",
        "## Current Job",
        "",
        f"- Status: {status['status']}",
        f"- Job: {heartbeat.get('job_name') or 'none'}",
        f"- PID: {heartbeat.get('pid') or 'none'}",
        f"- Stage: {heartbeat.get('stage') or 'none'}",
        f"- Processed: {heartbeat.get('processed') or 0} / {heartbeat.get('total') or 'unknown'}",
        f"- Elapsed: {heartbeat.get('elapsed') or 'n/a'}",
        f"- Heartbeat age: {status.get('heartbeat_age') or 'n/a'}",
        f"- Current item: {heartbeat.get('current_item') or 'none'}",
        "",
        "## Next Action",
        "",
        status["recommended_next_action"],
        "",
        "## Files",
        "",
        f"- Heartbeat: `{status['heartbeat_path']}`",
        f"- Checkpoint: `{status['checkpoint_path']}`",
    ]
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / DEFAULT_REPORT_FILE
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path


def format_elapsed(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    days, remainder = divmod(int(seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d {hours:02d}h {minutes:02d}m {seconds:02d}s"
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def stop_after_deadline(minutes: int | None) -> float | None:
    if minutes is None or minutes <= 0:
        return None
    return time.monotonic() + (minutes * 60)


def deadline_reached(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _should_checkpoint(processed: int, checkpoint_every: int) -> bool:
    return checkpoint_every > 0 and processed > 0 and processed % checkpoint_every == 0


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _recommended_status_action(heartbeat: dict[str, Any] | None, *, stale: bool) -> str:
    if not heartbeat:
        return "No heartbeat exists yet. Run link-remediate with --heartbeat-dir reports/phase3au."
    stage = str(heartbeat.get("stage") or "").lower()
    if stale:
        return "Heartbeat is stale. Check db-writer-monitor before starting another write job."
    if "sports" in stage:
        return "Sports remediation is active. Wait, or use --stop-after-minutes next run."
    if "complete" in stage:
        return "Run kalshi-bot derive-sports-schedule --build-features or market-coverage-doctor."
    return "Long job is active. Avoid starting another write job until it finishes."
