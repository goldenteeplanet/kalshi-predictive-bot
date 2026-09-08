"""Hash-verified diagnostic imports into the isolated evidence tables only."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from kalshi_predictor.overnight_paper.store import encode, initialize_store


def import_weather_archive(database: Path, archive: Path) -> str:
    """Retain provider clocks and rejected forecasts without promoting them to trading inputs."""
    raw = (archive / "result.json").read_bytes()
    result = json.loads(raw)
    for index, receipt in enumerate(result["requests"], 1):
        if receipt.get("status") != 200:
            raise ValueError("INCOMPLETE_PUBLIC_CAPTURE")
        response = (archive / f"response-{index:02}.json").read_bytes()
        if hashlib.sha256(response).hexdigest() != receipt["sha256"]:
            raise ValueError("PUBLIC_CAPTURE_HASH_MISMATCH")
    if result.get("role") != "DIAGNOSTIC_ONLY" or result.get("max_age_seconds") != 1800:
        raise ValueError("DIAGNOSTIC_BOUNDARY_OR_FRESHNESS_CHANGED")
    key = "weather:" + hashlib.sha256(raw).hexdigest()
    payload = {
        "kind": "WEATHER_DIAGNOSTIC",
        "archive": str(archive.absolute()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "result": result,
        "trading_input": False,
    }
    initialize_store(database)
    with closing(sqlite3.connect(database)) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("DATABASE_INTEGRITY_FAILED")
        for rule in result.get("rules", []):
            db.execute(
                "INSERT OR IGNORE INTO weather_settlement_rules "
                "(version,series,provider,effective_from,effective_to,payload) VALUES(?,?,?,?,?,?)",
                (
                    rule["version"],
                    rule["series"],
                    rule["provider"],
                    rule["effective_from"],
                    rule["effective_to"],
                    encode(rule),
                ),
            )
        db.execute(
            "INSERT OR IGNORE INTO overnight_sprint_cycles(id,captured_at,payload) VALUES(?,?,?)",
            (key, result["finished_at"], encode(payload)),
        )
        db.commit()
    return key
