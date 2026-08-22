from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from kalshi_predictor.system_lanes import build_lane_contract, command_owner
from kalshi_predictor.utils.time import utc_now

WEATHER_CHAIN_PREFIXES = (
    "phase3bb-r2-weather-fast-lane",
    "phase3bb-weather-fast-lane",
    "phase3bb-r42-weather-fast-lane-post-unblock-verification",
    "phase3bb-r43-weather-catalog-scheduler-hook",
    "phase3bb-r44-weather-catalog-hook-runtime-verification",
    "phase3bb-r45-weather-freshness-to-ranking-impact",
    "phase3bb-r46-cloud-scheduler-weather-writer-gate-repair",
    "phase3bb-r47-weather-current-window-series-discovery-linkability-repair",
    "phase3bb-r48-weather-feature-refresh-runtime-verification",
    "phase3bb-r49-weather-missing-link-apply-after-feature-refresh",
    "phase3bb-r50-weather-post-link-ranking-fast-lane-recheck",
    "phase3bb-r51-weather-ranking-path-repair",
    "phase3bb-r52-weather-ev-fair-value-diagnostic",
    "phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair",
    "phase3bb-r54-weather-missing-link-apply-deferral",
    "phase3bb-r55-weather-ranking-path-retry",
    "phase3bb-r57-weather-selected-window-pipeline-speed-repair",
    "phase3bb-r58-weather-selected-window-forecast-feature-alignment-repair",
    "phase3bb-r59-weather-catalog-refresh-r57-retry",
    "phase3bb-r60-weather-next-window-lead-time-scheduler-repair",
)


def build_wrapper_inventory(cli_path: Path | None = None) -> dict[str, Any]:
    path = cli_path or Path(__file__).with_name("cli.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wrappers = _command_wrappers(tree)
    lane_contract = build_lane_contract(path)
    weather = [row for row in wrappers if row["command"] in WEATHER_CHAIN_PREFIXES]
    lane_summary = {
        lane: {
            "commands": sum(1 for row in wrappers if row["lane"] == lane),
            "tables": counts["table"],
            "artifacts": counts["artifact"],
            "writers": counts["writer"],
            "counters": counts["counter"],
        }
        for lane, counts in lane_contract["lane_counts"].items()
    }
    violations = [row for row in weather if row["lane"] != "CURRENT_MARKET_RESEARCH"]
    return {
        "version": "canonical_wrapper_inventory_v1",
        "generated_at": utc_now().isoformat(),
        "status": "READY" if not violations else "LANE_VIOLATION",
        "lane_summary": lane_summary,
        "phase3bb_weather_chain": weather,
        "violations": violations,
        "consolidations": [
            {
                "canonical_module": "kalshi_predictor.phase3bb_weather_common",
                "scope": "Phase 3BB R42-R45 current-market research wrappers",
                "helpers": [
                    "check",
                    "first_line",
                    "mark_executable",
                    "stdout",
                    "target_payload",
                    "write_probe_csv",
                    "write_rows_csv",
                ],
                "compatibility": (
                    "Command names, writer callables, outputs, and transactions unchanged."
                ),
            }
        ],
        "deferred_command_merges": [
            {
                "commands": [row["command"] for row in weather],
                "reason": (
                    "The wrappers share a lane but call distinct stage writers and artifact "
                    "contracts; "
                    "therefore they are not behaviorally interchangeable."
                ),
            }
        ],
    }


def write_wrapper_inventory(*, json_path: Path, markdown_path: Path) -> dict[str, Any]:
    payload = build_wrapper_inventory()
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(render_wrapper_inventory_markdown(payload), encoding="utf-8")
    return payload


def render_wrapper_inventory_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Canonical Wrapper Inventory",
        "",
        f"Generated: `{payload['generated_at']}`",
        f"Status: `{payload['status']}`",
        "",
        "## Lane totals",
        "",
        "| Lane | Commands | Tables | Artifacts | Writers | Counters |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for lane, counts in payload["lane_summary"].items():
        lines.append(
            f"| {lane} | {counts['commands']} | {counts['tables']} | "
            f"{counts['artifacts']} | {counts['writers']} | {counts['counters']} |"
        )
    lines.extend(
        [
            "",
            "## Phase 3BB weather wrapper chain",
            "",
            "| Command | Writer callable | Transaction | Writer semantics | Disposition |",
            "|---|---|---|---|---|",
        ]
    )
    for row in payload["phase3bb_weather_chain"]:
        lines.append(
            f"| `{row['command']}` | `{row['writer_callable']}` | {row['transaction']} | "
            f"{row['writer_semantics']} | {row['disposition']} |"
        )
    lines.extend(["", "## Consolidated same-lane helpers", ""])
    for item in payload["consolidations"]:
        lines.append(
            f"- `{item['canonical_module']}` owns {', '.join(item['helpers'])}. "
            f"{item['compatibility']}"
        )
    lines.extend(
        [
            "",
            "## Command consolidation gate",
            "",
            "No Phase 3BB weather command was removed or aliased: each calls a distinct writer and "
            "retains a distinct artifact contract. Only byte-for-byte-equivalent same-lane helper "
            "implementations were consolidated.",
            "",
        ]
    )
    return "\n".join(lines)


def _command_wrappers(tree: ast.Module) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        command = _command_name(node)
        if command is None:
            continue
        calls = [_call_name(call) for call in ast.walk(node) if isinstance(call, ast.Call)]
        referenced_names = [item.id for item in ast.walk(node) if isinstance(item, ast.Name)]
        writers = [name for name in [*calls, *referenced_names] if name.startswith("write_")]
        transaction = (
            "commit" if "commit" in calls else "rollback" if "rollback" in calls else "none"
        )
        conditional_external = any(
            token in command for token in ("scheduler-hook", "writer-gate-repair")
        )
        semantics = (
            "conditional external scheduler writer"
            if conditional_external
            else "current-research database writer"
            if transaction == "commit"
            else "read-only diagnostic/report writer"
        )
        rows.append(
            {
                "command": command,
                "handler": node.name,
                "lane": command_owner(command),
                "writer_callable": writers[0] if writers else "none",
                "transaction": transaction,
                "writer_semantics": semantics,
                "disposition": "compatibility wrapper retained",
            }
        )
    return rows


def _command_name(node: ast.FunctionDef) -> str | None:
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
            continue
        if decorator.func.attr != "command" or not decorator.args:
            continue
        value = decorator.args[0]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return None


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""
