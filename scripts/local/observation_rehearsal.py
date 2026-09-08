"""Bounded public-data observation; never creates orders or starts services."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.request import HTTPRedirectHandler, Request, build_opener

SERIES = "KXTEMPNYCH"
BASE = "https://external-api.kalshi.com/trade-api/v2"
MARKETS = f"/markets?series_ticker={SERIES}&status=open&limit=100"
FLAGS = {
    "execution_enabled": False,
    "execution_dry_run": True,
    "execution_gateway_mode": "disabled",
    "autopilot_enabled": False,
    "autopilot_dry_run": True,
    "paper_order_creation_enabled": False,
    "paper_order_kill_switch": True,
    "dynamic_position_sizing_mode": "disabled",
    "advanced_risk_engine_mode": "disabled",
    "kalshi_websocket_enabled": False,
}
WRITABLE = {"markets", "market_snapshots"}


def now() -> datetime:
    return datetime.now(UTC)


def save(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def allowed_path(path: str) -> bool:
    return path in {MARKETS, f"/series/{SERIES}"} or bool(
        re.fullmatch(r"/markets/KXTEMPNYCH-[A-Za-z0-9.-]+/orderbook\?depth=5", path)
    )


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("REDIRECT_REFUSED")


def public_get(path: str) -> None:
    if not allowed_path(path):
        raise ValueError("ENDPOINT_REFUSED")
    request = Request(BASE + path, method="GET", headers={"Accept": "application/json"})
    with build_opener(NoRedirect()).open(request, timeout=15) as response:
        raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("RESPONSE_TOO_LARGE")
        sys.stdout.write(
            json.dumps(
                {
                    "body": raw.decode("utf-8"),
                    "status": response.status,
                    "server_date": response.headers.get("Date"),
                }
            )
        )


class Capture:
    def __init__(self, root: Path, deadline: float) -> None:
        self.root = root
        self.deadline = deadline
        self.requests: list[dict[str, Any]] = []

    def get(self, path: str) -> tuple[dict[str, Any], datetime]:
        remaining = self.deadline - time.monotonic()
        if not allowed_path(path) or len(self.requests) >= 25 or remaining <= 0:
            raise RuntimeError("REQUEST_SCOPE_OR_BUDGET_REFUSED")
        row: dict[str, Any] = {"url": BASE + path, "method": "GET", "started_at": now()}
        self.requests.append(row)
        try:
            result = subprocess.run(
                [sys.executable, "-I", str(Path(__file__).resolve()), "--public-get", path],
                capture_output=True,
                check=True,
                timeout=min(15, remaining),
            )
            envelope = json.loads(result.stdout)
            raw = envelope["body"].encode("utf-8")
            received = now()
            name = f"response-{len(self.requests):02d}.json"
            (self.root / name).write_bytes(raw)
            row.update(
                {
                    "received_at": received,
                    "file": name,
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "status": envelope["status"],
                    "server_date": envelope["server_date"],
                }
            )
            if envelope["status"] != 200:
                raise RuntimeError("HTTP_FAILURE")
            return json.loads(raw), received
        except Exception as error:
            row["error"] = str(error)
            raise
        finally:
            save(self.root / "requests.json", self.requests)


def check_path(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise RuntimeError("LINKED_PATH_REFUSED")
    if "onedrive" in str(path).lower():
        raise RuntimeError("SYNCED_PATH_REFUSED")


def authorizer(action: int, table: str | None, *rest: Any) -> int:
    if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE):
        return sqlite3.SQLITE_OK if table in WRITABLE else sqlite3.SQLITE_DENY
    if action in (
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_TRANSACTION,
    ):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {
        name: connection.execute(
            'SELECT count(*) FROM "' + name.replace('"', '""') + '"'
        ).fetchone()[0]
        for (name,) in tables
    }


def audit(db: Path) -> dict[str, int]:
    check_path(db)
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=1) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("DATABASE_INTEGRITY_FAILED")
        result = counts(connection)
        if any(value for table, value in result.items() if table not in WRITABLE):
            raise RuntimeError("UNEXPECTED_DATABASE_ROWS")
        return result


def validate_market(market: dict[str, Any], at: datetime) -> None:
    close = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00"))
    if (
        not market["ticker"].startswith(SERIES + "-")
        or market.get("series_ticker", SERIES) != SERIES
    ):
        raise RuntimeError("SERIES_LINEAGE_MISMATCH")
    if market.get("status") != "active" or close < at + timedelta(minutes=15):
        raise RuntimeError("INACTIVE_OR_NEAR_CLOSE_MARKET")


def import_snapshot(db: Path, market: dict[str, Any], book: dict[str, Any], at: datetime) -> None:
    # Deliberately avoid repositories.insert_market_snapshot: it also captures memory events.
    from kalshi_predictor.kalshi.orderbook import parse_orderbook

    prices = parse_orderbook(book)
    with sqlite3.connect(db, timeout=1) as connection:
        connection.set_authorizer(authorizer)
        connection.execute(
            "INSERT INTO markets (ticker,event_ticker,series_ticker,title,status,close_time,"
            "raw_json,first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(ticker) DO UPDATE SET status=excluded.status,"
            "close_time=excluded.close_time,raw_json=excluded.raw_json,"
            "last_seen_at=excluded.last_seen_at",
            (
                market["ticker"],
                market.get("event_ticker"),
                SERIES,
                market.get("title"),
                market["status"],
                market["close_time"],
                json.dumps(market),
                at.isoformat(),
                at.isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO market_snapshots (ticker,captured_at,status,best_yes_bid,best_yes_ask,"
            "best_no_bid,best_no_ask,spread,raw_market_json,raw_orderbook_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                market["ticker"],
                at.isoformat(),
                market["status"],
                *[
                    str(value) if value is not None else None
                    for value in (
                        prices.best_yes_bid,
                        prices.best_yes_ask,
                        prices.best_no_bid,
                        prices.best_no_ask,
                        prices.spread,
                    )
                ],
                json.dumps(market),
                json.dumps(book),
            ),
        )


def check_settings(settings: Any, db: Path) -> None:
    if any(getattr(settings, key) != value for key, value in FLAGS.items()):
        raise RuntimeError("SAFETY_FLAG_CHANGED")
    if settings.kalshi_api_key_id or settings.kalshi_private_key_path:
        raise RuntimeError("CREDENTIALS_REFUSED")
    expected = "sqlite:///" + db.as_posix()
    if settings.kalshi_db_url != expected or any(
        os.environ.get(key) != expected for key in ("DATABASE_URL", "KALSHI_DB_URL")
    ):
        raise RuntimeError("DATABASE_PATH_MISMATCH")


def run(baseline: Path, baseline_sha256: str) -> Path:
    local = Path(os.environ["LOCALAPPDATA"]) / "CodexPaperRehearsals"
    check_path(local)
    check_path(baseline)
    root = local / (now().strftime("%Y%m%dT%H%M%SZ") + "-observation")
    root.mkdir(parents=True, exist_ok=False)
    os.chdir(root)
    kept = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}
    }
    os.environ.clear()
    os.environ.update(kept)
    os.environ.update({key.upper(): str(value).lower() for key, value in FLAGS.items()})
    db = root / "paper.db"
    os.environ["DATABASE_URL"] = "sqlite:///" + db.as_posix()
    os.environ["KALSHI_DB_URL"] = os.environ["DATABASE_URL"]
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo / "src"))
    from sqlalchemy import create_engine

    from kalshi_predictor.config import Settings
    from kalshi_predictor.data.schema import Base
    from kalshi_predictor.kalshi.orderbook import usable_bid_ask_book
    from kalshi_predictor.phase3ap import MIN_EXECUTABLE_LIQUIDITY_SCORE
    from kalshi_predictor.phase_gh4 import build_gh4_paper_activation_preflight

    settings = Settings(_env_file=None)
    code = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo).decode()
    deadline = time.monotonic() + 570  # Outer process watchdog is 600 seconds.
    capture = Capture(root, deadline)
    report: dict[str, Any] = {
        "code_commit": code,
        "started_at": now(),
        "root": str(root),
        "flags": FLAGS,
        "cycles": [],
        "observation_status": "BLOCKED",
        "trading_readiness": "BLOCKED",
        "baseline_sha256": baseline_sha256,
        "credentials_loaded": False,
    }
    save(root / "progress.json", report)
    try:
        if dirty:
            raise RuntimeError("DIRTY_CODE_CHECKOUT")
        check_settings(settings, db)
        if hashlib.sha256(baseline.read_bytes()).hexdigest() != baseline_sha256:
            raise RuntimeError("BASELINE_HASH_MISMATCH")
        shutil.copy2(baseline, db)
        before = audit(db)
        if any(before.values()):
            raise RuntimeError("BASELINE_NOT_EMPTY")
        schema_engine = create_engine("sqlite://")
        try:
            Base.metadata.create_all(schema_engine)
            with schema_engine.connect() as expected, sqlite3.connect(db) as actual:
                sql = "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
                if [tuple(row) for row in expected.exec_driver_sql(sql)] != actual.execute(
                    sql
                ).fetchall():
                    raise RuntimeError("SCHEMA_MISMATCH")
        finally:
            schema_engine.dispose()
        report["initial_counts"] = before
        series, _ = capture.get(f"/series/{SERIES}")
        if series["series"]["ticker"] != SERIES:
            raise RuntimeError("SERIES_LINEAGE_MISMATCH")
        report["settlement_sources"] = series["series"].get("settlement_sources", [])
        selected: list[str] = []
        previous_start: float | None = None
        for cycle in range(5):
            if previous_start is not None:
                while time.monotonic() < previous_start + 60:
                    time.sleep(max(0, min(1, previous_start + 60 - time.monotonic())))
            previous_start = time.monotonic()
            if previous_start >= deadline:
                raise RuntimeError("TIME_BUDGET_EXHAUSTED")
            check_settings(Settings(_env_file=None), db)
            before_cycle = audit(db)
            payload, received = capture.get(MARKETS)
            markets = {row["ticker"]: row for row in payload["markets"]}
            if not selected:
                for ticker, market in sorted(markets.items()):
                    try:
                        validate_market(market, received)
                    except RuntimeError as error:
                        if str(error) == "INACTIVE_OR_NEAR_CLOSE_MARKET":
                            continue
                        raise
                    selected.append(ticker)
                    if len(selected) == 3:
                        break
                if not selected:
                    raise RuntimeError("NO_ELIGIBLE_MARKETS_IN_BOUNDED_PAGE")
            rows = []
            for ticker in selected:
                if ticker not in markets:
                    raise RuntimeError("SELECTED_MARKET_DISAPPEARED")
                market = markets[ticker]
                validate_market(market, now())
                book, book_received = capture.get(f"/markets/{ticker}/orderbook?depth=5")
                if (now() - received).total_seconds() > 60 or time.monotonic() >= deadline:
                    raise RuntimeError("STALE_CAPTURE_OR_TIME_BUDGET")
                import_snapshot(db, market, book, book_received)
                assessed = usable_bid_ask_book(
                    book,
                    side="YES",
                    liquidity_score=None,
                    min_liquidity_score=MIN_EXECUTABLE_LIQUIDITY_SCORE,
                    max_spread=settings.opportunity_max_spread,
                )
                fp = book.get("orderbook_fp", {})
                rows.append(
                    {
                        "ticker": ticker,
                        "status": market["status"],
                        "close_time": market["close_time"],
                        "market_received_at": received,
                        "book_received_at": book_received,
                        "provider_market_updated_at": market.get("updated_time"),
                        "provider_book_updated_at": book.get("updated_time"),
                        "quote_event_freshness": "UNVERIFIED",
                        "yes_bid_levels": len(fp.get("yes_dollars") or []),
                        "no_bid_levels": len(fp.get("no_dollars") or []),
                        "book_gate": asdict(assessed),
                        "source_lineage": "UNCERTIFIED_NO_FORECAST_OR_SETTLEMENT_LINK",
                    }
                )
            preflight = build_gh4_paper_activation_preflight(
                settings=settings,
                gh2_report_path=root / "gh2-not-produced.json",
                gh2_history_path=root / "gh2-not-produced.jsonl",
                gh1_status_path=root / "gh1-not-produced.json",
                now=now(),
            )
            if preflight["preflight_ready"]:
                raise RuntimeError("UNEXPECTED_READY_WITHOUT_EVIDENCE")
            save(root / f"gh4-cycle-{cycle + 1}.json", preflight)
            after_cycle = audit(db)
            if (
                after_cycle["market_snapshots"] - before_cycle["market_snapshots"] != len(selected)
                or after_cycle["markets"] != len(selected)
            ):
                raise RuntimeError("UNEXPECTED_OBSERVATION_ROW_DELTA")
            check_settings(Settings(_env_file=None), db)
            report["cycles"].append(
                {
                    "number": cycle + 1,
                    "completed_at": now(),
                    "markets": rows,
                    "counts_before": before_cycle,
                    "counts_after": after_cycle,
                    "gh4_status": preflight["status"],
                    "gh4_failed_checks": preflight["failed_checks"],
                }
            )
            save(root / "progress.json", report)
            print(
                f"Cycle {cycle + 1}/5 complete; readiness blocked; orders remain zero.", flush=True
            )
        report["observation_status"] = "PASS"
    except Exception as error:
        report["stop_reason"] = str(error)
    finally:
        report["finished_at"] = now()
        report["request_count"] = len(capture.requests)
        if db.exists():
            try:
                report["final_counts"] = audit(db)
                report["database_integrity"] = "ok"
            except Exception as error:
                report["observation_status"] = "BLOCKED"
                report["final_audit_error"] = str(error)
        save(root / "result.json", report)
        print(str(root / "result.json"), flush=True)
    return root


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-get")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--baseline-sha256")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.public_get:
        public_get(arguments.public_get)
    elif arguments.baseline and arguments.baseline_sha256 and not arguments.worker:
        try:
            child = subprocess.run(
                [sys.executable, "-I", str(Path(__file__).resolve()), *sys.argv[1:], "--worker"],
                timeout=600,
                check=False,
            )
            raise SystemExit(child.returncode)
        except subprocess.TimeoutExpired:
            print("REHEARSAL_WATCHDOG_EXPIRED; retain the run directory for inspection.")
            raise SystemExit(2) from None
    elif arguments.baseline and arguments.baseline_sha256:
        result_root = run(arguments.baseline.absolute(), arguments.baseline_sha256)
        result = json.loads((result_root / "result.json").read_text())
        raise SystemExit(0 if result["observation_status"] == "PASS" else 2)
    else:
        parser.error("--baseline and --baseline-sha256 are required")
