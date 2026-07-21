from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.crypto.features import build_crypto_features
from kalshi_predictor.crypto.ingestion import ingest_manual_crypto_json
from kalshi_predictor.crypto.linker import link_crypto_markets
from kalshi_predictor.crypto.providers import CryptoFetchResult, CryptoQuote, fetch_crypto_quotes
from kalshi_predictor.crypto.repository import normalize_symbol
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3au import LongJobHeartbeat, deadline_reached, stop_after_deadline
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R43_VERSION = "phase3bb_r43_single_writer_coordinator_v1"

FetchCryptoQuotesFn = Callable[..., CryptoFetchResult]
WriterMonitorFn = Callable[[], dict[str, Any]]


@dataclass(frozen=True)
class Phase3BBR43Artifacts:
    output_dir: Path
    staging_dir: Path
    json_path: Path
    markdown_path: Path


def run_phase3bb_r43_single_writer_coordinator(
    *,
    session_factory: Callable[[], Session],
    output_dir: Path = Path("reports/phase3bb_r43"),
    symbols: list[str] | None = None,
    crypto_sources: list[str] | None = None,
    stage_fetches: bool = True,
    drain_staged: bool = False,
    build_features_after_drain: bool = True,
    link_crypto_after_drain: bool = False,
    crypto_link_limit: int | None = None,
    staging_dir: Path | None = None,
    max_workers: int = 4,
    stop_after_minutes: int = 0,
    guard_active_writer: bool = True,
    settings: Settings | None = None,
    fetch_crypto_quotes_fn: FetchCryptoQuotesFn = fetch_crypto_quotes,
    writer_monitor_fn: WriterMonitorFn | None = None,
) -> Phase3BBR43Artifacts:
    """Stage parallel fetches and optionally drain them through one DB writer.

    Default behavior is stage-only. The caller must opt into the writer drain
    with ``drain_staged=True``; the drain uses one session and checks the active
    writer monitor before opening that session.
    """

    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = _run_id()
    resolved_staging_dir = staging_dir or (output_dir / "staging" / run_id)
    resolved_symbols = _unique_symbols(symbols or [])
    resolved_sources = _unique_sources(crypto_sources or ["coinbase"])
    deadline = stop_after_deadline(stop_after_minutes)
    heartbeat = LongJobHeartbeat(
        job_name="phase3bb-r43-single-writer-coordinator",
        output_dir=output_dir,
        checkpoint_every=1,
    )
    heartbeat.emit(
        stage="STARTING",
        total=len(resolved_symbols) * len(resolved_sources),
        message="Preparing parallel fetch staging and single-writer drain plan.",
        force_checkpoint=True,
        extra={
            "phase_version": PHASE3BB_R43_VERSION,
            "stage_fetches": stage_fetches,
            "drain_staged": drain_staged,
            "staging_dir": str(resolved_staging_dir),
        },
    )

    stage_result: dict[str, Any]
    if stage_fetches:
        heartbeat.emit(stage="STAGE_FETCHES", message="Starting parallel fetch staging.")
        stage_result = stage_crypto_quote_fetches(
            symbols=resolved_symbols,
            sources=resolved_sources,
            staging_dir=resolved_staging_dir,
            max_workers=max_workers,
            fetch_crypto_quotes_fn=fetch_crypto_quotes_fn,
        )
    else:
        stage_result = {
            "status": "SKIPPED",
            "reason": "stage_fetches disabled; drain will use existing staging files.",
            "staged_files": [],
            "jobs": [],
            "errors": [],
        }

    drain_result: dict[str, Any]
    writer_monitor_payload: dict[str, Any] | None = None
    if drain_staged:
        if deadline_reached(deadline):
            drain_result = {
                "status": "SKIPPED_DEADLINE_REACHED",
                "prices_inserted": 0,
                "features_inserted": 0,
                "links_created": 0,
                "errors": ["Deadline reached before writer drain."],
            }
        else:
            writer_monitor_payload = _writer_monitor_snapshot(
                settings=settings,
                writer_monitor_fn=writer_monitor_fn,
            )
            if guard_active_writer and not bool(
                writer_monitor_payload.get("safe_to_start_write", True)
            ):
                drain_result = {
                    "status": "BLOCKED_ACTIVE_WRITER",
                    "prices_inserted": 0,
                    "features_inserted": 0,
                    "links_created": 0,
                    "errors": [
                        "Active writer detected; coordinator refused to start a second "
                        "SQLite writer."
                    ],
                    "writer_monitor": writer_monitor_payload,
                }
            else:
                heartbeat.emit(
                    stage="WRITER_DRAIN",
                    message="Draining staged files through one DB writer session.",
                    force_checkpoint=True,
                )
                with session_factory() as session:
                    drain_result = drain_staged_crypto_quotes(
                        session,
                        staging_dir=resolved_staging_dir,
                        build_features_after_drain=build_features_after_drain,
                        link_crypto_after_drain=link_crypto_after_drain,
                        crypto_link_limit=crypto_link_limit,
                        should_stop=lambda: deadline_reached(deadline),
                    )
                    session.commit()
    else:
        drain_result = {
            "status": "SKIPPED_STAGE_ONLY",
            "prices_inserted": 0,
            "features_inserted": 0,
            "links_created": 0,
            "errors": [],
        }

    payload = {
        "generated_at": utc_now().isoformat(),
        "phase": "3BB-R43",
        "phase_title": "Cloud Single-Writer Coordinator / Parallel Fetch Staging",
        "phase_version": PHASE3BB_R43_VERSION,
        "mode": "PAPER_ONLY_COORDINATOR",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "stage_fetches": stage_fetches,
        "parallel_fetch_enabled": stage_fetches,
        "single_writer_drain_enabled": drain_staged,
        "guard_active_writer": guard_active_writer,
        "symbols": resolved_symbols,
        "crypto_sources": resolved_sources,
        "max_workers": max_workers,
        "staging_dir": str(resolved_staging_dir),
        "stage_result": stage_result,
        "drain_result": drain_result,
        "writer_monitor": writer_monitor_payload,
        "recommended_next_action": _recommended_next_action(
            stage_result=stage_result,
            drain_result=drain_result,
            drain_staged=drain_staged,
        ),
        "safety": {
            "submits_exchange_orders": False,
            "creates_paper_trades": False,
            "enables_execution": False,
            "parallel_fetchers_write_db": False,
            "writer_drain_requires_explicit_flag": True,
            "refuses_second_writer": guard_active_writer,
        },
    }
    json_path = output_dir / "phase3bb_r43_single_writer_coordinator.json"
    markdown_path = output_dir / "phase3bb_r43_single_writer_coordinator.md"
    _write_json(json_path, payload)
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    heartbeat.emit(
        stage="COMPLETE",
        processed=len(stage_result.get("jobs") or []),
        total=len(stage_result.get("jobs") or []),
        message="Coordinator report complete.",
        force_checkpoint=True,
        extra={
            "json_path": str(json_path),
            "markdown_path": str(markdown_path),
            "drain_status": drain_result.get("status"),
        },
    )
    return Phase3BBR43Artifacts(output_dir, resolved_staging_dir, json_path, markdown_path)


