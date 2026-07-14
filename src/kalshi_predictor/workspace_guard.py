from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import kalshi_predictor
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import (
    database_url_from_settings,
    detect_backend,
    redact_database_url,
    sqlite_path_from_url,
)
from kalshi_predictor.institutional_dashboard.contracts import query_hash
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.time import utc_now

PHASE3BB_VERSION = "phase3bb_workspace_guard_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb")
EXPECTED_WORKSPACE_COMMANDS = (
    "db-writer-monitor",
    "phase-orchestrator",
    "phase3aa-r2-exact-settlement-harvest",
    "phase3aa-r4-settlement-fetch-recovery",
    "phase3ah-round-placeholder-resolution",
    "phase3ah-sports-placeholder-watch",
    "phase3ay-health-refresh",
    "runtime-identity",
)


@dataclass(frozen=True)
class WorkspaceGuardArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


def build_workspace_consistency_guard(
    *,
    settings: Settings | None = None,
    expected_commands: Iterable[str] = EXPECTED_WORKSPACE_COMMANDS,
    registered_commands: Iterable[str] | None = None,
    cwd: Path | None = None,
    python_executable: Path | None = None,
    package_file: Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    runtime_env = env or dict(os.environ)
    package_path = (package_file or Path(kalshi_predictor.__file__)).resolve()
    package_dir = package_path.parent
    repo_root = _repo_root(package_path)
    cwd_path = (cwd or Path.cwd()).resolve()
    python_path = python_executable or Path(sys.executable).absolute()
    venv_path = _virtualenv_path(runtime_env)
    db_url = database_url_from_settings(resolved)
    redacted_db_url = redact_database_url(db_url)
    sqlite_path = sqlite_path_from_url(db_url)
    expected = sorted(set(expected_commands))
    source_commands = _source_registered_commands(package_dir / "cli.py")
    commands = sorted(set(registered_commands or source_commands))
    missing_commands = [command for command in expected if command not in commands]
    findings = _findings(
        repo_root=repo_root,
        cwd=cwd_path,
        python_executable=python_path,
        package_path=package_path,
        venv_path=venv_path,
        sqlite_path=sqlite_path,
        missing_commands=missing_commands,
    )
    status = _overall_status(findings)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3BB",
        "phase_version": PHASE3BB_VERSION,
        "mode": "READ_ONLY_WORKSPACE_BUILD_CONSISTENCY_GUARD",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "safety": {
            "live_or_demo_execution": False,
            "exchange_writes": False,
            "paper_pnl_writes": False,
        },
        "summary": {
            "status": status,
            "label": _status_label(status),
            "missing_required_commands": len(missing_commands),
            "finding_count": len(findings),
            "critical_findings": sum(1 for item in findings if item["severity"] == "CRITICAL"),
            "warning_findings": sum(1 for item in findings if item["severity"] == "WARNING"),
            "database_backend": detect_backend(resolved, db_url=db_url),
            "database_fingerprint": _database_fingerprint(redacted_db_url),
            "git_commit": _git_value(repo_root, "rev-parse", "--short", "HEAD") or "unknown",
        },
        "runtime": {
            "repository_root": str(repo_root),
            "current_working_directory": str(cwd_path),
            "python_executable": str(python_path),
            "virtualenv": str(venv_path) if venv_path else None,
            "package_path": str(package_path),
            "package_directory": str(package_dir),
            "git_branch": _git_value(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
            or "unknown",
            "git_commit": _git_value(repo_root, "rev-parse", "--short", "HEAD") or "unknown",
        },
        "database": {
            "backend": detect_backend(resolved, db_url=db_url),
            "database_url": redacted_db_url,
            "database_fingerprint": _database_fingerprint(redacted_db_url),
            "sqlite_path": str(sqlite_path) if sqlite_path else None,
            "sqlite_exists": bool(sqlite_path and sqlite_path.exists()),
            "sqlite_in_workspace_checkout": _sqlite_inside_checkout(sqlite_path),
        },
        "commands": {
            "expected_required_commands": expected,
            "registered_commands": commands,
            "missing_required_commands": missing_commands,
            "command_source": "cli_registry" if registered_commands is not None else "source_scan",
        },
        "findings": findings,
        "ui_badge": _ui_badge(status, findings),
        "next_action": _next_action(status, findings, missing_commands),
    }


def write_workspace_guard_report(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    settings: Settings | None = None,
    registered_commands: Iterable[str] | None = None,
) -> WorkspaceGuardArtifactSet:
    payload = build_workspace_consistency_guard(
        settings=settings,
        registered_commands=registered_commands,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3bb_workspace_guard.json"
    markdown_path = output_dir / "phase3bb_workspace_guard.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return WorkspaceGuardArtifactSet(output_dir, json_path, markdown_path)


def _findings(
    *,
    repo_root: Path,
    cwd: Path,
    python_executable: Path,
    package_path: Path,
    venv_path: Path | None,
    sqlite_path: Path | None,
    missing_commands: list[str],
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if not _path_inside(cwd, repo_root):
        findings.append(
            _finding(
                "WORKSPACE_CWD_MISMATCH",
                "CRITICAL",
                "Current terminal directory is not the package workspace.",
                f"Run cd {repo_root!s} before running kalshi-bot commands.",
            )
        )
    if not _path_inside(package_path, repo_root):
        findings.append(
            _finding(
                "PACKAGE_PATH_MISMATCH",
                "CRITICAL",
                "Imported kalshi_predictor package is not inside the detected workspace.",
                "Reactivate the checkout venv or reinstall the package in editable mode.",
            )
        )
    if venv_path is None:
        findings.append(
            _finding(
                "VENV_UNKNOWN",
                "WARNING",
                "No VIRTUAL_ENV value is set for this process.",
                "Activate the checkout .venv before running CLI commands.",
            )
        )
    elif not _path_inside(venv_path, repo_root):
        findings.append(
            _finding(
                "VENV_MISMATCH",
                "CRITICAL",
                "Active virtualenv is outside the package workspace.",
                f"Use source {repo_root / '.venv' / 'bin' / 'activate'}.",
            )
        )
    if missing_commands:
        findings.append(
            _finding(
                "STALE_COMMAND_BUILD",
                "CRITICAL",
                "This CLI build is missing required current-phase commands.",
                "Use the updated workspace checkout or rebuild the venv package.",
            )
        )
    if sqlite_path and _sqlite_inside_other_checkout(sqlite_path, repo_root):
        findings.append(
            _finding(
                "DATABASE_CHECKOUT_MISMATCH",
                "WARNING",
                "SQLite path appears to belong to another checkout.",
                "Point DATABASE_URL at the intended shared data path before refreshing producers.",
            )
        )
    if not _git_value(repo_root, "rev-parse", "--short", "HEAD"):
        findings.append(
            _finding(
                "GIT_COMMIT_UNKNOWN",
                "INFO",
                "Git commit is unavailable for this workspace copy.",
                (
                    "This is acceptable for a copied workspace, but reports will use "
                    "package path instead."
                ),
            )
        )
    if not findings:
        findings.append(
            _finding(
                "WORKSPACE_CONSISTENT",
                "INFO",
                "Workspace, venv, package, DB identity, and required commands are aligned.",
                "Continue using this checkout for CLI and UI work.",
            )
        )
    return findings


def _finding(code: str, severity: str, message: str, next_action: str) -> dict[str, str]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "next_action": next_action,
    }


def _overall_status(findings: list[dict[str, str]]) -> str:
    severities = {item["severity"] for item in findings}
    if "CRITICAL" in severities:
        return "BLOCKED"
    if "WARNING" in severities:
        return "DEGRADED"
    return "PASS"


def _status_label(status: str) -> str:
    if status == "PASS":
        return "Build OK"
    if status == "DEGRADED":
        return "Build Warn"
    return "Build Blocked"


def _ui_badge(status: str, findings: list[dict[str, str]]) -> dict[str, str]:
    classes = {
        "PASS": "status-healthy",
        "DEGRADED": "status-degraded",
        "BLOCKED": "status-blocked",
    }
    primary = next(
        (item for item in findings if item["severity"] in {"CRITICAL", "WARNING"}),
        findings[0],
    )
    return {
        "status": status,
        "label": _status_label(status),
        "class": classes.get(status, "status-unknown"),
        "description": primary["message"],
        "href": "/system",
    }


def _next_action(
    status: str,
    findings: list[dict[str, str]],
    missing_commands: list[str],
) -> str:
    if missing_commands:
        return (
            "Switch to the checkout whose CLI lists the missing command(s): "
            + ", ".join(missing_commands)
            + "."
        )
    if status == "PASS":
        return "Workspace guard is clear; continue from this checkout."
    for finding in findings:
        if finding["severity"] in {"CRITICAL", "WARNING"}:
            return finding["next_action"]
    return findings[0]["next_action"]


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    runtime = payload["runtime"]
    database = payload["database"]
    lines = [
        "# Phase 3BB Workspace Guard",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Status: {summary['status']}",
        f"- Label: {summary['label']}",
        f"- Safety: {payload['paper_only_safety']}",
        "- Live/demo execution: blocked",
        "",
        "## Runtime",
        "",
        f"- Repository root: `{runtime['repository_root']}`",
        f"- Current working directory: `{runtime['current_working_directory']}`",
        f"- Python executable: `{runtime['python_executable']}`",
        f"- Virtualenv: `{runtime['virtualenv'] or 'unknown'}`",
        f"- Package path: `{runtime['package_path']}`",
        f"- Git branch: `{runtime['git_branch']}`",
        f"- Git commit: `{runtime['git_commit']}`",
        "",
        "## Database",
        "",
        f"- Backend: {database['backend']}",
        f"- URL: `{database['database_url']}`",
        f"- Fingerprint: `{database['database_fingerprint']}`",
        f"- SQLite path: `{database['sqlite_path'] or 'n/a'}`",
        "",
        "## Commands",
        "",
        f"- Required commands missing: {summary['missing_required_commands']}",
    ]
    for command in payload["commands"]["missing_required_commands"]:
        lines.append(f"  - `{command}`")
    lines.extend(["", "## Findings", ""])
    for finding in payload["findings"]:
        lines.append(
            f"- **{finding['severity']} {finding['code']}**: "
            f"{finding['message']} Next: {finding['next_action']}"
        )
    lines.extend(["", "## Next Action", "", payload["next_action"], ""])
    return "\n".join(lines)


def _repo_root(package_path: Path) -> Path:
    for parent in package_path.parents:
        if (parent / "alembic.ini").exists() and (parent / "src").exists():
            return parent
    return package_path.parents[1]


def _virtualenv_path(env: dict[str, str]) -> Path | None:
    value = env.get("VIRTUAL_ENV")
    if value:
        return Path(value).expanduser().resolve()
    prefix = Path(sys.prefix).resolve()
    base_prefix = Path(getattr(sys, "base_prefix", sys.prefix)).resolve()
    return prefix if prefix != base_prefix else None


def _source_registered_commands(cli_path: Path) -> list[str]:
    if not cli_path.exists():
        return []
    text = cli_path.read_text(encoding="utf-8")
    return sorted(set(re.findall(r'@app\.command\(\s*"([^"]+)"', text)))


def _database_fingerprint(redacted_db_url: str) -> str:
    return query_hash({"database_url": redacted_db_url})


def _sqlite_inside_checkout(sqlite_path: Path | None) -> bool:
    if sqlite_path is None or str(sqlite_path) == ":memory:":
        return False
    parts = [part.lower() for part in sqlite_path.parts]
    return "kalshi-predictive-bot" in parts


def _sqlite_inside_other_checkout(sqlite_path: Path | None, repo_root: Path) -> bool:
    if sqlite_path is None or str(sqlite_path) == ":memory:":
        return False
    return _sqlite_inside_checkout(sqlite_path) and not _path_inside(sqlite_path, repo_root)


def _path_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _git_value(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return None
    return result.stdout.strip() or None
