from __future__ import annotations

import ast
import hashlib
import json
from collections import defaultdict
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

CURRENT_RESEARCH_MODULE_PATTERNS = (
    "phase3ag_crypto.py",
    "phase3ar*.py",
    "phase3bc*.py",
    "*crypto*.py",
    "*ranking*.py",
    "*snapshot*.py",
)

HISTORICAL_REPLAY_KEYWORDS = (
    "settlement",
    "backtest",
    "calibration",
    "walk_forward",
    "walkforward",
    "tournament",
    "replay",
)

GUARDED_PAPER_MODULE_PARTS = ("paper", "learning", "autopilot", "position_sizing")
GUARDED_PAPER_ROOT_KEYWORDS = (
    "paper",
    "learning",
    "autopilot",
    "phase3m",
    "phase3n",
    "risk",
    "pnl",
)
GUARDED_PAPER_STATEFUL_TOKENS = (
    "approval",
    "authorization",
    "fill",
    "idempotency",
    "order",
    "pending",
    "pnl",
    "position",
    "realize",
    "risk",
    "settlement",
    "sizing",
)


def build_wrapper_inventory(cli_path: Path | None = None) -> dict[str, Any]:
    path = cli_path or Path(__file__).with_name("cli.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wrappers = _command_wrappers(tree)
    lane_contract = build_lane_contract(path)
    weather = [row for row in wrappers if row["command"] in WEATHER_CHAIN_PREFIXES]
    duplicate_helpers = _current_research_duplicate_helpers(path.parent)
    replay_duplicate_helpers = _historical_replay_duplicate_helpers(path.parent)
    guarded_duplicates = _guarded_paper_duplicate_helpers(path.parent)
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
        "current_research_duplicate_helpers": duplicate_helpers,
        "historical_replay_duplicate_helpers": replay_duplicate_helpers,
        "guarded_paper_eligible_duplicate_helpers": guarded_duplicates["eligible"],
        "guarded_paper_isolated_duplicate_helpers": guarded_duplicates["isolated"],
        "guarded_paper_cross_lane_duplicates": guarded_duplicates["cross_lane"],
        "violations": violations,
        "consolidations": [
            {
                "canonical_module": "kalshi_predictor.phase3bb_weather_common",
                "scope": "Phase 3BB R42-R45 current-market research wrappers",
                "helpers": [
                    "check",
                    "first_line",
                    "legacy_check",
                    "mark_executable",
                    "stdout",
                    "tail",
                    "target_payload",
                    "write_legacy_probe_csv",
                    "write_probe_csv",
                    "write_rows_csv",
                    "write_sorted_rows_csv",
                ],
                "compatibility": (
                    "Command names, writer callables, outputs, and transactions unchanged."
                ),
            },
            {
                "canonical_module": "kalshi_predictor.current_research_common",
                "scope": "Crypto refresh, forecast, ranking, and snapshot-repair helpers",
                "helpers": [
                    "crypto_candidate_sort_key",
                    "decode_list",
                    "format_cents",
                    "int_from_float_or_none",
                    "int_or_none",
                    "latest_crypto_v2_forecast",
                    "latest_market_snapshot",
                    "latest_risk_decisions_by_ticker",
                    "markdown_cell",
                    "read_json",
                    "read_json_required",
                ],
                "compatibility": (
                    "Public commands and private compatibility aliases retain their prior call "
                    "signatures and return types."
                ),
            },
            {
                "canonical_module": "kalshi_predictor.historical_replay_common",
                "scope": "Settlement, calibration, walk-forward, tournament, and backtest helpers",
                "helpers": [
                    "has_usable_outcome",
                    "is_local_derived_composite_ticker",
                    "markdown_cell_empty",
                    "markdown_cell_none",
                    "normalize_result",
                    "settlement_to_y_true",
                    "source_is_closed_without_outcome",
                    "source_is_settled",
                    "trade_from_decision",
                ],
                "compatibility": (
                    "Historical commands retain their public names; no current forecast, paper, "
                    "or GH-2 writer imports this module."
                ),
            },
            {
                "canonical_module": "kalshi_predictor.guarded_paper_common",
                "scope": "Pure Guarded Paper parsing helpers",
                "helpers": ["int_or_zero"],
                "compatibility": (
                    "Learning and paper-readiness callers retain their private aliases; order, "
                    "fill, sizing, risk, settlement, and P&L writers remain isolated."
                ),
            },
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
    lines.extend(["", "## Remaining exact Current-Market Research duplicates", ""])
    if not payload["current_research_duplicate_helpers"]:
        lines.append("No exact helper-body duplicates remain in the scanned research families.")
    for group in payload["current_research_duplicate_helpers"]:
        members = ", ".join(f"`{item['module']}:{item['function']}`" for item in group["members"])
        lines.append(f"- `{group['fingerprint']}`: {members}")
    lines.extend(["", "## Remaining exact Historical Replay duplicates", ""])
    if not payload["historical_replay_duplicate_helpers"]:
        lines.append("No exact helper-body duplicates remain in the scanned replay families.")
    for group in payload["historical_replay_duplicate_helpers"]:
        members = ", ".join(f"`{item['module']}:{item['function']}`" for item in group["members"])
        lines.append(f"- `{group['fingerprint']}`: {members}")
    lines.extend(["", "## Guarded Paper eligible exact duplicates", ""])
    if not payload["guarded_paper_eligible_duplicate_helpers"]:
        lines.append("No eligible pure helper duplicates remain after consolidation.")
    for group in payload["guarded_paper_eligible_duplicate_helpers"]:
        members = ", ".join(f"`{item['module']}:{item['function']}`" for item in group["members"])
        lines.append(f"- `{group['fingerprint']}`: {members}")
    lines.extend(["", "## Guarded Paper stateful duplicates kept isolated", ""])
    for group in payload["guarded_paper_isolated_duplicate_helpers"]:
        members = ", ".join(f"`{item['module']}:{item['function']}`" for item in group["members"])
        lines.append(f"- `{group['fingerprint']}` ({group['reason']}): {members}")
    lines.extend(["", "## Cross-lane exact matches not merged", ""])
    for group in payload["guarded_paper_cross_lane_duplicates"]:
        members = ", ".join(f"`{item['module']}:{item['function']}`" for item in group["members"])
        lines.append(f"- {members}: {group['reason']}")
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


def _current_research_duplicate_helpers(root: Path) -> list[dict[str, Any]]:
    files: set[Path] = set()
    for pattern in CURRENT_RESEARCH_MODULE_PATTERNS:
        files.update(root.glob(pattern))
    return _duplicate_helper_groups(files, root)


def _historical_replay_duplicate_helpers(root: Path) -> list[dict[str, Any]]:
    files = {
        path
        for path in root.rglob("*.py")
        if "paper" not in path.relative_to(root).parts
        and (
            path.name.startswith("phase3aa")
            or any(
                keyword in path.name.lower() or keyword in str(path.parent).lower()
                for keyword in HISTORICAL_REPLAY_KEYWORDS
            )
        )
    }
    return _duplicate_helper_groups(files, root)


def _guarded_paper_duplicate_helpers(root: Path) -> dict[str, list[dict[str, Any]]]:
    files = {
        path
        for path in root.rglob("*.py")
        if any(part in GUARDED_PAPER_MODULE_PARTS for part in path.relative_to(root).parts[:-1])
        or (
            path.parent == root
            and any(keyword in path.name.lower() for keyword in GUARDED_PAPER_ROOT_KEYWORDS)
        )
    }
    files.add(root / "phase3bb_r33_cloud_paper_only_operations_readiness.py")
    groups = _duplicate_helper_groups(files, root)
    eligible: list[dict[str, Any]] = []
    isolated: list[dict[str, Any]] = []
    for group in groups:
        names = " ".join(member["function"].lower() for member in group["members"])
        if any(token in names for token in GUARDED_PAPER_STATEFUL_TOKENS):
            isolated.append(
                {
                    **group,
                    "disposition": "keep_isolated",
                    "reason": "stateful paper identity or writer contract",
                }
            )
        else:
            eligible.append(group)
    return {
        "eligible": eligible,
        "isolated": isolated,
        "cross_lane": [
            {
                "members": [
                    {"module": "paper_trading_gap.py", "function": "_read_json"},
                    {"module": "phase3bb_r3_activation.py", "function": "_read_json"},
                ],
                "disposition": "not_merged_cross_lane",
                "reason": (
                    "Guarded Paper and Current-Market Research artifact readers have "
                    "different lane ownership."
                ),
            }
        ],
    }


def _duplicate_helper_groups(files: set[Path], root: Path) -> list[dict[str, Any]]:
    fingerprints: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in sorted(files):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        helpers = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("_")
        }
        raw_fingerprints = {name: _helper_fingerprint(node) for name, node in helpers.items()}
        for name, node in helpers.items():
            normalized = ast.FunctionDef(
                name="_",
                args=node.args,
                body=node.body,
                decorator_list=[],
                returns=node.returns,
                type_comment=node.type_comment,
            )
            dependencies = sorted(
                raw_fingerprints[item.id]
                for item in ast.walk(node)
                if isinstance(item, ast.Name) and item.id != name and item.id in raw_fingerprints
            )
            material = ast.dump(normalized, include_attributes=False) + "|" + "|".join(dependencies)
            digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
            fingerprints[digest].append(
                {
                    "module": str(path.relative_to(root)),
                    "function": node.name,
                    "line": node.lineno,
                }
            )
    return [
        {"fingerprint": digest, "members": members, "disposition": "review_before_merge"}
        for digest, members in sorted(fingerprints.items())
        if len(members) > 1
    ]


def _helper_fingerprint(node: ast.FunctionDef) -> str:
    normalized = ast.FunctionDef(
        name="_",
        args=node.args,
        body=node.body,
        decorator_list=[],
        returns=node.returns,
        type_comment=node.type_comment,
    )
    return hashlib.sha256(
        ast.dump(normalized, include_attributes=False).encode("utf-8")
    ).hexdigest()
