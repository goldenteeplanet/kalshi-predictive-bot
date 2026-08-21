from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import kalshi_predictor
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import (
    database_url_from_settings,
    redact_database_url,
    sqlite_path_from_url,
)
from kalshi_predictor.utils.time import utc_now

READY = "READY"
PATH_MISMATCH = "PATH_MISMATCH"
OLD_RUNTIME = "OLD_RUNTIME"
ONEDRIVE_REFERENCE = "ONEDRIVE_REFERENCE"
EDITABLE_INSTALL_MISMATCH = "EDITABLE_INSTALL_MISMATCH"
DATABASE_PATH_MISMATCH = "DATABASE_PATH_MISMATCH"
RUNTIME_ORIGIN_VERSION = "runtime_origin_v1"


@dataclass(frozen=True)
class RuntimePaths:
    development_root: Path
    runtime_root: Path
    database_path: Path | None
    report_root: Path
    ui_report_root: Path
    research_data_root: Path
    writer_lock: Path
    staging_root: Path


def resolve_runtime_paths(
    *,
    settings: Settings | None = None,
    cwd: Path | None = None,
    package_file: Path | None = None,
) -> RuntimePaths:
    resolved = settings or get_settings()
    package_path = (package_file or Path(kalshi_predictor.__file__)).resolve()
    detected_runtime_root = _source_root(package_path)
    cwd_path = (cwd or Path.cwd()).resolve()
    runtime_root = _configured_path(resolved.kalshi_runtime_root) or detected_runtime_root
    development_root = (
        _configured_path(resolved.kalshi_development_root)
        or _nearest_git_root(cwd_path)
        or runtime_root
    )
    database_path = sqlite_path_from_url(database_url_from_settings(resolved))
    report_root = _configured_path(resolved.kalshi_report_root) or runtime_root / "reports"
    ui_report_root = _configured_path(resolved.kalshi_ui_report_root) or report_root
    research_data_root = (
        _configured_path(resolved.kalshi_research_data_root)
        or runtime_root / "data" / "research"
    )
    writer_lock = (
        _configured_path(resolved.kalshi_writer_lock)
        or runtime_root.parent / "kalshi-local-runtime" / "kalshi-writer.lock"
    )
    staging_root = _configured_path(resolved.kalshi_staging_root) or report_root / "staging"
    return RuntimePaths(
        development_root=development_root.resolve(),
        runtime_root=runtime_root.resolve(),
        database_path=database_path.resolve() if database_path else None,
        report_root=report_root.resolve(),
        ui_report_root=ui_report_root.resolve(),
        research_data_root=research_data_root.resolve(),
        writer_lock=writer_lock.resolve(),
        staging_root=staging_root.resolve(),
    )


