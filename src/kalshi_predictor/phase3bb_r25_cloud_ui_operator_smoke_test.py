from __future__ import annotations

import csv
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.phase3bb_acceleration import (
    _metadata,
    _metadata_lines,
    _read_json,
    _safety_flags,
    _write_manifest,
)
from kalshi_predictor.phase3bb_r18_cloud_scheduler_runtime_cutover import DEFAULT_REPORTS_DIR
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R25_VERSION = "phase3bb_r25_cloud_ui_operator_smoke_test_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r25")
DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:8081"
DEFAULT_TIMEOUT_SECONDS = 60
TEXT_FAILURE_MARKERS = (
    "Traceback (most recent call last)",
    "Internal Server Error",
    "Database error",
    "Database is busy",
    "Application startup failed",
)


@dataclass(frozen=True)
class LocalHttpProbe:
    name: str
    method: str
    path: str
    expected_statuses: tuple[int, ...]
    kind: str
    timeout_seconds: int
    body: bytes | None = None
    content_type: str | None = None
    must_contain: tuple[str, ...] = ()
    must_not_contain: tuple[str, ...] = TEXT_FAILURE_MARKERS


@dataclass(frozen=True)
class LocalHttpResult:
    name: str
    method: str
    path: str
    url: str
    ok: bool
    status_code: int | None
    content_type: str
    duration_seconds: float
    final_url: str
    body_sha256: str
    body_excerpt: str
    error: str


@dataclass(frozen=True)
class Phase3BBR25CloudUiOperatorSmokeTestArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    results_csv_path: Path
    checks_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


LocalProbeRunner = Callable[[LocalHttpProbe, str], LocalHttpResult]


