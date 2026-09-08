"""Build a deterministic, read-only manifest of outstanding Git worktree changes."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

SCHEMA = "phase4kx.workspace-provenance.v1"


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(repo: Path) -> dict[str, object]:
    root = repo.resolve()
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    records: list[dict[str, object]] = []
    fields = result.stdout.split(b"\0")
    index = 0
    while index < len(fields) and fields[index]:
        entry = fields[index].decode("utf-8", errors="surrogateescape")
        status, relative = entry[:2], entry[3:]
        original = None
        if status[0] in "RC" or status[1] in "RC":
            index += 1
            original = fields[index].decode("utf-8", errors="surrogateescape")
        path = root / relative
        records.append(
            {
                "path": relative.replace("\\", "/"),
                "status": status,
                "original_path": original,
                "size_bytes": path.stat().st_size if path.is_file() else None,
                "sha256": _sha256(path),
            }
        )
        index += 1
    records.sort(key=lambda item: (str(item["path"]), str(item["status"])))
    payload: dict[str, object] = {
        "schema": SCHEMA,
        "repository": str(root),
        "entry_count": len(records),
        "entries": records,
        "safety": {
            "read_only": True,
            "database_access": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = build_manifest(args.repo)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