def stage_crypto_quote_fetches(
    *,
    symbols: list[str],
    sources: list[str],
    staging_dir: Path,
    max_workers: int = 4,
    max_attempts: int = 3,
    retry_delay_seconds: float = 1.0,
    fetch_crypto_quotes_fn: FetchCryptoQuotesFn = fetch_crypto_quotes,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    staging_dir.mkdir(parents=True, exist_ok=True)
    jobs = [
        {"category": "crypto_quotes", "source": source, "symbol": symbol}
        for source in _unique_sources(sources)
        for symbol in _unique_symbols(symbols)
    ]
    if not jobs:
        return {
            "status": "NO_JOBS",
            "jobs": [],
            "staged_files": [],
            "errors": ["No crypto symbols or sources were provided."],
        }

    staged_files: list[str] = []
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    worker_count = max(1, min(max_workers, len(jobs)))
    resolved_attempts = max(1, max_attempts)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_job = {
            executor.submit(
                _fetch_crypto_job_with_retry,
                job,
                fetch_crypto_quotes_fn=fetch_crypto_quotes_fn,
                max_attempts=resolved_attempts,
                retry_delay_seconds=max(0.0, retry_delay_seconds),
                sleep_fn=sleep_fn,
            ): job
            for job in jobs
        }
        for future in as_completed(future_to_job):
            job = future_to_job[future]
            try:
                payload = future.result()
            except Exception as exc:
                payload = _failed_fetch_payload(job, str(exc))
            path = staging_dir / _stage_filename(job)
            _write_json(path, payload)
            staged_files.append(str(path))
            results.append(payload)
            errors.extend(str(error) for error in payload.get("errors") or [])

    return {
        "status": "COMPLETE_WITH_ERRORS" if errors else "COMPLETE",
        "jobs": sorted(results, key=lambda item: str(item.get("job_name") or "")),
        "staged_files": sorted(staged_files),
        "errors": errors,
        "parallel_workers": worker_count,
        "max_attempts": resolved_attempts,
    }


def _fetch_crypto_job_with_retry(
    job: dict[str, str],
    *,
    fetch_crypto_quotes_fn: FetchCryptoQuotesFn,
    max_attempts: int,
    retry_delay_seconds: float,
    sleep_fn: Callable[[float], None],
) -> dict[str, Any]:
    payload: dict[str, Any] | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = fetch_crypto_quotes_fn(
                [job["symbol"]],
                source=job["source"],
            )
            payload = _crypto_fetch_payload(job, result)
        except Exception as exc:
            payload = _failed_fetch_payload(job, str(exc))
        payload["attempts"] = attempt
        if payload.get("quotes"):
            return payload
        if attempt < max_attempts and retry_delay_seconds > 0:
            sleep_fn(retry_delay_seconds * attempt)
    return payload or _failed_fetch_payload(job, "Fetch failed without a result.")


def drain_staged_crypto_quotes(
    session: Session,
    *,
    staging_dir: Path,
    build_features_after_drain: bool = True,
    link_crypto_after_drain: bool = False,
    crypto_link_limit: int | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    files = _staged_crypto_quote_files(staging_dir)
    inserted = 0
    errors: list[str] = []
    symbols: set[str] = set()
    drained_files: list[str] = []
    for path in files:
        if should_stop is not None and should_stop():
            errors.append("Stopped before all staged files were drained.")
            break
        payload = _load_json(path)
        if str(payload.get("category") or "") != "crypto_quotes":
            continue
        quotes = [item for item in payload.get("quotes") or [] if isinstance(item, dict)]
        summary = ingest_manual_crypto_json(
            session,
            {"source": payload.get("source") or "staged", "prices": quotes},
            source=str(payload.get("source") or "staged"),
        )
        inserted += summary.prices_inserted
        errors.extend(summary.errors)
        symbols.update(
            normalize_symbol(str(item.get("symbol"))) for item in quotes if item.get("symbol")
        )
        drained_files.append(str(path))

    feature_summary = None
    if build_features_after_drain and symbols and (should_stop is None or not should_stop()):
        feature_summary = build_crypto_features(session, symbols=sorted(symbols))

    link_summary = None
    if link_crypto_after_drain and (should_stop is None or not should_stop()):
        link_summary = link_crypto_markets(
            session,
            limit=crypto_link_limit if crypto_link_limit and crypto_link_limit > 0 else None,
            progress_every=0,
            should_stop=should_stop,
        )

    return {
        "status": "COMPLETE" if not errors else "COMPLETE_WITH_ERRORS",
        "files_seen": len(files),
        "files_drained": len(drained_files),
        "drained_files": drained_files,
        "symbols": sorted(symbols),
        "prices_inserted": inserted,
        "features_inserted": feature_summary.features_inserted if feature_summary else 0,
        "feature_symbols_processed": feature_summary.symbols_processed if feature_summary else 0,
        "links_created": link_summary.links_created if link_summary else 0,
        "link_markets_processed": link_summary.markets_processed if link_summary else 0,
        "link_stopped_early": link_summary.stopped_early if link_summary else False,
        "errors": errors,
    }


def _writer_monitor_snapshot(
    *,
    settings: Settings | None,
    writer_monitor_fn: WriterMonitorFn | None,
) -> dict[str, Any]:
    if writer_monitor_fn is not None:
        return writer_monitor_fn()
    return db_writer_monitor(settings=settings)


def _crypto_fetch_payload(
    job: dict[str, str],
    result: CryptoFetchResult,
) -> dict[str, Any]:
    quotes = [_quote_payload(quote) for quote in result.quotes]
    status = "COMPLETE"
    if result.errors and quotes:
        status = "COMPLETE_WITH_ERRORS"
    elif result.errors:
        status = "FAILED"
    return {
        "generated_at": utc_now().isoformat(),
        "category": "crypto_quotes",
        "job_name": _job_name(job),
        "source": result.source,
        "symbol": job["symbol"],
        "status": status,
        "quotes": quotes,
        "quote_count": len(quotes),
        "errors": list(result.errors),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "writes_database": False,
    }


def _failed_fetch_payload(job: dict[str, str], error: str) -> dict[str, Any]:
    return {
        "generated_at": utc_now().isoformat(),
        "category": "crypto_quotes",
        "job_name": _job_name(job),
        "source": job["source"],
        "symbol": job["symbol"],
        "status": "FAILED",
        "quotes": [],
        "quote_count": 0,
        "errors": [error],
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "writes_database": False,
    }


def _quote_payload(quote: CryptoQuote) -> dict[str, Any]:
    return {
        "symbol": quote.symbol,
        "source": quote.source,
        "observed_at": quote.observed_at.isoformat(),
        "price_usd": str(quote.price_usd),
        "volume_24h": str(quote.volume_24h) if quote.volume_24h is not None else None,
        "market_cap": str(quote.market_cap) if quote.market_cap is not None else None,
        "raw_json": quote.raw_json,
    }


def _staged_crypto_quote_files(staging_dir: Path) -> list[Path]:
    direct = list(staging_dir.glob("crypto_quotes_*.json"))
    nested = [
        path for path in staging_dir.glob("*/crypto_quotes_*.json") if path.parent.name != "drained"
    ]
    return sorted({path for path in direct + nested})


def _render_markdown(payload: dict[str, Any]) -> str:
    stage = payload.get("stage_result") or {}
    drain = payload.get("drain_result") or {}
    lines = [
        "# Phase 3BB-R43 Single-Writer Coordinator",
        "",
        f"- Generated at: {payload.get('generated_at')}",
        "- Mode: PAPER ONLY coordinator",
        "- Live/demo execution: blocked",
        "- Order submission/cancel/replace: blocked",
        f"- Staging dir: `{payload.get('staging_dir')}`",
        "",
        "## Stage",
        "",
        f"- Status: {stage.get('status')}",
        f"- Jobs: {len(stage.get('jobs') or [])}",
        f"- Staged files: {len(stage.get('staged_files') or [])}",
        f"- Errors: {len(stage.get('errors') or [])}",
        "",
        "## Single Writer Drain",
        "",
        f"- Enabled: {payload.get('single_writer_drain_enabled')}",
        f"- Status: {drain.get('status')}",
        f"- Prices inserted: {drain.get('prices_inserted') or 0}",
        f"- Features inserted: {drain.get('features_inserted') or 0}",
        f"- Links created: {drain.get('links_created') or 0}",
        "",
        "## Next Action",
        "",
        str(payload.get("recommended_next_action") or ""),
    ]
    return "\n".join(lines) + "\n"


def _recommended_next_action(
    *,
    stage_result: dict[str, Any],
    drain_result: dict[str, Any],
    drain_staged: bool,
) -> str:
    drain_status = str(drain_result.get("status") or "")
    if drain_status == "BLOCKED_ACTIVE_WRITER":
        return "Wait for the active writer to finish, then rerun with --drain-staged."
    if not drain_staged:
        return (
            "Review staged files, run db-writer-monitor, then drain with --drain-staged "
            "if no writer is active."
        )
    if stage_result.get("errors") or drain_result.get("errors"):
        return "Review stage/drain errors before scheduling this coordinator."
    return (
        "Coordinator completed. It is ready to be called by the scheduler as the "
        "single writer drain."
    )


def _run_id() -> str:
    return f"{utc_now().strftime('%Y%m%dT%H%M%SZ')}_{os.getpid()}"


def _stage_filename(job: dict[str, str]) -> str:
    return f"crypto_quotes_{_safe_token(job['source'])}_{_safe_token(job['symbol'])}.json"


def _job_name(job: dict[str, str]) -> str:
    return f"crypto_quotes.{job['source']}.{job['symbol']}"


def _unique_symbols(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    resolved: list[str] = []
    for symbol in symbols:
        normalized = normalize_symbol(str(symbol))
        if normalized and normalized not in seen:
            seen.add(normalized)
            resolved.append(normalized)
    return resolved


def _unique_sources(sources: list[str]) -> list[str]:
    seen: set[str] = set()
    resolved: list[str] = []
    for source in sources:
        normalized = str(source).strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            resolved.append(normalized)
    return resolved


def _safe_token(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("_").lower() or "unknown"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
