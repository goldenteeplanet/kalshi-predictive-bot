from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PHASE3AN_CRYPTO_SOURCE_QUALITY_VERSION = "phase3an_crypto_source_quality_v1"
PAPER_ONLY_SAFETY = "PAPER_ONLY_NO_EXCHANGE_WRITES"
DEFAULT_SYMBOLS = ("BTC", "ETH", "SOL", "XRP", "DOGE")
DEFAULT_SERIES_TICKERS = ("KXBTC", "KXETH", "KXSOLE", "KXXRP", "KXDOGE")

_RATE_LIMIT_LOG_PATTERN = re.compile(
    r"(http\s*429|status\s*code\s*429|rate[-_ ]limit|rate_limited|retry_exhausted)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Phase3ANCryptoSourceQualityArtifacts:
    output_dir: Path
    json_path: Path
    markdown_path: Path


def write_phase3an_crypto_source_quality_report(
    *,
    output_dir: Path = Path("reports/phase3an"),
    reports_dir: Path = Path("reports"),
    symbols: str | None = None,
) -> Phase3ANCryptoSourceQualityArtifacts:
    payload = build_phase3an_crypto_source_quality(
        output_dir=output_dir,
        reports_dir=reports_dir,
        symbols=symbols,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "crypto_watch_source_quality.json"
    markdown_path = output_dir / "crypto_watch_source_quality.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(
        render_phase3an_crypto_source_quality_markdown(payload),
        encoding="utf-8",
    )
    return Phase3ANCryptoSourceQualityArtifacts(output_dir, json_path, markdown_path)


def build_phase3an_crypto_source_quality(
    *,
    output_dir: Path = Path("reports/phase3an"),
    reports_dir: Path = Path("reports"),
    symbols: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    status_path = reports_dir / "phase3bc_r5" / "phase3bc_r5_status.json"
    watch_path = reports_dir / "phase3bc_r5" / "phase3bc_r5_crypto_freshness_watch.json"
    stdout_path = reports_dir / "phase3bc_r5" / "phase3bc_r5_unattended_stdout.log"
    stderr_path = reports_dir / "phase3bc_r5" / "phase3bc_r5_unattended_stderr.log"
    paper_gap_path = reports_dir / "paper_trading_gap" / "paper_trading_gap_analysis.json"

    status = _load_json(status_path)
    watch = _load_json(watch_path)
    paper_gap = _load_json(paper_gap_path)

    guard = _dict(status.get("guard"))
    status_summary = _dict(status.get("latest_summary"))
    watch_summary = _dict(watch.get("summary"))
    r3_summary = _dict(watch.get("phase3bc_r3_summary"))
    options = _dict(watch.get("options"))
    paper_gap_summary = _dict(paper_gap.get("summary"))

    requested_symbols = _requested_symbols(symbols or options.get("symbols"))
    requested_series = _requested_series(
        r3_summary.get("crypto_series_tickers")
        or options.get("crypto_series_tickers")
    )
    snapshot_counts = _symbol_counts(
        r3_summary.get("per_symbol_snapshot_counts"),
        r3_summary.get("crypto_series_refreshes"),
        "per_symbol_snapshot_counts",
    )
    liquidity_counts = _symbol_counts(
        r3_summary.get("per_symbol_liquidity_first_counts"),
        r3_summary.get("crypto_series_refreshes"),
        "per_symbol_liquidity_first_counts",
    )
    symbol_rows = [
        _symbol_coverage_row(
            symbol,
            snapshot_counts=snapshot_counts,
            liquidity_counts=liquidity_counts,
        )
        for symbol in requested_symbols
    ]
    series_rows = _series_refresh_rows(
        r3_summary.get("crypto_series_refreshes"),
        requested_symbols=requested_symbols,
        requested_series=requested_series,
    )
    missing_symbols = [row["symbol"] for row in symbol_rows if row["snapshot_count"] <= 0]
    zero_liquidity_symbols = [
        row["symbol"] for row in symbol_rows if row["liquidity_first_count"] <= 0
    ]
    zero_market_symbols = _zero_market_symbols(series_rows)

    stdout_text = _read_tail(stdout_path)
    stderr_text = _read_tail(stderr_path)
    rate_limit = _rate_limit_summary(
        watch=watch,
        stdout_text=stdout_text,
        stderr_text=stderr_text,
    )

    paper_ready = _first_int(
        guard.get("paper_ready_candidates"),
        status_summary.get("paper_ready_candidates"),
        watch_summary.get("paper_ready_candidates"),
        r3_summary.get("phase3bc_paper_ready_candidates"),
        paper_gap_summary.get("paper_ready_candidates"),
    )
    positive_ev_rows = _first_int(
        guard.get("positive_ev_rows"),
        status_summary.get("positive_ev_rows"),
        watch_summary.get("positive_ev_rows"),
        paper_gap_summary.get("positive_ev_rows"),
    )
    snapshot_stale_rows = _first_int(
        guard.get("snapshot_stale_rows"),
        status_summary.get("snapshot_stale_rows"),
        watch_summary.get("snapshot_stale_rows"),
    )
    forecast_stale_rows = _first_int(
        guard.get("forecast_stale_rows"),
        status_summary.get("forecast_stale_rows"),
        watch_summary.get("forecast_stale_rows"),
    )
    ranking_gap = _first_int(
        guard.get("true_ranking_gap_after_repair"),
        status_summary.get("true_ranking_gap_after_repair"),
        watch_summary.get("true_ranking_gap_after_repair"),
        status_summary.get("ranking_coverage_gap_after_repair"),
        watch_summary.get("ranking_coverage_gap_after_repair"),
    )
    missing_or_stale_ranking = _first_int(
        guard.get("missing_or_stale_ranking_rows"),
        status_summary.get("missing_or_stale_ranking_rows"),
        watch_summary.get("missing_or_stale_ranking_rows"),
    )
    market_fill_ready = snapshot_stale_rows == 0 and forecast_stale_rows == 0
    trade_ranking_ready = ranking_gap == 0 and missing_or_stale_ranking == 0

    classification = _source_quality_classification(
        missing_symbols=missing_symbols,
        zero_market_symbols=zero_market_symbols,
        rate_limit_pressure=rate_limit["pressure_detected"],
        paper_ready=paper_ready,
        positive_ev_rows=positive_ev_rows,
        market_fill_ready=market_fill_ready,
        trade_ranking_ready=trade_ranking_ready,
    )
    summary = {
        "classification": classification,
        "requested_symbols": requested_symbols,
        "missing_symbols": missing_symbols,
        "zero_liquidity_symbols": zero_liquidity_symbols,
        "zero_market_symbols": zero_market_symbols,
        "symbols_with_snapshots": len([row for row in symbol_rows if row["snapshot_count"] > 0]),
        "rate_limit_pressure": rate_limit["pressure_detected"],
        "paper_ready_candidates": paper_ready,
        "positive_ev_rows": positive_ev_rows,
        "market_fill_ready": market_fill_ready,
        "trade_ranking_ready": trade_ranking_ready,
        "watch_state": _first(
            guard.get("watch_state"),
            status_summary.get("watch_state"),
            watch_summary.get("watch_state"),
        ),
        "current_blocker": _first(
            guard.get("primary_gap_after_refresh"),
            status_summary.get("primary_gap_after_refresh"),
            watch_summary.get("primary_gap_after_refresh"),
            paper_gap_summary.get("current_blocker"),
        ),
        "best_ev_candidate_ticker": _first(
            status_summary.get("best_ev_candidate_ticker"),
            watch_summary.get("best_ev_candidate_ticker"),
            paper_gap_summary.get("best_ev_candidate_ticker"),
        ),
        "best_current_expected_value_cents": _first(
            status_summary.get("best_current_expected_value_cents"),
            watch_summary.get("best_current_expected_value_cents"),
            paper_gap_summary.get("best_current_expected_value_cents"),
        ),
        "slowest_stage": _slowest_stage(status=status, watch=watch),
    }
    return {
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "phase": "3AN",
        "phase_version": PHASE3AN_CRYPTO_SOURCE_QUALITY_VERSION,
        "mode": "PAPER_READ_ONLY_CRYPTO_SOURCE_QUALITY",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "summary": summary,
        "symbol_coverage": symbol_rows,
        "series_refreshes": series_rows,
        "rate_limit": rate_limit,
        "stage_evidence": {
            "latest_slowest_stage": status.get("latest_slowest_stage"),
            "stage_duration_seconds": _first(
                status.get("latest_stage_duration_seconds"),
                watch.get("stage_duration_seconds"),
                watch.get("stage_durations_seconds"),
            ),
        },
        "artifact_sources": {
            "status_json": _artifact_source(status_path),
            "watch_json": _artifact_source(watch_path),
            "stdout_log": _artifact_source(stdout_path),
            "stderr_log": _artifact_source(stderr_path),
            "paper_gap_json": _artifact_source(paper_gap_path),
        },
        "exact_next_action": _source_quality_next_action(classification, missing_symbols),
        "next_commands": _source_quality_next_commands(classification),
        "do_not_run": _source_quality_do_not_run(paper_ready=paper_ready),
    }


def render_phase3an_crypto_source_quality_markdown(payload: dict[str, Any]) -> str:
    summary = _dict(payload.get("summary"))
    lines = [
        "# Phase 3AN Crypto Watch Source Quality",
        "",
        f"- Generated at: `{payload.get('generated_at')}`",
        f"- Classification: `{summary.get('classification')}`",
        f"- Watch state: `{summary.get('watch_state')}`",
        f"- Current blocker: `{summary.get('current_blocker')}`",
        f"- Paper-ready candidates: `{summary.get('paper_ready_candidates')}`",
        f"- Positive EV rows: `{summary.get('positive_ev_rows')}`",
        f"- Rate-limit pressure: `{summary.get('rate_limit_pressure')}`",
        f"- Missing symbols: `{', '.join(summary.get('missing_symbols') or []) or 'none'}`",
        (
            f"- Zero-market symbols: "
            f"`{', '.join(summary.get('zero_market_symbols') or []) or 'none'}`"
        ),
        f"- Slowest stage: `{summary.get('slowest_stage')}`",
        "",
        "## Symbol Coverage",
        "",
        "| Symbol | Snapshots | Liquidity-first | Status |",
        "|---|---:|---:|---|",
    ]
    for row in payload.get("symbol_coverage") or []:
        lines.append(
            f"| {row['symbol']} | {row['snapshot_count']} | "
            f"{row['liquidity_first_count']} | `{row['status']}` |"
        )
    lines.extend(
        [
            "",
            "## Series Refreshes",
            "",
            "| Symbol | Series | Markets | Snapshots | Status |",
            "|---|---|---:|---:|---|",
        ]
    )
    for row in payload.get("series_refreshes") or []:
        lines.append(
            f"| {row['symbol']} | {row['series_ticker']} | {row['markets_seen']} | "
            f"{row['snapshots_inserted']} | `{row['status']}` |"
        )
    lines.extend(["", "## Rate Limit Evidence", ""])
    rate_limit = _dict(payload.get("rate_limit"))
    lines.append(f"- Pressure detected: `{rate_limit.get('pressure_detected')}`")
    lines.append(f"- Structured events: `{len(rate_limit.get('structured_evidence') or [])}`")
    lines.append(f"- Log matches: `{len(rate_limit.get('log_evidence') or [])}`")
    lines.extend(["", "## Next", "", str(payload.get("exact_next_action") or "")])
    lines.extend(["", "## Do Not Run", ""])
    for item in payload.get("do_not_run") or []:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def source_quality_classification_for_phase3an(payload: dict[str, Any]) -> str:
    return str(
        _dict(payload.get("summary")).get("classification")
        or "UNKNOWN_REQUIRES_INVESTIGATION"
    )


def _source_quality_classification(
    *,
    missing_symbols: list[str],
    zero_market_symbols: list[str],
    rate_limit_pressure: bool,
    paper_ready: int,
    positive_ev_rows: int,
    market_fill_ready: bool,
    trade_ranking_ready: bool,
) -> str:
    if rate_limit_pressure:
        return "API_RATE_LIMIT_PRESSURE"
    if zero_market_symbols:
        return "SOURCE_SERIES_EMPTY"
    if missing_symbols:
        return "SOURCE_COVERAGE_GAP"
    if paper_ready > 0:
        return "PAPER_READY_REVIEW"
    if positive_ev_rows > 0:
        return "WAIT_FOR_EXECUTABLE_BOOK"
    if market_fill_ready and trade_ranking_ready and positive_ev_rows == 0:
        return "WAIT_FOR_MARKET_EV"
    return "UNKNOWN_REQUIRES_INVESTIGATION"


def _source_quality_next_action(classification: str, missing_symbols: list[str]) -> str:
    if classification == "API_RATE_LIMIT_PRESSURE":
        return "Keep the watch paper-only; reduce source pressure before any acceleration."
    if classification == "SOURCE_COVERAGE_GAP":
        symbols = ",".join(missing_symbols)
        return (
            f"Repair source coverage for {symbols}; investigate zero markets/snapshots before "
            "treating the watch as EV-only."
        )
    if classification == "SOURCE_SERIES_EMPTY":
        symbols = ",".join(missing_symbols)
        return (
            f"Verify Kalshi source series for {symbols}; current collection completed with "
            "zero open markets, so rerunning the same watch will not fill coverage."
        )
    if classification == "WAIT_FOR_MARKET_EV":
        return (
            "Market fill and ranking are current; wait for positive EV without "
            "lowering thresholds."
        )
    if classification == "WAIT_FOR_EXECUTABLE_BOOK":
        return "Positive EV exists, but executable book/risk gates are not paper-ready yet."
    if classification == "PAPER_READY_REVIEW":
        return "Paper-ready rows exist; operator review is required before any paper creation."
    return "Run bounded read-only diagnostics; do not open writer-capable branches."


def _source_quality_next_commands(classification: str) -> list[str]:
    commands = [
        (
            "kalshi-bot phase3an-crypto-watch-doctor "
            "--output-dir reports/phase3an --reports-dir reports"
        ),
        "kalshi-bot paper-trading-gap-analysis --output-dir reports/paper_trading_gap",
    ]
    if classification == "SOURCE_COVERAGE_GAP":
        commands.append(
            "Inspect reports/phase3an/crypto_watch_source_quality.json for zero "
            "per-symbol coverage."
        )
    elif classification == "SOURCE_SERIES_EMPTY":
        commands.append(
            "Verify whether the zero-market source series is deprecated, paused, or replaced."
        )
    elif classification == "API_RATE_LIMIT_PRESSURE":
        commands.append("Inspect R5 unattended logs for HTTP 429/rate-limit events.")
    elif classification == "PAPER_READY_REVIEW":
        commands.append(
            "Review paper-ready rows manually before any paper-only creation command."
        )
    else:
        commands.append("Keep the guarded R5 watch running; do not run accelerate-learning.")
    return commands


def _source_quality_do_not_run(*, paper_ready: int) -> list[str]:
    items = [
        "Do not submit live/demo exchange orders.",
        "Do not lower EV, liquidity, source, spread, sizing, or risk thresholds.",
    ]
    if paper_ready <= 0:
        items.extend(
            [
                "Do not run accelerate-learning.",
                "Do not create paper trades from current rows.",
            ]
        )
    return items


def _symbol_coverage_row(
    symbol: str,
    *,
    snapshot_counts: dict[str, int],
    liquidity_counts: dict[str, int],
) -> dict[str, Any]:
    snapshot_count = snapshot_counts.get(symbol, 0)
    liquidity_count = liquidity_counts.get(symbol, 0)
    if snapshot_count <= 0:
        status = "MISSING_SOURCE_COVERAGE"
    elif liquidity_count <= 0:
        status = "NO_LIQUIDITY_FIRST_COVERAGE"
    else:
        status = "COVERED"
    return {
        "symbol": symbol,
        "snapshot_count": snapshot_count,
        "liquidity_first_count": liquidity_count,
        "status": status,
    }


def _series_refresh_rows(
    series_refreshes: Any,
    *,
    requested_symbols: list[str],
    requested_series: list[str],
) -> list[dict[str, Any]]:
    if not isinstance(series_refreshes, list):
        return []
    rows: list[dict[str, Any]] = []
    for index, refresh in enumerate(series_refreshes):
        if not isinstance(refresh, dict):
            continue
        symbol = str(
            refresh.get("symbol")
            or _symbol_from_counts(refresh)
            or (requested_symbols[index] if index < len(requested_symbols) else "")
            or ""
        ).upper()
        series_ticker = str(
            refresh.get("series_ticker")
            or (requested_series[index] if index < len(requested_series) else "")
            or _series_for_symbol(symbol)
            or ""
        ).upper()
        markets_seen = _int_value(refresh.get("markets_seen"))
        snapshots_inserted = _int_value(refresh.get("snapshots_inserted"))
        if markets_seen <= 0 and snapshots_inserted <= 0:
            status = "ZERO_MARKETS_OR_SNAPSHOTS"
        elif snapshots_inserted <= 0:
            status = "ZERO_SNAPSHOTS"
        else:
            status = "COVERED"
        rows.append(
            {
                "symbol": symbol,
                "series_ticker": series_ticker,
                "markets_seen": markets_seen,
                "snapshots_inserted": snapshots_inserted,
                "collection_status": refresh.get("collection_status"),
                "data_complete": refresh.get("data_complete"),
                "rate_limit_status": refresh.get("rate_limit_status"),
                "stopped_reason": refresh.get("stopped_reason"),
                "status": status,
            }
        )
    return rows


def _zero_market_symbols(series_rows: list[dict[str, Any]]) -> list[str]:
    symbols: list[str] = []
    for row in series_rows:
        if (
            row.get("status") == "ZERO_MARKETS_OR_SNAPSHOTS"
            and row.get("collection_status") == "COMPLETE"
            and row.get("rate_limit_status") == "COMPLETE"
            and row.get("data_complete") is True
        ):
            symbol = str(row.get("symbol") or "")
            if symbol:
                symbols.append(symbol)
    return symbols


def _symbol_counts(
    summary_counts: Any,
    series_refreshes: Any,
    key: str,
) -> dict[str, int]:
    counts = _count_map(summary_counts)
    if counts:
        return counts
    if not isinstance(series_refreshes, list):
        return {}
    merged: dict[str, int] = {}
    for refresh in series_refreshes:
        if not isinstance(refresh, dict):
            continue
        for symbol, value in _count_map(refresh.get(key)).items():
            merged[symbol] = merged.get(symbol, 0) + value
    return merged


def _symbol_from_counts(refresh: dict[str, Any]) -> str | None:
    for key in ("per_symbol_snapshot_counts", "per_symbol_liquidity_first_counts"):
        counts = _count_map(refresh.get(key))
        if counts:
            return next(iter(counts))
    return None


def _requested_series(raw: Any) -> list[str]:
    if raw is None or raw == "":
        return list(DEFAULT_SERIES_TICKERS)
    if isinstance(raw, str):
        series = [part.strip().upper() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        series = [str(part).strip().upper() for part in raw]
    else:
        series = []
    return [item for item in series if item] or list(DEFAULT_SERIES_TICKERS)


def _series_for_symbol(symbol: str) -> str | None:
    try:
        return DEFAULT_SERIES_TICKERS[DEFAULT_SYMBOLS.index(symbol)]
    except ValueError:
        return None


def _rate_limit_summary(
    *,
    watch: dict[str, Any],
    stdout_text: str,
    stderr_text: str,
) -> dict[str, Any]:
    structured = _structured_rate_limit_evidence(watch)
    log_evidence = _log_rate_limit_evidence(stdout_text, "stdout") + _log_rate_limit_evidence(
        stderr_text,
        "stderr",
    )
    return {
        "pressure_detected": bool(structured or log_evidence),
        "structured_evidence": structured[:20],
        "log_evidence": log_evidence[:20],
    }


def _structured_rate_limit_evidence(value: Any, path: str = "root") -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    if isinstance(value, dict):
        details = _dict(value.get("rate_limit_details"))
        status = _first(value.get("rate_limit_status"), details.get("status"))
        rate_limited = _truthy(_first(value.get("rate_limited"), details.get("rate_limited")))
        rate_limited_count = _first_int(
            value.get("rate_limited_count"),
            details.get("rate_limited_count"),
        )
        retry_exhausted_count = _first_int(
            value.get("retry_exhausted_count"),
            details.get("retry_exhausted_count"),
        )
        if rate_limited or rate_limited_count > 0 or retry_exhausted_count > 0:
            evidence.append(
                {
                    "path": path,
                    "status": status,
                    "rate_limited": rate_limited,
                    "rate_limited_count": rate_limited_count,
                    "retry_exhausted_count": retry_exhausted_count,
                }
            )
        for child_key, child_value in value.items():
            evidence.extend(
                _structured_rate_limit_evidence(child_value, f"{path}.{child_key}")
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            evidence.extend(_structured_rate_limit_evidence(item, f"{path}[{index}]"))
    return evidence


def _log_rate_limit_evidence(text: str, source: str) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if _RATE_LIMIT_LOG_PATTERN.search(line):
            evidence.append(
                {
                    "source": source,
                    "line_number": line_number,
                    "line": line[:240],
                }
            )
    return evidence


def _requested_symbols(raw: Any) -> list[str]:
    if raw is None or raw == "":
        return list(DEFAULT_SYMBOLS)
    if isinstance(raw, str):
        symbols = [part.strip().upper() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        symbols = [str(part).strip().upper() for part in raw]
    else:
        symbols = []
    return [symbol for symbol in symbols if symbol] or list(DEFAULT_SYMBOLS)


def _slowest_stage(*, status: dict[str, Any], watch: dict[str, Any]) -> str | None:
    latest = _dict(status.get("latest_slowest_stage"))
    if latest.get("stage"):
        return str(latest.get("stage"))
    summary = _dict(watch.get("summary"))
    if summary.get("slowest_stage"):
        return str(summary.get("slowest_stage"))
    return None


def _artifact_source(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "exists": False, "size_bytes": 0, "modified_at": None}
    return {
        "path": str(path),
        "exists": True,
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
    }


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_tail(path: Path, max_chars: int = 40_000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:]


def _count_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key).upper(): _int_value(count) for key, count in value.items()}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _first_int(*values: Any) -> int:
    for value in values:
        if value is None or value == "":
            continue
        return _int_value(value)
    return 0


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}
