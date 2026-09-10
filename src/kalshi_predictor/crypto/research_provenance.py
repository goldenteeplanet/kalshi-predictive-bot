"""Freeze committed model code before acquisition; not a model release certificate."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def freeze_code(repo: Path, output: Path, paths: tuple[str, ...]) -> dict[str, Any]:
    repo = repo.resolve(strict=True)

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-c", f"safe.directory={repo.as_posix()}", *args],
            cwd=repo,
            text=True,
            timeout=10,
        ).strip()

    if not paths or len(set(paths)) != len(paths):
        raise ValueError("EXPLICIT_UNIQUE_DEPENDENCIES_REQUIRED")
    head = git("rev-parse", "HEAD")
    committed = datetime.fromtimestamp(int(git("show", "-s", "--format=%ct", head)), UTC)
    frozen = datetime.now(UTC)
    if committed > frozen:
        raise ValueError("FUTURE_COMMIT_TIME")
    files: list[dict[str, Any]] = []
    for relative in paths:
        path = (repo / relative).resolve(strict=True)
        if not path.is_relative_to(repo) or path.is_symlink():
            raise ValueError("DEPENDENCY_OUTSIDE_REPO")
        if git("status", "--porcelain", "--", relative):
            raise ValueError("UNCOMMITTED_MODEL_DEPENDENCY")
        if git("hash-object", str(path)) != git("rev-parse", f"{head}:{relative}"):
            raise ValueError("MODEL_BLOB_MISMATCH")
        raw = path.read_bytes()
        files.append({"path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "raw": raw})
    output.mkdir(parents=True, exist_ok=False)
    for item in files:
        destination = output / item["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(item.pop("raw"))
    return {
        "source_commit": head,
        "commit_recorded_at": committed.isoformat(),
        "code_frozen_at": frozen.isoformat(),
        "files": files,
        "release_certified": False,
        "clock_authority": "LOCAL_CLOCK_AND_GIT_METADATA_NOT_EXTERNAL_ATTESTATION",
    }


def verify_unchanged(repo: Path, proof: dict[str, Any]) -> None:
    for item in proof["files"]:
        raw = (repo / item["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("MODEL_CHANGED_DURING_CAPTURE")


def freeze_prediction(
    output: Path,
    prediction: dict[str, Any],
    *,
    model_input_as_of: datetime,
    input_received_at: datetime,
    model_committed_at: datetime,
    target_at: datetime,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Persist a prediction before recording the subsequent research decision.

    The numerical model cutoff is not its disk completion or decision timestamp.
    Hashes bind exact bytes; post-fsync local clock receipts are not external
    timestamp attestations. An incomplete directory is never a valid decision.
    """

    def aware(value: datetime) -> bool:
        return value.tzinfo is not None and value.utcoffset() is not None

    if not all(
        aware(t) for t in (model_input_as_of, input_received_at, model_committed_at, target_at)
    ):
        raise ValueError("AWARE_PREDICTION_TIMESTAMPS_REQUIRED")
    if not (
        input_received_at <= model_input_as_of < target_at
        and model_committed_at <= model_input_as_of
    ):
        raise ValueError("INVALID_PREDICTION_INPUT_CHRONOLOGY")
    payload = {
        "schema": "frozen-research-prediction-v1",
        "model_input_as_of": model_input_as_of.isoformat(),
        "input_received_at": input_received_at.isoformat(),
        "model_committed_at": model_committed_at.isoformat(),
        "target_at": target_at.isoformat(),
        "prediction": prediction,
    }
    encoded = json.dumps(payload, sort_keys=True, default=str, allow_nan=False).encode()
    if len(encoded) > 8_000_000:
        raise ValueError("PREDICTION_SIZE_CAP")
    output.mkdir(parents=True, exist_ok=False)

    def write(name: str, raw: bytes) -> str:
        pending = output / (name + ".pending")
        with pending.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Publish a complete file only after fsync; a failed write remains .pending.
        pending.rename(output / name)
        return hashlib.sha256(raw).hexdigest()

    digest = write("prediction.json", encoded)
    recorded = clock()  # sampled only after prediction bytes are flushed to disk
    if not aware(recorded) or recorded < model_input_as_of:
        raise ValueError("PREDICTION_RECORDING_CLOCK_REGRESSED")
    receipt = {
        "schema": "prediction-recording-receipt-v1",
        "prediction_sha256": digest,
        "prediction_recorded_at": recorded.isoformat(),
        "clock_authority": "POST_FSYNC_LOCAL_CLOCK_NOT_EXTERNAL_ATTESTATION",
    }
    receipt_sha = write("recording-receipt.json", json.dumps(receipt, sort_keys=True).encode())
    decision = clock()  # research decision follows both prediction and its disk receipt
    if not aware(decision) or not recorded <= decision < target_at:
        raise ValueError("DECISION_CLOCK_REGRESSED_OR_TARGET_EXPIRED")
    result = {
        **receipt,
        "schema": "recorded-research-decision-v1",
        "prediction_receipt_sha256": receipt_sha,
        "model_input_as_of": model_input_as_of.isoformat(),
        "input_received_at": input_received_at.isoformat(),
        "model_committed_at": model_committed_at.isoformat(),
        "decision_at": decision.isoformat(),
        "status": "PROXY_SHADOW_UNQUALIFIED",
        "execution_authority": False,
    }
    write("decision.json", json.dumps(result, sort_keys=True).encode())
    return result