def write_phase3bb_r25_cloud_ui_operator_smoke_test_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    local_base_url: str = DEFAULT_LOCAL_BASE_URL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    probe_runner: LocalProbeRunner | None = None,
) -> Phase3BBR25CloudUiOperatorSmokeTestArtifacts:
    payload = build_phase3bb_r25_cloud_ui_operator_smoke_test(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        local_base_url=local_base_url,
        timeout_seconds=timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_ui_operator_smoke_test.md"
    json_path = output_dir / "cloud_ui_operator_smoke_test.json"
    results_csv_path = output_dir / "local_ui_smoke_results.csv"
    checks_csv_path = output_dir / "smoke_checks.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_results_csv(results_csv_path, payload["local_ui_smoke_results"])
    _write_checks_csv(checks_csv_path, payload["smoke_checks"])
    operator_command_path.write_text(_render_operator_command(payload), encoding="utf-8")
    _mark_executable(operator_command_path)
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            results_csv_path,
            checks_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR25CloudUiOperatorSmokeTestArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        results_csv_path=results_csv_path,
        checks_csv_path=checks_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r25_cloud_ui_operator_smoke_test(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    local_base_url: str = DEFAULT_LOCAL_BASE_URL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    probe_runner: LocalProbeRunner | None = None,
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
        "command": "kalshi-bot phase3bb-r25-cloud-ui-operator-smoke-test",
        "argv": command_args or [],
    }
    r24_path = reports_dir / "phase3bb_r24" / "cloud_ui_start_tunnel_verification.json"
    r24 = _read_json(r24_path)
    probes = _default_smoke_probes(timeout_seconds=timeout_seconds)
    runner = probe_runner or _run_local_http_probe
    started = time.monotonic()
    results = [runner(probe, local_base_url) for probe in probes]
    duration = round(time.monotonic() - started, 3)
    result_payloads = [_result_payload(result) for result in results]
    prechecks = _prechecks(r24, local_base_url)
    route_checks = [
        _route_check(probe, result)
        for probe, result in zip(probes, results, strict=True)
    ]
    checks = prechecks + route_checks
    decision = _decision(checks, r24, result_payloads, local_base_url)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "local_http_read_only_smoke": True,
        "local_http_requests_executed": len(results),
        "http_get_requests": sum(1 for probe in probes if probe.method == "GET"),
        "http_post_requests": sum(1 for probe in probes if probe.method == "POST"),
        "remote_commands_executed": 0,
        "remote_db_writes_performed": 0,
        "db_writes_performed": 0,
        "service_files_written_to_system": False,
        "systemctl_mutating_commands_executed": 0,
        "systemctl_read_only_commands_executed": 0,
        "starts_ui_service": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "stops_processes": False,
        "nginx_or_firewall_changed": False,
        "secrets_printed": False,
        "creates_paper_trades": False,
        "creates_paper_orders": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
    }
    return {
        **metadata,
        "phase": "3BB-R25-CLOUD-UI-OPERATOR-SMOKE-TEST",
        "phase_version": PHASE3BB_R25_VERSION,
        "mode": "PAPER_READ_ONLY_LOCAL_TUNNEL_UI_SMOKE_TEST",
        "reports_dir": str(reports_dir),
        "r24_artifact_path": str(r24_path),
        "r24_context_available": bool(r24),
        "local_base_url": local_base_url.rstrip("/"),
        "smoke_probe_duration_seconds": duration,
        "local_ui_smoke_results": result_payloads,
        "smoke_checks": checks,
        "smoke_decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _default_smoke_probes(*, timeout_seconds: int) -> list[LocalHttpProbe]:
    html_common = ("Kalshi",)
    return [
        LocalHttpProbe(
            "today_workspace",
            "GET",
            "/",
            (200,),
            "html",
            timeout_seconds,
            must_contain=html_common,
        ),
        LocalHttpProbe(
            "opportunities",
            "GET",
            "/opportunities",
            (200,),
            "html",
            timeout_seconds,
            must_contain=("Opportunities",),
        ),
        LocalHttpProbe(
            "markets",
            "GET",
            "/markets",
            (200,),
            "html",
            timeout_seconds,
            must_contain=("Markets",),
        ),
        LocalHttpProbe(
            "system_health",
            "GET",
            "/system",
            (200,),
            "html",
            timeout_seconds,
            must_contain=("System",),
        ),
        LocalHttpProbe(
            "link_coverage",
            "GET",
            "/links/coverage",
            (200,),
            "html",
            timeout_seconds,
            must_contain=("Coverage",),
        ),
        LocalHttpProbe(
            "portfolio",
            "GET",
            "/portfolio",
            (200,),
            "html",
            timeout_seconds,
            must_contain=("Portfolio",),
        ),
        LocalHttpProbe(
            "models",
            "GET",
            "/models",
            (200,),
            "html",
            timeout_seconds,
            must_contain=("Model",),
        ),
        LocalHttpProbe(
            "settings",
            "GET",
            "/settings",
            (200,),
            "html",
            timeout_seconds,
            must_contain=("Settings",),
        ),
        LocalHttpProbe(
            "db_writer_api",
            "GET",
            "/api/db-writer-monitor",
            (200,),
            "json",
            timeout_seconds,
            must_contain=('"ok"', '"read_only"'),
        ),
        LocalHttpProbe(
            "workspace_guard_api",
            "GET",
            "/api/workspace-guard",
            (200,),
            "json",
            timeout_seconds,
            must_contain=('"ok"', '"guard"'),
        ),
        LocalHttpProbe(
            "dashboard_snapshot_api",
            "GET",
            "/api/dashboard/v1/snapshots/current",
            (200,),
            "json",
            timeout_seconds,
            must_contain=('"request_id"', '"dashboard_snapshot_id"'),
        ),
    ]


def _run_local_http_probe(probe: LocalHttpProbe, base_url: str) -> LocalHttpResult:
    url = _join_url(base_url, probe.path)
    started = time.monotonic()
    headers = {"User-Agent": "kalshi-bot-phase3bb-r25-smoke-test/1.0"}
    if probe.content_type:
        headers["Content-Type"] = probe.content_type
    request = urllib.request.Request(
        url,
        data=probe.body,
        method=probe.method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=probe.timeout_seconds) as response:  # noqa: S310 - operator-local tunnel smoke URL only.
            body = response.read()
            return _local_result(
                probe,
                url=url,
                status_code=response.status,
                content_type=response.headers.get("Content-Type", ""),
                final_url=response.geturl(),
                body=body,
                error="",
                duration_seconds=round(time.monotonic() - started, 3),
            )
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp else b""
        return _local_result(
            probe,
            url=url,
            status_code=exc.code,
            content_type=exc.headers.get("Content-Type", "") if exc.headers else "",
            final_url=exc.url,
            body=body,
            error=str(exc),
            duration_seconds=round(time.monotonic() - started, 3),
        )
    except Exception as exc:  # noqa: BLE001 - smoke report must capture operator-local tunnel failures.
        return _local_result(
            probe,
            url=url,
            status_code=None,
            content_type="",
            final_url=url,
            body=b"",
            error=str(exc),
            duration_seconds=round(time.monotonic() - started, 3),
        )


def _local_result(
    probe: LocalHttpProbe,
    *,
    url: str,
    status_code: int | None,
    content_type: str,
    final_url: str,
    body: bytes,
    error: str,
    duration_seconds: float,
) -> LocalHttpResult:
    decoded = _decode_body(body, content_type)
    return LocalHttpResult(
        name=probe.name,
        method=probe.method,
        path=probe.path,
        url=url,
        ok=status_code is not None and 200 <= status_code < 400 and not error,
        status_code=status_code,
        content_type=content_type,
        duration_seconds=duration_seconds,
        final_url=final_url,
        body_sha256=hashlib.sha256(body).hexdigest(),
        body_excerpt=decoded[:4096],
        error=error,
    )


def _prechecks(r24: dict[str, Any], local_base_url: str) -> list[dict[str, Any]]:
    r24_decision = r24.get("verification_decision") or {}
    return [
        _check("r24_artifact_present", bool(r24), "R24 start/tunnel verification artifact exists."),
        _check(
            "r24_tunnel_ready",
            r24_decision.get("status") == "VERIFIED_UI_RUNNING_SSH_TUNNEL_READY",
            f"R24 status is {r24_decision.get('status')}.",
        ),
        _check(
            "local_base_url_is_loopback",
            _is_loopback_http_url(local_base_url),
            f"Local base URL is {local_base_url}.",
        ),
    ]


def _route_check(probe: LocalHttpProbe, result: LocalHttpResult) -> dict[str, Any]:
    body = result.body_excerpt
    missing = [needle for needle in probe.must_contain if needle not in body]
    forbidden = [needle for needle in probe.must_not_contain if needle in body]
    expected_status = result.status_code in probe.expected_statuses
    kind_ok = _kind_ok(probe.kind, result.content_type, body)
    passed = bool(
        result.ok
        and expected_status
        and kind_ok
        and not missing
        and not forbidden
    )
    detail_parts = [
        f"{probe.method} {probe.path}",
        f"status={result.status_code}",
        f"content_type={result.content_type or 'unknown'}",
        f"duration={result.duration_seconds}s",
    ]
    if result.error:
        detail_parts.append(f"error={result.error}")
    if missing:
        detail_parts.append(f"missing={','.join(missing)}")
    if forbidden:
        detail_parts.append(f"forbidden={','.join(forbidden)}")
    return _check(f"route_{probe.name}", passed, "; ".join(detail_parts))


def _decision(
    checks: list[dict[str, Any]],
    r24: dict[str, Any],
    results: list[dict[str, Any]],
    local_base_url: str,
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    route_failed = [row for row in failed if row["check"].startswith("route_")]
    connected = any(row.get("status_code") == 200 for row in results)
    r24_decision = r24.get("verification_decision") or {}
    if failed:
        if not connected:
            status = "BLOCKED_TUNNEL_NOT_REACHABLE"
            reason = "No smoke route returned HTTP 200 through the local SSH tunnel."
            next_step = "Phase 3BB-R25 - Fix SSH Tunnel Or UI Service"
        else:
            status = "BLOCKED_CLOUD_UI_OPERATOR_SMOKE_TEST"
            reason = f"First failing check: {failed[0]['check']}."
            next_step = "Phase 3BB-R25 - Fix Cloud UI Smoke Failure"
    else:
        status = "VERIFIED_CLOUD_UI_OPERATOR_SMOKE_PASS"
        reason = "The operator UI is reachable through the SSH tunnel and all read-only smoke probes passed."
        next_step = "Phase 3BB-R26 - Cloud UI Access Control And HTTPS Exposure Decision Gate"
    return {
        "status": status,
        "smoke_passed": not failed,
        "failed_check_count": len(failed),
        "failed_route_count": len(route_failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "r24_status": r24_decision.get("status"),
        "r5_pid": r24_decision.get("r5_pid"),
        "local_base_url": local_base_url.rstrip("/"),
        "routes_checked": len(results),
        "routes_http_200": sum(1 for row in results if row.get("status_code") == 200),
        "operator_next_command": f"Open {local_base_url.rstrip('/')} in your local browser.",
        "next_codex_step": next_step,
    }


def _kind_ok(kind: str, content_type: str, body: str) -> bool:
    lowered = content_type.lower()
    if kind == "json":
        if "json" in lowered:
            return True
        try:
            json.loads(body)
        except json.JSONDecodeError:
            return False
        return True
    if kind == "html":
        return "html" in lowered or "<html" in body.lower() or "<!doctype html" in body.lower()
    return True


def _is_loopback_http_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def _join_url(base_url: str, path: str) -> str:
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def _decode_body(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip() or charset
    return body.decode(charset, errors="replace")


def _result_payload(result: LocalHttpResult) -> dict[str, Any]:
    return {
        "name": result.name,
        "method": result.method,
        "path": result.path,
        "url": result.url,
        "ok": result.ok,
        "status_code": result.status_code,
        "content_type": result.content_type,
        "duration_seconds": result.duration_seconds,
        "final_url": result.final_url,
        "body_sha256": result.body_sha256,
        "body_excerpt": result.body_excerpt,
        "error": result.error,
    }


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R25 Cloud UI Operator Smoke Test")
    decision = payload["smoke_decision"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Smoke passed: `{decision['smoke_passed']}`",
            f"- Local base URL: `{decision['local_base_url']}`",
            f"- Routes checked: `{decision['routes_checked']}`",
            f"- HTTP 200 routes: `{decision['routes_http_200']}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Safety",
            "",
            "- Used the local SSH tunnel only.",
            "- Did not expose the UI publicly.",
            "- Did not start/stop R5.",
            "- Did not create paper/live/demo trades.",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R25 Smoke Test Detail")
    decision = payload["smoke_decision"]
    lines.extend(["", f"- Decision: `{decision['status']}`", "", "## Checks", ""])
    for row in payload["smoke_checks"]:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- `{marker}` `{row['check']}` - {row['detail']}")
    lines.extend(["", "## Routes", ""])
    for row in payload["local_ui_smoke_results"]:
        lines.append(
            f"- `{row['name']}` `{row['method']} {row['path']}` "
            f"status=`{row['status_code']}` duration=`{row['duration_seconds']}s`"
        )
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    decision = payload["smoke_decision"]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            f"printf '%s\\n' {_shell_quote(decision['operator_next_command'])}",
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R25 Next Actions")
    decision = payload["smoke_decision"]
    lines.extend(
        [
            "",
            "## Next Operator Action",
            "",
            f"- Status: `{decision['status']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- Command/action: `{decision['operator_next_command']}`",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "## Do Not Run",
            "",
            "- Do not expose the UI publicly yet.",
            "- Do not install nginx or open firewall ports yet.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_results_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = [
        "name",
        "method",
        "path",
        "url",
        "ok",
        "status_code",
        "content_type",
        "duration_seconds",
        "final_url",
        "body_sha256",
        "error",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_checks_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = ["check", "passed", "detail"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _mark_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        return
