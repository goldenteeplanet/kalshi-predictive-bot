from __future__ import annotations

import csv
import json
import os
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.phase3bb_acceleration import (
    _metadata,
    _metadata_lines,
    _read_json,
    _safety_flags,
    _write_manifest,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R12_VERSION = "phase3bb_r12_cloud_bootstrap_verification_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r12")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 45

REMOTE_COMMANDS_THAT_MAY_WRITE_REPORTS = {
    "phase3bc-r5-status",
    "phase3ba-status",
}


@dataclass(frozen=True)
class CloudBootstrapTarget:
    ssh_target: str
    identity_file: str
    app_path: str
    env_path: str
    db_path: str
    reports_path: str


@dataclass(frozen=True)
class RemoteProbe:
    name: str
    command: str
    timeout_seconds: int


@dataclass(frozen=True)
class RemoteProbeResult:
    name: str
    command: str
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False


@dataclass(frozen=True)
class Phase3BBR12CloudBootstrapArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    next_actions_path: Path
    manifest_path: Path


ProbeRunner = Callable[[RemoteProbe, CloudBootstrapTarget], RemoteProbeResult]


def write_phase3bb_r12_cloud_bootstrap_verification_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR12CloudBootstrapArtifacts:
    payload = build_phase3bb_r12_cloud_bootstrap_verification(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_bootstrap_verification.md"
    json_path = output_dir / "cloud_bootstrap_verification.json"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_probe_csv(probe_csv_path, payload["remote_probe_results"])
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            probe_csv_path,
            next_actions_path,
        ],
    )
    return Phase3BBR12CloudBootstrapArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        probe_csv_path=probe_csv_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r12_cloud_bootstrap_verification(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    now = utc_now()
    metadata = _metadata(
        session,
        settings=resolved,
        generated_at=now.isoformat(),
        command_args=command_args or [],
        output_dir=output_dir,
    )
    metadata["command_arguments"] = {
        "command": "kalshi-bot phase3bb-r12-cloud-bootstrap-verification",
        "argv": command_args or [],
    }
    r11_context = _read_json(reports_dir / "phase3bb_r11" / "codex_cloud_context.json")
    target = _resolve_target(
        r11_context,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
    )
    runner = probe_runner or _run_ssh_probe
    probes = _build_remote_probes(target, timeout_seconds=per_probe_timeout_seconds)
    started = time.monotonic()
    results = [runner(probe, target) for probe in probes]
    duration = round(time.monotonic() - started, 3)
    parsed = _parse_probe_outputs(results)
    decision = _decide_bootstrap_status(results, parsed)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "no_deploy": True,
        "remote_commands_executed": len(results),
        "remote_report_writes_only": True,
        "remote_db_writes_performed": 0,
        "secrets_printed": False,
        "secrets_copied": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "creates_paper_trades": False,
        "creates_paper_orders": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "db_writes_performed": 0,
    }
    return {
        **metadata,
        "phase": "3BB-R12-CLOUD-BOOTSTRAP-VERIFICATION",
        "phase_version": PHASE3BB_R12_VERSION,
        "mode": "PAPER_READ_ONLY_REMOTE_BOOTSTRAP_VERIFICATION",
        "reports_dir": str(reports_dir),
        "r11_context_available": bool(r11_context),
        "cloud_target": {
            "ssh_target": target.ssh_target,
            "identity_file": target.identity_file,
            "app_path": target.app_path,
            "env_path": target.env_path,
            "db_path": target.db_path,
            "reports_path": target.reports_path,
        },
        "remote_probe_duration_seconds": duration,
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_remote_state": parsed,
        "bootstrap_decision": decision,
        "next_operator_command": decision["next_operator_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _resolve_target(
    r11_context: dict[str, Any],
    *,
    ssh_target: str | None,
    identity_file: str | None,
    app_path: str | None,
    env_path: str | None,
    db_path: str | None,
) -> CloudBootstrapTarget:
    ssh_profile = r11_context.get("ssh_profile") or {}
    paths = r11_context.get("remote_paths") or {}
    user = str(ssh_profile.get("user") or "kalshi")
    host = str(ssh_profile.get("host") or "159.65.35.72")
    resolved_ssh_target = ssh_target or f"{user}@{host}"
    resolved_identity = identity_file or str(
        ssh_profile.get("identity_file") or "~/.ssh/id_ed25519"
    )
    resolved_app = app_path or str(paths.get("app_path") or "/opt/kalshi-predictive-bot")
    resolved_env = env_path or str(paths.get("env_path") or "/etc/kalshi-bot/kalshi-bot.env")
    resolved_db = db_path or str(paths.get("db_path") or "/var/lib/kalshi-bot/kalshi_phase1.db")
    reports_path = str(paths.get("reports_path") or f"{resolved_app.rstrip('/')}/reports")
    return CloudBootstrapTarget(
        ssh_target=resolved_ssh_target,
        identity_file=resolved_identity,
        app_path=resolved_app,
        env_path=resolved_env,
        db_path=resolved_db,
        reports_path=reports_path,
    )


def _build_remote_probes(
    target: CloudBootstrapTarget,
    *,
    timeout_seconds: int,
) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    db = shlex.quote(target.db_path)
    reports = shlex.quote(target.reports_path)
    source_env = f"set -a && . {env} && set +a"
    env_probe = _env_probe_python_command()
    registry_commands = [
        "db-writer-monitor",
        "phase3ba-status",
        "phase3bb-r1-operator-scheduler",
        "phase3bc-r5-status",
    ]
    registry_loop = " ".join(shlex.quote(command) for command in registry_commands)
    return [
        RemoteProbe(
            "ssh_identity",
            "hostname && whoami",
            timeout_seconds,
        ),
        RemoteProbe(
            "os_python",
            (
                "uname -srm; "
                "if command -v lsb_release >/dev/null 2>&1; then "
                "lsb_release -ds; else . /etc/os-release && printf '%s\\n' \"$PRETTY_NAME\"; fi; "
                "command -v python3; python3 --version"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "repo",
            (
                f"test -d {app} && cd {app} && pwd && "
                "if test -d .git; then "
                "git rev-parse --short HEAD 2>/dev/null || true; echo GIT_METADATA_OK; "
                "else echo GIT_METADATA_MISSING; fi; "
                "echo REPO_PATH_OK"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "venv_cli",
            (
                f"test -x {app}/.venv/bin/python && {app}/.venv/bin/python --version && "
                f"test -x {app}/.venv/bin/kalshi-bot && "
                f"{app}/.venv/bin/kalshi-bot --help >/tmp/phase3bb_r12_kalshi_help.txt && "
                "echo VENV_CLI_OK"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "env_flags",
            f"cd {app} && test -f {env} && {source_env} && .venv/bin/python -c {env_probe}",
            timeout_seconds,
        ),
        RemoteProbe(
            "db_path",
            f"test -f {db} && test -r {db} && echo DB_PATH_OK",
            timeout_seconds,
        ),
        RemoteProbe(
            "reports_path",
            f"test -d {reports} && test -w {reports} && echo REPORTS_PATH_OK",
            timeout_seconds,
        ),
        RemoteProbe(
            "db_writer_monitor",
            f"cd {app} && {source_env} && .venv/bin/kalshi-bot db-writer-monitor --json",
            timeout_seconds,
        ),
        RemoteProbe(
            "r5_status",
            (
                f"cd {app} && {source_env} && "
                ".venv/bin/kalshi-bot phase3bc-r5-status "
                "--output-dir reports/phase3bc_r5 >/tmp/phase3bb_r12_r5_status.out && "
                "cat reports/phase3bc_r5/phase3bc_r5_status.json"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "phase3ba_status",
            (
                f"cd {app} && {source_env} && "
                ".venv/bin/kalshi-bot phase3ba-status "
                "--output-dir reports/phase3ba_status --reports-dir reports "
                ">/tmp/phase3bb_r12_phase3ba_status.out && "
                "cat reports/phase3ba_status/status.json"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "command_registry",
            (
                f"cd {app} && for cmd in {registry_loop}; do "
                ".venv/bin/kalshi-bot \"$cmd\" --help >/dev/null || exit 30; "
                "done; echo COMMAND_REGISTRY_OK"
            ),
            timeout_seconds,
        ),
    ]


def _env_probe_python_command() -> str:
    code = r"""
import json
import os

truthy = {"1", "true", "yes", "on", "enabled"}
safe_envs = {"demo", "paper", "paper_only", "sandbox", "read_only", "readonly", "test"}
kalshi_env = (os.getenv("KALSHI_ENV") or "").strip()
db_url = (os.getenv("DATABASE_URL") or os.getenv("KALSHI_DB_URL") or "").strip()
danger_names = [
    "LIVE_TRADING_ENABLED",
    "KALSHI_LIVE_TRADING",
    "ENABLE_LIVE_TRADING",
    "ALLOW_LIVE_ORDERS",
    "AUTOPILOT_LIVE_ORDERS",
    "DEMO_TRADING_ENABLED",
    "ALLOW_DEMO_ORDERS",
    "ENABLE_DEMO_ORDERS",
    "PAPER_TRADE_AUTOCREATE",
]
danger_truthy = [
    name for name in danger_names if (os.getenv(name) or "").strip().lower() in truthy
]
payload = {
    "kalshi_env_present": bool(kalshi_env),
    "kalshi_env_class": (
        "safe" if kalshi_env.lower() in safe_envs else
        "default_demo" if not kalshi_env else
        "review"
    ),
    "database_url_present": bool(db_url),
    "database_backend_class": (
        "sqlite" if db_url.startswith("sqlite") else
        "postgres" if db_url.startswith("postgres") else
        "present_redacted" if db_url else
        "missing"
    ),
    "danger_truthy_flags": danger_truthy,
    "paper_read_only_pass": (not kalshi_env or kalshi_env.lower() in safe_envs)
    and not danger_truthy,
}
print(json.dumps(payload, sort_keys=True))
"""
    return shlex.quote(code)


def _run_ssh_probe(probe: RemoteProbe, target: CloudBootstrapTarget) -> RemoteProbeResult:
    started = time.monotonic()
    identity_file = os.path.expanduser(target.identity_file)
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        identity_file,
        target.ssh_target,
        probe.command,
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - ssh argv is operator-provided target only.
            command,
            capture_output=True,
            text=True,
            timeout=probe.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return RemoteProbeResult(
            name=probe.name,
            command=probe.command,
            ok=False,
            exit_code=None,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "probe timed out",
            duration_seconds=round(time.monotonic() - started, 3),
            timed_out=True,
        )
    return RemoteProbeResult(
        name=probe.name,
        command=probe.command,
        ok=completed.returncode == 0,
        exit_code=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_seconds=round(time.monotonic() - started, 3),
    )


def _parse_probe_outputs(results: list[RemoteProbeResult]) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    env_flags = _json_from_probe(by_name.get("env_flags"))
    writer = _json_from_probe(by_name.get("db_writer_monitor"))
    if not writer:
        writer = _loose_db_writer_state(by_name.get("db_writer_monitor"))
    r5_status = _json_from_probe(by_name.get("r5_status"))
    phase3ba_status = _json_from_probe(by_name.get("phase3ba_status"))
    r5_process = r5_status.get("process") if isinstance(r5_status, dict) else {}
    r5_pids = list((r5_process or {}).get("phase3bc_r5_pids") or [])
    return {
        "env_flags": env_flags,
        "db_writer_monitor": writer,
        "r5_status": r5_status,
        "phase3ba_status": phase3ba_status,
        "r5_running": bool((r5_process or {}).get("phase3bc_r5_process_running")),
        "r5_pids": r5_pids,
        "duplicate_r5": len(r5_pids) > 1,
        "writer_safe_to_start_write": bool(writer.get("safe_to_start_write"))
        if isinstance(writer, dict)
        else False,
        "writer_status": writer.get("status") if isinstance(writer, dict) else "UNKNOWN",
    }


def _json_from_probe(result: RemoteProbeResult | None) -> dict[str, Any]:
    if result is None or not result.ok:
        return {}
    text = result.stdout.strip()
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start >= 0:
        try:
            parsed, _end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(parsed, dict):
            return parsed
        start = text.find("{", start + 1)
    return {}


def _loose_db_writer_state(result: RemoteProbeResult | None) -> dict[str, Any]:
    if result is None or not result.ok:
        return {}
    text = result.stdout
    safe_match = re.search(r'"safe_to_start_write"\s*:\s*(true|false)', text)
    status_matches = re.findall(r'"status"\s*:\s*"([^"]+)"', text)
    pid_match = re.search(r'"current_writer_pid"\s*:\s*(null|\d+)', text)
    if not safe_match and not status_matches:
        return {}
    return {
        "safe_to_start_write": safe_match.group(1) == "true" if safe_match else False,
        "status": status_matches[-1] if status_matches else "UNKNOWN",
        "current_writer_pid": None
        if not pid_match or pid_match.group(1) == "null"
        else int(pid_match.group(1)),
        "parsed_with_loose_fallback": True,
    }


def _decide_bootstrap_status(
    results: list[RemoteProbeResult],
    parsed: dict[str, Any],
) -> dict[str, Any]:
    failed = [result.name for result in results if not result.ok]
    critical = {
        "ssh_identity",
        "repo",
        "venv_cli",
        "env_flags",
        "db_path",
        "reports_path",
        "db_writer_monitor",
        "r5_status",
        "phase3ba_status",
        "command_registry",
    }
    failed_critical = [name for name in failed if name in critical]
    env_flags = parsed.get("env_flags") or {}
    env_safe = bool(env_flags.get("paper_read_only_pass"))
    duplicate_r5 = bool(parsed.get("duplicate_r5"))
    writer_safe = bool(parsed.get("writer_safe_to_start_write"))

    if "ssh_identity" in failed:
        status = "SSH_BLOCKED"
        reason = "SSH did not connect with the configured key."
        next_command = "Fix SSH identity/authorized_keys, then rerun Phase 3BB-R12."
    elif failed_critical:
        status = "BOOTSTRAP_BLOCKED"
        reason = f"Critical remote probes failed: {', '.join(failed_critical)}."
        next_command = "Fix the failed remote probe, then rerun Phase 3BB-R12."
    elif not env_safe:
        status = "UNSAFE_ENV_BLOCKED"
        reason = "Remote env is not classified as paper/read-only safe."
        next_command = "Review remote env safety flags before any scheduler work."
    elif duplicate_r5:
        status = "DUPLICATE_R5_BLOCKED"
        reason = "More than one R5 watcher appears to be running."
        next_command = (
            "Run cloud status only; do not start scheduler until duplicate R5 is cleared."
        )
    elif not writer_safe:
        status = "WRITER_ACTIVE_WAIT"
        reason = "Remote db-writer-monitor does not report safe_to_start_write=true."
        next_command = (
            "Wait or run the report-only operator scheduler; "
            "do not start a background scheduler yet."
        )
    else:
        status = "READY_FOR_OPERATOR_SCHEDULER"
        reason = "Remote bootstrap checks passed and no active DB writer is blocking startup."
        next_command = (
            "kalshi-bot phase3bb-r1-operator-scheduler "
            "--output-dir reports/phase3bb_r1 --reports-dir reports"
        )
    return {
        "status": status,
        "ready_for_scheduler": status == "READY_FOR_OPERATOR_SCHEDULER",
        "primary_reason": reason,
        "failed_probes": failed,
        "failed_critical_probes": failed_critical,
        "env_safe": env_safe,
        "duplicate_r5": duplicate_r5,
        "writer_safe_to_start_write": writer_safe,
        "next_operator_command": next_command,
    }


def _result_payload(result: RemoteProbeResult) -> dict[str, Any]:
    return {
        "name": result.name,
        "command": result.command,
        "ok": result.ok,
        "exit_code": result.exit_code,
        "duration_seconds": result.duration_seconds,
        "timed_out": result.timed_out,
        "stdout_excerpt": _safe_excerpt(result.stdout),
        "stderr_excerpt": _safe_excerpt(result.stderr),
    }


def _safe_excerpt(text: str, *, limit: int = 2000) -> str:
    if not text:
        return ""
    redacted_lines = []
    for line in text.splitlines():
        upper = line.upper()
        if any(token in upper for token in ("SECRET=", "TOKEN=", "PASSWORD=", "PRIVATE_KEY=")):
            redacted_lines.append("[REDACTED_SECRET_LINE]")
        else:
            redacted_lines.append(line)
    return "\n".join(redacted_lines)[:limit]


def _write_probe_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "name",
        "ok",
        "exit_code",
        "duration_seconds",
        "timed_out",
        "stdout_excerpt",
        "stderr_excerpt",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R12 Cloud Bootstrap Verification")
    decision = payload["bootstrap_decision"]
    target = payload["cloud_target"]
    parsed = payload["parsed_remote_state"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Ready for scheduler: `{decision['ready_for_scheduler']}`",
            f"- Primary reason: {decision['primary_reason']}",
            f"- SSH target: `{target['ssh_target']}`",
            f"- Remote app path: `{target['app_path']}`",
            f"- Remote DB path: `{target['db_path']}`",
            "",
            "## Remote State",
            "",
            f"- Writer status: `{parsed.get('writer_status')}`",
            f"- Writer safe_to_start_write: `{parsed.get('writer_safe_to_start_write')}`",
            f"- R5 running: `{parsed.get('r5_running')}`",
            f"- R5 PIDs: `{parsed.get('r5_pids')}`",
            f"- Duplicate R5: `{parsed.get('duplicate_r5')}`",
            f"- Env safe: `{decision['env_safe']}`",
            "",
            "## Safety",
            "",
            "- No scheduler was started.",
            "- No deployment was performed.",
            "- Env contents were not printed.",
            "- No paper/live/demo trades were created.",
            "",
            "## Next Command",
            "",
            f"```bash\n{decision['next_operator_command']}\n```",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R12 Cloud Bootstrap Detail")
    lines.extend(["", "## Probe Results", ""])
    for result in payload["remote_probe_results"]:
        lines.append(
            f"- `{result['name']}` ok=`{result['ok']}` exit=`{result['exit_code']}` "
            f"duration=`{result['duration_seconds']}`"
        )
        if not result["ok"] and result["stderr_excerpt"]:
            lines.append(f"  Blocker: `{result['stderr_excerpt'][:240]}`")
    lines.extend(["", "## Parsed Remote State", ""])
    lines.append("```json")
    lines.append(json.dumps(payload["parsed_remote_state"], indent=2, sort_keys=True))
    lines.append("```")
    lines.extend(
        [
            "",
            "## Commands That May Write Remote Reports",
            "",
        ]
    )
    for command in sorted(REMOTE_COMMANDS_THAT_MAY_WRITE_REPORTS):
        lines.append(f"- `{command}`")
    lines.extend(
        [
            "",
            "These commands write report artifacts only. They do not create DB rows, "
            "paper trades, or exchange orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R12 Next Actions")
    decision = payload["bootstrap_decision"]
    lines.extend(
        [
            "",
            "## Next Operator Action",
            "",
            f"- Status: `{decision['status']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "```bash",
            decision["next_operator_command"],
            "```",
            "",
            "## Do Not Run Yet",
            "",
            "- Do not start a background scheduler until this report is ready.",
            "- Do not start a duplicate R5 watcher.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
            "- Do not print or copy env secrets.",
        ]
    )
    return "\n".join(lines) + "\n"
