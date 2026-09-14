"""Consume newly certified NYC-W4 windows into the disabled W8 shadow census."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from kalshi_predictor.phase_nyc_w7 import write_shadow_runtime_report
from kalshi_predictor.phase_nyc_w8 import write_nyc_w8_report
from kalshi_predictor.utils.time import parse_datetime


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_nyc_w8"))
    parser.add_argument("--max-adjustment", type=Decimal, default=Decimal("0.10"))
    parser.add_argument("--recover-corrupt-state", action="store_true")
    args = parser.parse_args()
    state_path = args.output_dir / "cadence_state.json"
    if args.recover_corrupt_state:
        state = _recover_corrupt_state(state_path)
    else:
        state = _load_or_start(state_path)
    if not state_path.exists():
        _save_state(state_path, state)
    started_at = parse_datetime(state["started_at"])
    consumed = set(state.get("consumed_source_reports", []))
    census_dir = _census_directory(args.output_dir, args.reports_dir, state)

    for source in sorted(args.reports_dir.glob(
        "phase_nyc_w4*/nyc_w4_observation_feature_integration_preview.json"
    )):
        source_key = str(source.resolve())
        if source_key in consumed:
            continue
        payload = json.loads(source.read_text(encoding="utf-8"))
        generated_at = parse_datetime(payload.get("generated_at"))
        if generated_at is None or generated_at <= started_at:
            continue
        if state.get("recovery_epoch") and generated_at > datetime.now(UTC):
            continue
        targets = sorted({
            str(row.get("target_utc_time") or "") for row in payload.get("rows", [])
            if row.get("preview_passed") and row.get("target_utc_time")
        })
        if not targets:
            continue
        if state.get("recovery_epoch") and any(
            parse_datetime(target) is None or parse_datetime(target) <= started_at
            for target in targets
        ):
            continue
        slug = targets[0].replace(":", "").replace("+", "p").replace("-", "")
        write_shadow_runtime_report(
            reports_dir=args.reports_dir,
            output_dir=census_dir / (
                f"phase_nyc_w7_live_{slug}"
                + ("_" + hashlib.sha256(source_key.encode()).hexdigest() if state.get(
                    "recovery_epoch"
                ) else "")
            ),
            max_adjustment=args.max_adjustment,
            source_paths=[source],
        )
        consumed.add(source_key)
        state["consumed_source_reports"] = sorted(consumed)
        _save_state(state_path, state)

    state["consumed_source_reports"] = sorted(consumed)
    state["last_run_at"] = datetime.now(UTC).isoformat()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _save_state(state_path, state)
    print(write_nyc_w8_report(
        reports_dir=census_dir,
        output_dir=census_dir if state.get("recovery_epoch") else args.output_dir,
    ))


def _load_or_start(path: Path) -> dict[str, object]:
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            _validate_state(state)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(
                "CADENCE_STATE_INVALID_PRESERVE_ORIGINAL_RESTORE_VERIFIED_BACKUP"
            ) from exc
        return state
    return {
        "started_at": datetime.now(UTC).isoformat(),
        "consumed_source_reports": [],
        "mode": "READ_ONLY_DISABLED_FEATURE_FLAG",
    }


def _validate_state(state: object) -> None:
    if not isinstance(state, dict):
        raise ValueError("CADENCE_STATE_OBJECT_REQUIRED")
    started = state.get("started_at")
    if not isinstance(started, str):
        raise ValueError("CADENCE_START_REQUIRED")
    parsed = datetime.fromisoformat(started.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("CADENCE_START_OFFSET_REQUIRED")
    consumed = state.get("consumed_source_reports")
    if not isinstance(consumed, list) or any(
        not isinstance(value, str) or not value for value in consumed
    ):
        raise ValueError("CADENCE_CONSUMED_IDENTITIES_REQUIRED")
    if "recovery_epoch" in state:
        epoch = state["recovery_epoch"]
        if not isinstance(epoch, str) or len(epoch) != 32 or any(
            value not in "0123456789abcdef" for value in epoch
        ):
            raise ValueError("CADENCE_RECOVERY_EPOCH_INVALID")
        if state.get("historical_consumption_status") != "UNKNOWN_NOT_RECONSTRUCTED":
            raise ValueError("CADENCE_RECOVERY_HISTORY_REQUIRED")


def _census_directory(output_dir: Path, reports_dir: Path, state: dict[str, object]) -> Path:
    _validate_state(state)
    if "recovery_epoch" not in state:
        return reports_dir
    return output_dir / "recovery_epochs" / str(state["recovery_epoch"])


def _write_exclusive(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _recover_corrupt_state(path: Path) -> dict[str, object]:
    """Explicit new epoch; preserve corrupt bytes, never infer old consumption.

    The production unit's existing flock must cover this entire operation.
    A failed attempt leaves any exclusive archive intact for inspection.
    """
    original = path.read_bytes()  # Missing state is not corrupt-state recovery.
    try:
        _load_or_start(path)
    except ValueError:
        pass
    else:
        raise ValueError("CADENCE_VALID_STATE_MUST_NOT_RESET")
    epoch = uuid.uuid4().hex
    archive = path.parent / "recovery_epochs" / epoch
    archive.mkdir(parents=True, exist_ok=False)
    _write_exclusive(archive / "corrupt-state.original", original)
    original_sha = hashlib.sha256(original).hexdigest()
    recorded = datetime.now(UTC).isoformat()
    receipt = {
        "schema": "NYC_CADENCE_CORRUPT_STATE_ARCHIVE_V1",
        "sha256": original_sha,
        "byte_count": len(original),
        "original_path": str(path.resolve()),
        "original_recorded_at": recorded,
        "historical_consumption_status": "UNKNOWN_NOT_RECONSTRUCTED",
    }
    _write_exclusive(
        archive / "archive.receipt.json",
        json.dumps(receipt, sort_keys=True, allow_nan=False).encode(),
    )
    # Start after original and archive receipt are durably written, never backdated.
    state: dict[str, object] = {
        "started_at": datetime.now(UTC).isoformat(),
        "consumed_source_reports": [],
        "mode": "READ_ONLY_DISABLED_FEATURE_FLAG",
        "recovery_epoch": epoch,
        "historical_consumption_status": "UNKNOWN_NOT_RECONSTRUCTED",
        "archived_original_sha256": original_sha,
        "recovery_scope": "POST_EPOCH_SOURCES_AND_TARGETS_ONLY",
    }
    if path.read_bytes() != original:
        raise ValueError("CADENCE_ORIGINAL_CHANGED_DURING_RECOVERY")
    _save_state(path, state)
    return state


def _save_state(path: Path, state: dict[str, object]) -> None:
    """Replace only after a complete durable write; pre-replace failures preserve old state.

    Caller holds the existing cadence flock. This is not an exactly-once transaction
    with W7 output publication; a crash between output and checkpoint can replay it.
    """
    _validate_state(state)
    payload = json.dumps(state, indent=2, sort_keys=True, allow_nan=False).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    main()