def build_runtime_origin(
    *,
    settings: Settings | None = None,
    cwd: Path | None = None,
    package_file: Path | None = None,
    python_executable: Path | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    cwd_path = (cwd or Path.cwd()).resolve()
    package_path = (package_file or Path(kalshi_predictor.__file__)).resolve()
    python_path = (python_executable or Path(sys.executable)).absolute()
    paths = resolve_runtime_paths(
        settings=resolved,
        cwd=cwd_path,
        package_file=package_path,
    )
    development_sha = _git_value(paths.development_root, "rev-parse", "HEAD")
    deployment = _deployment_manifest(paths.runtime_root)
    deployed_sha = deployment.get("git_sha") if deployment else None
    statuses = _statuses(
        paths=paths,
        cwd=cwd_path,
        package_path=package_path,
        development_sha=development_sha,
        deployed_sha=deployed_sha,
    )
    db_url = database_url_from_settings(resolved)
    return {
        "version": RUNTIME_ORIGIN_VERSION,
        "generated_at": utc_now().isoformat(),
        "status": statuses[0],
        "statuses": statuses,
        "cwd": str(cwd_path),
        "git_root": str(paths.development_root),
        "development_root": str(paths.development_root),
        "runtime_source": str(paths.runtime_root),
        "installed_package_source": str(package_path),
        "branch": _git_value(paths.development_root, "branch", "--show-current"),
        "sha": development_sha,
        "deployed_sha": deployed_sha,
        "python": str(python_path),
        "virtualenv": str(Path(sys.prefix).resolve()),
        "database": {
            "url": redact_database_url(db_url),
            "path": str(paths.database_path) if paths.database_path else None,
            "exists": bool(paths.database_path and paths.database_path.exists()),
        },
        "report_root": str(paths.report_root),
        "ui_report_root": str(paths.ui_report_root),
        "research_data_root": str(paths.research_data_root),
        "writer_lock": str(paths.writer_lock),
        "staging_root": str(paths.staging_root),
        "path_fingerprints": {
            "development": _path_fingerprint(paths.development_root),
            "runtime": _path_fingerprint(paths.runtime_root),
            "database": _path_fingerprint(paths.database_path),
            "report": _path_fingerprint(paths.report_root),
            "ui_report": _path_fingerprint(paths.ui_report_root),
            "research": _path_fingerprint(paths.research_data_root),
            "lock": _path_fingerprint(paths.writer_lock),
            "staging": _path_fingerprint(paths.staging_root),
        },
        "layout": {
            "intentional_separation": paths.development_root != paths.runtime_root,
            "runtime_import_matches": _inside(package_path, paths.runtime_root),
            "database_outside_checkouts": bool(
                paths.database_path
                and not _inside(paths.database_path, paths.development_root)
                and not _inside(paths.database_path, paths.runtime_root)
            ),
            "ui_report_is_publishing_path": paths.ui_report_root != paths.report_root,
        },
    }


def render_runtime_origin_markdown(payload: dict[str, Any]) -> str:
    database = payload["database"]
    layout = payload["layout"]
    lines = [
        "# Runtime Origin",
        "",
        f"Generated: `{payload['generated_at']}`",
        f"Status: `{payload['status']}`",
        f"All statuses: `{', '.join(payload['statuses'])}`",
        "",
        "## Source identity",
        "",
        f"- CWD: `{payload['cwd']}`",
        f"- Development root: `{payload['development_root']}`",
        f"- Runtime source: `{payload['runtime_source']}`",
        f"- Installed package: `{payload['installed_package_source']}`",
        f"- Branch: `{payload.get('branch') or 'unknown'}`",
        f"- Development SHA: `{payload.get('sha') or 'unknown'}`",
        f"- Deployed SHA: `{payload.get('deployed_sha') or 'unknown'}`",
        f"- Python: `{payload['python']}`",
        f"- Virtualenv: `{payload['virtualenv']}`",
        "",
        "## Persistent paths",
        "",
        f"- Database: `{database.get('path') or database['url']}`",
        f"- Report root: `{payload['report_root']}`",
        f"- UI report root: `{payload['ui_report_root']}`",
        f"- Research data root: `{payload['research_data_root']}`",
        f"- Writer lock: `{payload['writer_lock']}`",
        f"- Staging root: `{payload['staging_root']}`",
        "",
        "## Layout decision",
        "",
        f"- Intentional development/runtime separation: `{layout['intentional_separation']}`",
        f"- Installed package under runtime root: `{layout['runtime_import_matches']}`",
        f"- Database outside both code checkouts: `{layout['database_outside_checkouts']}`",
        f"- Separate UI publishing path: `{layout['ui_report_is_publishing_path']}`",
        "",
        "The separated layout is retained. Duplicate launch wrappers are not consolidated until "
        "the deployed manifest and this command both report `READY`.",
        "",
    ]
    return "\n".join(lines)


def _statuses(
    *,
    paths: RuntimePaths,
    cwd: Path,
    package_path: Path,
    development_sha: str | None,
    deployed_sha: str | None,
) -> list[str]:
    statuses: list[str] = []
    if not (_inside(cwd, paths.development_root) or _inside(cwd, paths.runtime_root)):
        statuses.append(PATH_MISMATCH)
    if not _inside(package_path, paths.runtime_root):
        statuses.append(EDITABLE_INSTALL_MISMATCH)
    if development_sha and deployed_sha and development_sha != deployed_sha:
        statuses.append(OLD_RUNTIME)
    if paths.database_path and (
        _inside(paths.database_path, paths.development_root)
        or _inside(paths.database_path, paths.runtime_root)
    ):
        statuses.append(DATABASE_PATH_MISMATCH)
    protected_paths = (
        paths.runtime_root,
        paths.database_path,
        paths.research_data_root,
        paths.writer_lock,
        paths.staging_root,
    )
    if any(path and "onedrive" in str(path).lower() for path in protected_paths):
        statuses.append(ONEDRIVE_REFERENCE)
    return statuses or [READY]


def _configured_path(value: str | None) -> Path | None:
    return Path(value).expanduser() if value else None


def _source_root(package_path: Path) -> Path:
    for candidate in package_path.parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return package_path.parents[2]


def _nearest_git_root(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _git_value(root: Path, *args: str) -> str | None:
    if not (root / ".git").exists():
        return None
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _deployment_manifest(runtime_root: Path) -> dict[str, Any] | None:
    path = runtime_root / ".kalshi-deployment.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _path_fingerprint(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    resolved = path.resolve()
    return {
        "path": str(resolved),
        "exists": resolved.exists(),
        "is_symlink": path.is_symlink(),
        "digest": hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16],
    }
