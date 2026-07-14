from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kalshi_predictor.utils.time import utc_now

PHASE_VERSION = "paper_trading_gap_analysis_v1"
PAPER_ONLY_SAFETY = "PAPER_ONLY_NO_EXCHANGE_WRITES"


@dataclass(frozen=True)
class PaperTradingGapArtifacts:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    next_commands_path: Path


def write_paper_trading_gap_analysis_report(
    *,
    output_dir: Path = Path("reports/paper_trading_gap"),
    reports_dir: Path = Path("reports"),
) -> PaperTradingGapArtifacts:
    payload = build_paper_trading_gap_analysis(reports_dir=reports_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "paper_trading_gap_analysis.json"
    markdown_path = output_dir / "paper_trading_gap_analysis.md"
    next_commands_path = output_dir / "NEXT_COMMANDS.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    next_commands_path.write_text(_render_next_commands(payload), encoding="utf-8")
    return PaperTradingGapArtifacts(
        output_dir=output_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        next_commands_path=next_commands_path,
    )


def build_paper_trading_gap_analysis(
    *,
    reports_dir: Path = Path("reports"),
    generated_at: Any | None = None,
) -> dict[str, Any]:
    generated = generated_at or utc_now()
    r5_status = _read_json(reports_dir / "phase3bc_r5" / "phase3bc_r5_status.json")
    r5_watch = _read_json(
        reports_dir / "phase3bc_r5" / "phase3bc_r5_crypto_freshness_watch.json"
    )
    funnel = _read_json(reports_dir / "phase3aw" / "current_crypto_funnel.json")
    r16 = _read_json(
        reports_dir
        / "phase3bc_r16"
        / "phase3bc_r16_crypto_paper_ready_edge_hunt.json"
    )
    r17 = _read_json(
        reports_dir
        / "phase3bc_r17"
        / "phase3bc_r17_crypto_liquidity_actionability.json"
    )
    sports = _read_json(reports_dir / "phase3ax" / "phase3ax_gap_analysis.json")

    status_guard = _dict(r5_status.get("guard"))
    status_summary = _dict(r5_status.get("latest_summary"))
    watch_summary = _dict(r5_watch.get("summary"))
    r16_summary = _dict(r16.get("summary"))
    r17_summary = _dict(r17.get("summary"))
    sports_summary = _dict(sports.get("summary"))

    facts = _facts(
        status_guard=status_guard,
        status_summary=status_summary,
        watch_summary=watch_summary,
        funnel=_dict(funnel),
        r16_summary=r16_summary,
        r17_summary=r17_summary,
        sports_summary=sports_summary,
    )
    phases = _phase_statuses(facts)
    remaining_gaps = _remaining_gaps(facts, sports_summary=sports_summary)
    next_commands = _next_commands(facts, phases=phases)
    summary = {
        "market_fill_ready": phases["crypto_market_fill"]["status"] == "DONE",
        "trade_ranking_ready": phases["trade_ranking"]["status"] == "DONE",
        "paper_trade_ready": facts["paper_ready_candidates"] > 0,
        "accelerate_learning_allowed": facts["paper_ready_candidates"] > 0,
        "current_blocker": _current_blocker(facts),
        "paper_ready_candidates": facts["paper_ready_candidates"],
        "positive_ev_rows": facts["positive_ev_rows"],
        "best_ev_candidate_ticker": facts["best_ev_candidate_ticker"],
        "best_current_expected_value_cents": facts["best_current_expected_value_cents"],
        "best_ev_gap_to_positive_cents": facts["best_ev_gap_to_positive_cents"],
    }
    return {
        "generated_at": generated.isoformat(),
        "phase": "PAPER_TRADING_GAP",
        "phase_version": PHASE_VERSION,
        "mode": "PAPER_ONLY_REPORT",
        "reports_dir": str(reports_dir),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "summary": summary,
        "facts": facts,
        "phase_statuses": phases,
        "remaining_gaps": remaining_gaps,
        "next_commands": next_commands,
        "do_not_run_yet": _do_not_run_yet(facts),
    }


def _facts(
    *,
    status_guard: dict[str, Any],
    status_summary: dict[str, Any],
    watch_summary: dict[str, Any],
    funnel: dict[str, Any],
    r16_summary: dict[str, Any],
    r17_summary: dict[str, Any],
    sports_summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "r5_status": status_guard.get("status"),
        "r5_running": bool(status_guard.get("running") or funnel.get("r5_running")),
        "r5_stale_report": bool(status_guard.get("stale_report") or funnel.get("r5_stale_report")),
        "watch_state": _first(
            status_guard.get("watch_state"),
            status_summary.get("watch_state"),
            watch_summary.get("watch_state"),
            funnel.get("watch_state"),
        ),
        "active_pure_crypto_rows": _first_int(
            status_summary.get("active_pure_crypto_rows"),
            watch_summary.get("active_pure_crypto_rows"),
            funnel.get("current_active_crypto_markets"),
        ),
        "current_active_window_rows": _first_int(
            status_summary.get("current_active_window_rows"),
            watch_summary.get("current_active_window_rows"),
        ),
        "expired_crypto_window_rows": _first_int(
            status_summary.get("expired_crypto_window_rows"),
            watch_summary.get("expired_crypto_window_rows"),
        ),
        "snapshot_backlog_status": _first(
            status_summary.get("snapshot_backlog_status"),
            watch_summary.get("snapshot_backlog_status"),
        ),
        "forecast_backlog_status": _first(
            status_summary.get("forecast_backlog_status"),
            watch_summary.get("forecast_backlog_status"),
        ),
        "snapshot_stale_rows": _first_int(
            status_guard.get("snapshot_stale_rows"),
            status_summary.get("snapshot_stale_rows"),
            watch_summary.get("snapshot_stale_rows"),
            funnel.get("snapshot_stale_rows"),
        ),
        "forecast_stale_rows": _first_int(
            status_guard.get("forecast_stale_rows"),
            status_summary.get("forecast_stale_rows"),
            watch_summary.get("forecast_stale_rows"),
            funnel.get("forecast_stale_rows"),
        ),
        "ranking_gap_after_repair": _first_int(
            status_guard.get("true_ranking_gap_after_repair"),
            status_summary.get("true_ranking_gap_after_repair"),
            watch_summary.get("true_ranking_gap_after_repair"),
            funnel.get("ranking_gap_after_repair"),
        ),
        "missing_or_stale_ranking_rows": _first_int(
            status_guard.get("missing_or_stale_ranking_rows"),
            status_summary.get("missing_or_stale_ranking_rows"),
            watch_summary.get("missing_or_stale_ranking_rows"),
        ),
        "positive_ev_rows": _first_int(
            status_guard.get("positive_ev_rows"),
            status_summary.get("positive_ev_rows"),
            watch_summary.get("positive_ev_rows"),
            r16_summary.get("positive_ev_rows"),
            r17_summary.get("positive_ev_rows"),
        ),
        "positive_ev_preflight_candidates": _first_int(
            status_guard.get("positive_ev_preflight_candidates"),
            status_summary.get("positive_ev_preflight_candidates"),
            watch_summary.get("positive_ev_preflight_candidates"),
        ),
        "clean_execution_rows": _first_int(
            status_guard.get("clean_execution_rows"),
            status_summary.get("clean_execution_rows"),
            watch_summary.get("clean_execution_rows"),
            r16_summary.get("clean_execution_rows"),
        ),
        "risk_ready_rows": _first_int(
            status_guard.get("risk_ready_rows"),
            status_summary.get("risk_ready_rows"),
            watch_summary.get("risk_ready_rows"),
        ),
        "paper_ready_candidates": _first_int(
            status_guard.get("paper_ready_candidates"),
            status_summary.get("paper_ready_candidates"),
            watch_summary.get("paper_ready_candidates"),
            r16_summary.get("paper_ready_candidates"),
            r17_summary.get("paper_ready_candidates"),
            funnel.get("paper_ready_candidates"),
        ),
        "primary_gap_after_refresh": _first(
            status_guard.get("primary_gap_after_refresh"),
            status_summary.get("primary_gap_after_refresh"),
            watch_summary.get("primary_gap_after_refresh"),
            funnel.get("primary_gap_after_refresh"),
        ),
        "phase3bc_main_blocker": _first(
            status_summary.get("phase3bc_main_blocker"),
            watch_summary.get("phase3bc_main_blocker"),
            funnel.get("phase3bc_main_blocker"),
        ),
        "best_current_expected_value_cents": _first(
            status_summary.get("best_current_expected_value_cents"),
            watch_summary.get("best_current_expected_value_cents"),
            funnel.get("best_current_expected_value_cents"),
        ),
        "best_ev_gap_to_positive_cents": _first(
            status_summary.get("best_ev_gap_to_positive_cents"),
            watch_summary.get("best_ev_gap_to_positive_cents"),
            funnel.get("best_ev_gap_to_positive_cents"),
        ),
        "best_ev_candidate_ticker": _first(
            status_summary.get("best_ev_candidate_ticker"),
            watch_summary.get("best_ev_candidate_ticker"),
            funnel.get("best_ev_candidate_ticker"),
        ),
        "r17_watch_target": r17_summary.get("watch_target"),
        "sports_diagnostic_only_rows": _first_int(sports_summary.get("diagnostic_only_rows")),
        "sports_safe_repair_rows": _first_int(
            sports_summary.get("safe_exact_repair_rows"),
            sports_summary.get("phase3z_rows_safe_to_repair"),
        ),
        "sports_first_blocker": sports_summary.get("phase3ax_r6_gate"),
    }


def _phase_statuses(facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    snapshot_done = (
        facts["snapshot_stale_rows"] == 0
        and str(facts["snapshot_backlog_status"]).upper() in {"COMPLETE", "NONE"}
    )
    forecast_done = (
        facts["forecast_stale_rows"] == 0
        and str(facts["forecast_backlog_status"]).upper() in {"COMPLETE", "NONE"}
    )
    ranking_done = (
        facts["ranking_gap_after_repair"] == 0
        and facts["missing_or_stale_ranking_rows"] == 0
    )
    market_fill_done = snapshot_done and forecast_done and ranking_done
    phases = {
        "watcher": _phase(
            "Watcher running",
            "DONE" if facts["r5_running"] and not facts["r5_stale_report"] else "BLOCKED",
            "R5 is running and current."
            if facts["r5_running"] and not facts["r5_stale_report"]
            else "R5 is stopped, stale, or unknown.",
        ),
        "crypto_market_fill": _phase(
            "Fill current crypto markets",
            "DONE" if market_fill_done else "NEEDS_REFRESH",
            (
                "Current crypto snapshots, forecasts, and rankings are filled."
                if market_fill_done
                else "Refresh snapshots, forecasts, or rankings before paper ranking."
            ),
        ),
        "trade_ranking": _phase(
            "Rank candidate trades",
            "DONE" if ranking_done else "NEEDS_REPAIR",
            (
                "Ranking gap is zero."
                if ranking_done
                else "Ranking coverage still has missing or stale rows."
            ),
        ),
        "positive_ev_gate": _phase(
            "Wait for positive EV",
            "DONE" if facts["positive_ev_rows"] > 0 else "WAITING",
            (
                "At least one current row has positive EV."
                if facts["positive_ev_rows"] > 0
                else "No current positive-EV rows exist."
            ),
        ),
        "liquidity_and_risk": _phase(
            "Liquidity and risk gate",
            _liquidity_status(facts),
            _liquidity_reason(facts),
        ),
        "paper_trade_creation": _phase(
            "Create paper trades",
            "READY_FOR_OPERATOR_REVIEW" if facts["paper_ready_candidates"] > 0 else "BLOCKED",
            (
                "Paper-ready candidates exist; review before creating paper orders."
                if facts["paper_ready_candidates"] > 0
                else "No paper-ready candidates. Do not run accelerate-learning."
            ),
        ),
        "sports_market_fill": _phase(
            "Fill non-crypto sports markets",
            "READY" if facts["sports_safe_repair_rows"] > 0 else "DIAGNOSTIC_ONLY",
            (
                "Safe sports repair rows exist."
                if facts["sports_safe_repair_rows"] > 0
                else "Sports rows remain diagnostic-only; no safe repair rows."
            ),
        ),
    }
    return phases


def _remaining_gaps(
    facts: dict[str, Any],
    *,
    sports_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    if not facts["r5_running"] or facts["r5_stale_report"]:
        gaps.append(
            _gap(
                "WATCHER_NOT_CURRENT",
                "R5 watcher is stopped, stale, or unknown.",
                "Restart or refresh the guarded R5 watch when the runtime is healthy.",
            )
        )
    if facts["ranking_gap_after_repair"] > 0 or facts["missing_or_stale_ranking_rows"] > 0:
        gaps.append(
            _gap(
                "RANKING_GAP",
                "Current crypto ranking coverage is not complete.",
                "Run R5/R7 ranking repair before paper ranking.",
            )
        )
    if facts["snapshot_stale_rows"] > 0 or facts["forecast_stale_rows"] > 0:
        gaps.append(
            _gap(
                "FRESHNESS_GAP",
                "Snapshots or forecasts are stale.",
                "Run the bounded crypto freshness watch.",
            )
        )
    if facts["positive_ev_rows"] == 0:
        gaps.append(
            _gap(
                "EV_NOT_POSITIVE",
                (
                    "No current rows have positive expected value. "
                    f"Best candidate {facts['best_ev_candidate_ticker']} is "
                    f"{facts['best_current_expected_value_cents']}c EV, "
                    f"{facts['best_ev_gap_to_positive_cents']}c from positive."
                ),
                "Keep the watch running; do not lower thresholds.",
            )
        )
    r17_blocker = _r17_blocker(facts)
    if r17_blocker:
        gaps.append(
            _gap(
                r17_blocker,
                f"R17 target is {r17_blocker}; positive EV rows do not yet clear the executable book/risk gate.",
                "Keep the guarded R5 watch running and rerun R17 after fresh book updates.",
            )
        )
    if facts["paper_ready_candidates"] == 0:
        gaps.append(
            _gap(
                "NO_PAPER_READY_CANDIDATES",
                "No row clears EV, executable book, liquidity, sizing, and risk gates.",
                "Do not run paper-trade creation or accelerate-learning yet.",
            )
        )
    if facts["sports_diagnostic_only_rows"] > 0 and facts["sports_safe_repair_rows"] == 0:
        gaps.append(
            _gap(
                "SPORTS_DIAGNOSTIC_ONLY",
                (
                    f"{facts['sports_diagnostic_only_rows']} sports rows are diagnostic-only; "
                    "safe repair rows are zero."
                ),
                "Keep sports out of paper trading until exact evidence exists.",
            )
        )
    if not gaps:
        gaps.append(
            _gap(
                "READY_FOR_OPERATOR_REVIEW",
                "Paper-ready candidates exist.",
                "Review R16/R17 rows, then choose the paper-only creation path.",
            )
        )
    return gaps


def _next_commands(
    facts: dict[str, Any],
    *,
    phases: dict[str, dict[str, Any]],
) -> list[str]:
    commands = [
        "kalshi-bot paper-trading-gap-analysis --output-dir reports/paper_trading_gap",
        "kalshi-bot phase3bc-r17-crypto-liquidity-actionability --output-dir reports/phase3bc_r17",
    ]
    if phases["watcher"]["status"] != "DONE":
        commands.append(
            "kalshi-bot phase3bc-r5-unattended-start --output-dir reports/phase3bc_r5"
        )
    elif phases["crypto_market_fill"]["status"] != "DONE":
        commands.append(
            "kalshi-bot phase3bc-r5-crypto-freshness-watch --output-dir reports/phase3bc_r5"
        )
    elif facts["paper_ready_candidates"] > 0:
        commands.append(
            "kalshi-bot phase3bc-r16-crypto-paper-ready-edge-hunt "
            "--output-dir reports/phase3bc_r16 --run-refresh --max-preflight 5"
        )
    else:
        commands.append(
            "Keep the guarded R5 watch running; do not run accelerate-learning until paper_ready_candidates > 0."
        )
    return commands


def _do_not_run_yet(facts: dict[str, Any]) -> list[str]:
    blocked = [
        "Do not submit live/demo exchange orders.",
        "Do not lower EV, liquidity, spread, sizing, source, or risk thresholds.",
    ]
    if facts["paper_ready_candidates"] <= 0:
        blocked.extend(
            [
                "Do not run accelerate-learning.",
                "Do not create paper trades from current rows.",
            ]
        )
    if facts["sports_safe_repair_rows"] <= 0:
        blocked.append("Do not create paper trades from sports diagnostic-only rows.")
    return blocked


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Paper Trading Gap Analysis",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Safety: `{payload['paper_only_safety']}`",
        f"- Current blocker: `{summary['current_blocker']}`",
        f"- Market fill ready: `{summary['market_fill_ready']}`",
        f"- Trade ranking ready: `{summary['trade_ranking_ready']}`",
        f"- Paper trade ready: `{summary['paper_trade_ready']}`",
        "",
        "## Phase Status",
        "",
        "| Phase | Status | Reason |",
        "|---|---|---|",
    ]
    for phase in payload["phase_statuses"].values():
        lines.append(
            f"| {_cell(phase['name'])} | `{phase['status']}` | {_cell(phase['reason'])} |"
        )
    lines.extend(
        [
            "",
            "## Remaining Gaps",
            "",
            "| Gap | Detail | Next |",
            "|---|---|---|",
        ]
    )
    for gap in payload["remaining_gaps"]:
        lines.append(f"| `{gap['code']}` | {_cell(gap['detail'])} | {_cell(gap['next'])} |")
    lines.extend(
        [
            "",
            "## Do Not Run Yet",
            "",
        ]
    )
    for item in payload["do_not_run_yet"]:
        lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "## Next Commands",
            "",
            "```bash",
            *payload["next_commands"],
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _render_next_commands(payload: dict[str, Any]) -> str:
    lines = [
        "# Next Commands",
        "",
        "```bash",
        *payload["next_commands"],
        "```",
        "",
        "Blocked commands:",
        "",
    ]
    for item in payload["do_not_run_yet"]:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def _liquidity_status(facts: dict[str, Any]) -> str:
    if facts["paper_ready_candidates"] > 0:
        return "READY_FOR_OPERATOR_REVIEW"
    if facts["positive_ev_rows"] <= 0:
        return "BLOCKED_BY_EV"
    target = str(facts.get("r17_watch_target") or "")
    if target == "RUN_PAPER_ONLY_RISK_PREFLIGHT":
        return "READY_FOR_PREFLIGHT"
    if target:
        return target
    return "NEEDS_R17_REVIEW"


def _liquidity_reason(facts: dict[str, Any]) -> str:
    if facts["paper_ready_candidates"] > 0:
        return "Candidate passed paper-ready gates."
    if facts["positive_ev_rows"] <= 0:
        return "Liquidity and risk gates wait until EV is positive."
    if facts.get("r17_watch_target"):
        return f"R17 target is {facts['r17_watch_target']}."
    return "Run R17 liquidity actionability report."


def _current_blocker(facts: dict[str, Any]) -> str:
    if facts["paper_ready_candidates"] > 0:
        return "PAPER_READY_REVIEW"
    if facts["positive_ev_rows"] <= 0:
        return "EV_NOT_POSITIVE"
    r17_blocker = _r17_blocker(facts)
    if r17_blocker:
        return r17_blocker
    if facts["positive_ev_preflight_candidates"] <= 0:
        return "PREFLIGHT_NOT_READY"
    return "OPERATOR_REVIEW_REQUIRED"


def _r17_blocker(facts: dict[str, Any]) -> str:
    target = str(facts.get("r17_watch_target") or "")
    if target and target not in {"RUN_PAPER_ONLY_RISK_PREFLIGHT", "PAPER_READY_REVIEW"}:
        return target
    return ""


def _phase(name: str, status: str, reason: str) -> dict[str, Any]:
    return {"name": name, "status": status, "reason": reason}


def _gap(code: str, detail: str, next_step: str) -> dict[str, str]:
    return {"code": code, "detail": detail, "next": next_step}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _first_int(*values: Any) -> int:
    for value in values:
        parsed = _int_value(value)
        if parsed is not None:
            return parsed
    return 0


def _int_value(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")
