from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from kalshi_predictor.ui.performance_cache import BoundedSingleFlightCache

ALLOWED_PREFIXES = (
    "ui_obs",
    "phase_prov",
    "phase_pmb",
    "phase_nyc",
    "phase_gh",
    "phase_readiness",
    "provenance",
)
ALLOWED_SUFFIXES = {".json", ".md", ".txt"}
MAX_FILE_BYTES = 1_048_576
MAX_RENDER_BYTES = 65_536
MAX_CATALOG_ITEMS = 25
SENSITIVE_KEYS = {"api_key", "secret", "token", "password", "private_key", "private_key_path"}
_CATALOG_CACHE: BoundedSingleFlightCache[dict[str, Any]] = BoundedSingleFlightCache(
    ttl_seconds=30, max_entries=1
)


class EvidenceRejected(ValueError):
    pass


def evidence_root() -> Path:
    return Path(os.environ.get("KALSHI_EVIDENCE_ROOT", "reports"))


def _allowed_relative(relative: Path) -> bool:
    return (
        bool(relative.parts)
        and any(relative.parts[0].startswith(prefix) for prefix in ALLOWED_PREFIXES)
        and relative.suffix.lower() in ALLOWED_SUFFIXES
    )


def _safe_file(root: Path, candidate: Path) -> tuple[Path, Path]:
    root_resolved = root.resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise EvidenceRejected("PATH_OUTSIDE_ROOT") from exc
    if not _allowed_relative(relative) or ".." in relative.parts:
        raise EvidenceRejected("PATH_NOT_ALLOWLISTED")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise EvidenceRejected("SYMLINK_REJECTED")
    resolved = candidate.resolve()
    if resolved == root_resolved or root_resolved not in resolved.parents:
        raise EvidenceRejected("PATH_OUTSIDE_ROOT")
    if not resolved.is_file():
        raise EvidenceRejected("NOT_A_FILE")
    if resolved.stat().st_size > MAX_FILE_BYTES:
        raise EvidenceRejected("FILE_TOO_LARGE")
    return resolved, relative


def _id(relative: Path) -> str:
    return hashlib.sha256(relative.as_posix().encode()).hexdigest()[:24]


def _summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "phase": None,
            "status": "UNKNOWN",
            "gate_failures": [],
            "provenance": None,
            "rollback": None,
        }
    diagnostics = payload.get("diagnostics") or []
    failures = [
        str(item)
        for item in diagnostics
        if "FAIL" in str(item).upper()
        or "DRIFT" in str(item).upper()
        or "INVALID" in str(item).upper()
    ]
    return {
        "phase": payload.get("phase"),
        "status": payload.get("status") or payload.get("decision") or "UNKNOWN",
        "gate_failures": failures[:20],
        "provenance": payload.get("provenance") or payload.get("bundle", {}).get("bundle_digest")
        if isinstance(payload.get("bundle"), dict)
        else payload.get("provenance"),
        "rollback": payload.get("rollback")
        or payload.get("rollback_evidence")
        or payload.get("backup_path"),
    }


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if str(key).lower() in SENSITIVE_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def build_evidence_catalog(root: Path | None = None) -> dict[str, Any]:
    root = root or evidence_root()
    items = []
    rejected = 0
    if root.is_dir():
        stop = False
        top_entries = sorted(root.iterdir(), key=lambda path: (-path.stat().st_mtime_ns, path.name))
        for top in top_entries:
            if stop:
                break
            if not any(top.name.startswith(prefix) for prefix in ALLOWED_PREFIXES):
                if top.is_dir() or top.is_file():
                    rejected += 1
                continue
            if top.is_symlink():
                rejected += 1
                continue
            if not top.is_dir():
                continue
            for current, directories, filenames in os.walk(top, followlinks=False):
                directories[:] = sorted(
                    name for name in directories if not (Path(current) / name).is_symlink()
                )
                for filename in sorted(filenames):
                    candidate = Path(current) / filename
                    try:
                        resolved, relative = _safe_file(root, candidate)
                    except EvidenceRejected:
                        rejected += 1
                        continue
                    raw = resolved.read_bytes()
                    if relative.suffix.lower() == ".json":
                        try:
                            summary = _summary(json.loads(raw.decode("utf-8")))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            summary = {
                                **_summary(None),
                                "status": "INVALID",
                                "gate_failures": ["ARTIFACT_JSON_INVALID"],
                            }
                    else:
                        summary = _summary(None)
                    status = str(summary["status"]).upper()
                    items.append(
                        {
                            "id": _id(relative),
                            "path": relative.as_posix(),
                            "bytes": len(raw),
                            "sha256": hashlib.sha256(raw).hexdigest(),
                            **summary,
                            "status": status,
                            "state_class": status.lower()
                            if status in {"PASSED", "FAILED", "BLOCKED", "WAITING", "RUNNING"}
                            else "blocked",
                        }
                    )
                    if len(items) >= MAX_CATALOG_ITEMS:
                        stop = True
                        break
                if stop:
                    break
    return {
        "read_only": True,
        "root_label": "reports",
        "items": items,
        "count": len(items),
        "limit": MAX_CATALOG_ITEMS,
        "truncated": len(items) >= MAX_CATALOG_ITEMS,
        "rejected": rejected,
        "allowed_suffixes": sorted(ALLOWED_SUFFIXES),
    }


def get_cached_evidence_catalog(root: Path | None = None, *, force: bool = False) -> dict[str, Any]:
    selected = root or evidence_root()
    return _CATALOG_CACHE.get(
        str(selected.resolve()), lambda: build_evidence_catalog(selected), force=force
    )


def evidence_cache_metrics() -> dict[str, int | float]:
    return _CATALOG_CACHE.metrics()


def load_evidence_artifact(artifact_id: str, root: Path | None = None) -> dict[str, Any]:
    if len(artifact_id) != 24 or any(
        character not in "0123456789abcdef" for character in artifact_id
    ):
        raise EvidenceRejected("ARTIFACT_ID_INVALID")
    root = root or evidence_root()
    match = next(
        (item for item in get_cached_evidence_catalog(root)["items"] if item["id"] == artifact_id),
        None,
    )
    if match is None:
        raise EvidenceRejected("ARTIFACT_NOT_FOUND")
    resolved, relative = _safe_file(root, root / match["path"])
    raw = resolved.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != match["sha256"]:
        raise EvidenceRejected("ARTIFACT_CHANGED_DURING_READ")
    rendered = raw[:MAX_RENDER_BYTES].decode("utf-8", errors="replace")
    if relative.suffix.lower() == ".json":
        try:
            rendered = json.dumps(_redact(json.loads(rendered)), indent=2, sort_keys=True)
        except json.JSONDecodeError:
            pass
    else:
        rendered = re.sub(
            r"(?im)(api_key|secret|token|password|private_key)\s*[:=]\s*\S+",
            r"\1: [REDACTED]",
            rendered,
        )
    return {
        **match,
        "content": rendered,
        "content_truncated": len(raw) > MAX_RENDER_BYTES,
        "verified_sha256": digest,
        "read_only": True,
    }
