from __future__ import annotations

import copy
import hashlib
import json

from scripts.local.phase4kx_workspace_provenance import SCHEMA as MANIFEST_SCHEMA
from scripts.local.phase4ky_change_boundary import ALLOWLIST_SCHEMA, verify_boundary


def _manifest(*rows: tuple[str, str, str | None]) -> dict:
    entries = [
        {
            "path": path,
            "status": status,
            "original_path": None,
            "size_bytes": 1,
            "sha256": digest,
        }
        for path, status, digest in rows
    ]
    payload = {
        "schema": MANIFEST_SCHEMA,
        "repository": "/fixture",
        "entry_count": len(entries),
        "entries": entries,
        "safety": {"read_only": True},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def _allow(*paths: str) -> dict:
    return {"schema": ALLOWLIST_SCHEMA, "owned_paths": list(paths)}


def test_allows_only_owned_delta_and_preserves_unrelated_baseline() -> None:
    baseline = _manifest(("user.txt", " M", "a"))
    current = _manifest(("phase.py", "??", "b"), ("user.txt", " M", "a"))
    result = verify_boundary(baseline, current, _allow("phase.py"))
    assert result["verdict"] == "PASS"


def test_refuses_undeclared_new_change() -> None:
    result = verify_boundary(_manifest(), _manifest(("surprise.py", "??", "x")), _allow())
    assert result["verdict"] == "REFUSE"
    assert "UNRELATED_CHANGE:surprise.py" in result["errors"]


def test_refuses_changed_preexisting_user_file() -> None:
    baseline = _manifest(("user.txt", " M", "a"))
    current = _manifest(("user.txt", " M", "b"))
    assert verify_boundary(baseline, current, _allow())["verdict"] == "REFUSE"


def test_refuses_unrelated_staged_file_even_when_unchanged() -> None:
    baseline = _manifest(("user.txt", "M ", "a"))
    current = copy.deepcopy(baseline)
    result = verify_boundary(baseline, current, _allow())
    assert "UNRELATED_STAGED:user.txt" in result["errors"]


def test_refuses_missing_owned_file() -> None:
    result = verify_boundary(_manifest(), _manifest(), _allow("phase.py"))
    assert "OWNED_PATH_MISSING:phase.py" in result["errors"]


def test_refuses_manifest_tampering() -> None:
    current = _manifest(("phase.py", "??", "x"))
    current["entries"][0]["sha256"] = "tampered"
    result = verify_boundary(_manifest(), current, _allow("phase.py"))
    assert "CURRENT:HASH_MISMATCH" in result["errors"]


def test_refuses_path_traversal_and_duplicate_allowlist() -> None:
    result = verify_boundary(_manifest(), _manifest(), _allow("../escape", "ok.py", "ok.py"))
    assert result["verdict"] == "REFUSE"
    assert "ALLOWLIST:UNSAFE_PATH" in result["errors"]
    assert "ALLOWLIST:DUPLICATE_PATH" in result["errors"]
