"""Durable research route receipts around the unchanged CF average journal.

Sidecar originals describe this route invocation, not a retroactive rewrite of
the forecast's source closure. Incomplete sidecars fail closed; no repair or
paper path is implicit.
"""

from __future__ import annotations

import importlib
import json
import math
import os
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from kalshi_predictor.crypto import research_shadow as S
from kalshi_predictor.forecasting.model_roles import ModelRole, research_model_role

MODEL = "crypto_settlement_average_research_v1"
ROUTE = "crypto_v3/settlement_average"


def now() -> datetime:
    return datetime.now(UTC)


def _load(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("DUPLICATE_ROUTE_JSON")
            result[key] = value
        return result

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("NONFINITE_ROUTE_JSON")
        return result

    value = json.loads(raw, object_pairs_hook=pairs, parse_float=number, parse_constant=number)
    if type(value) is not dict:
        raise ValueError("ROUTE_OBJECT_REQUIRED")
    return value


def _read(path: Path, limit: int = S.LIMIT) -> bytes:
    if path.is_symlink() or not 0 < path.stat().st_size <= limit:
        raise ValueError("BOUNDED_ROUTE_ORIGINAL_REQUIRED")
    return path.read_bytes()


def _write(path: Path, raw: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def route_sources() -> dict:
    root = Path(__file__).resolve().parents[1]
    sources = {}
    for name in ("crypto_average_shadow_route", "crypto_research_router", "model_roles"):
        module = importlib.import_module("kalshi_predictor.forecasting." + name)
        if module.__file__ is None:
            raise ValueError("ROUTE_SOURCE_IDENTITY")
        path = Path(module.__file__).resolve()
        if path != root / "forecasting" / (name + ".py"):
            raise ValueError("ROUTE_SOURCE_IDENTITY")
        raw = _read(path)
        sources[name] = {"sha256": S.sha(raw), "hex": raw.hex()}
    return sources


def _root(journal: Path) -> Path:
    if not isinstance(journal, Path) or journal.is_symlink():
        raise ValueError("EXPLICIT_RESEARCH_JOURNAL_REQUIRED")
    root = journal.resolve().with_name(journal.name + ".crypto-v3-route")
    if root.is_symlink():
        raise ValueError("ROUTE_SYMLINK")
    return root


def _row(journal: Path, decision_id: str) -> tuple[dict, str, str]:
    with closing(S.connect(journal, readonly=True)) as db:
        row = db.execute(
            "SELECT payload,payload_sha FROM research_shadow WHERE id=?", (decision_id,)
        ).fetchone()
        completion = db.execute(
            "SELECT payload,payload_sha FROM research_completion WHERE id=?", (decision_id,)
        ).fetchone()
        if row is None or completion is None or S.sha(completion[0]) != completion[1]:
            raise ValueError("COMPLETE_DURABLE_DECISION_REQUIRED")
        decision = S._read_decision(db, row[0], row[1])
        if (
            decision["decision_id"] != decision_id
            or decision["journal_completion"]["decision_id"] != decision_id
            or decision["forecast"]["model"] != MODEL
            or decision["forecast"]["model_role"] != ModelRole.RESEARCH_CHALLENGER
            or research_model_role(MODEL) is not ModelRole.RESEARCH_CHALLENGER
            or decision["paper_eligible"] is not False
            or decision["execution_authority"] is not False
        ):
            raise ValueError("RESEARCH_ROUTE_MODEL_OR_AUTHORITY")
        return decision, row[1], completion[1]


def read_route(journal: Path, decision_id: str) -> dict:
    if len(decision_id) != 64 or any(c not in "0123456789abcdef" for c in decision_id):
        raise ValueError("DECISION_ID_REQUIRED")
    directory = _root(journal) / decision_id
    if directory.is_symlink() or (directory / "failure.json").exists():
        raise ValueError("INCOMPLETE_ROUTE_RECEIPT")
    raw = _read(directory / "route.json")
    receipt = _load(_read(directory / "completion.json"))
    value = _load(raw)
    decision, payload_sha, completion_sha = _row(journal, decision_id)
    if (
        receipt["route_sha256"] != S.sha(raw)
        or value["schema"] != "crypto-v3-average-route-v1"
        or value["route"] != ROUTE
        or value["decision_id"] != decision_id
        or value["request_sha256"] != decision["request_sha256"]
        or value["payload_sha256"] != payload_sha
        or value["completion_sha256"] != completion_sha
        or value["model"] != MODEL
        or value["model_role"] != "RESEARCH_CHALLENGER"
        or value["forecast_status"] != decision["journal_completion"]["status"]
        or value["execution_authority"] is not False
        or value["paper_eligible"] is not False
        or not S.at(value["invoked_at"])
        <= S.at(decision["decision_at"])
        <= S.at(decision["computed_at"])
        <= S.at(decision["journal_completion"]["original_committed_before"])
        <= S.at(value["linked_at"])
        <= S.at(receipt["recorded_after_route"])
        <= now()
    ):
        raise ValueError("ROUTE_RECEIPT_BINDING")
    expected_names = {"crypto_average_shadow_route", "crypto_research_router", "model_roles"}
    if set(value["route_source_originals"]) != expected_names:
        raise ValueError("ROUTE_SOURCE_CLOSURE")
    for artifact in value["route_source_originals"].values():
        S.original(artifact)
    if not S.same(value["route_source_originals"], route_sources()):
        # v1 supports only the exact current reviewed closure. Historical source
        # migrations need a separately reviewed registry; never silently relabel.
        raise ValueError("ROUTE_SOURCE_VERSION_NOT_CURRENT")
    return {
        "route": ROUTE,
        "route_sha256": S.sha(raw),
        "route_receipt": value,
        "route_completion": receipt,
        "decision": decision,
        "paper_eligible": False,
        "execution_authority": False,
    }


def record_average_shadow(*, journal: Path, request_raw: bytes) -> dict:
    if type(request_raw) is not bytes or not 0 < len(request_raw) <= S.LIMIT:
        raise ValueError("BOUNDED_ORIGINAL_REQUEST_REQUIRED")
    root = _root(journal)
    if research_model_role(MODEL) is not ModelRole.RESEARCH_CHALLENGER:
        raise ValueError("RESEARCH_ROLE_REQUIRED")
    with closing(S.connect(journal, readonly=True)) as db:
        existing = db.execute(
            "SELECT id FROM research_shadow WHERE request_sha=?", (S.sha(request_raw),)
        ).fetchone()
    if existing:
        # Never relabel a previously standalone prediction as created through this route.
        return read_route(journal, existing[0])
    sources, invoked = route_sources(), now()
    # Checked atomically under the writer's BEGIN IMMEDIATE. A competing
    # standalone insert cannot be adopted between our initial lookup and append.
    decision = S.append_decision(journal, request_raw, require_new=True)
    actual, payload_sha, completion_sha = _row(journal, decision["decision_id"])
    if not S.same(actual, decision) or sources != route_sources():
        raise ValueError("ROUTE_SOURCE_OR_JOURNAL_CHANGED")
    linked = now()
    if not invoked <= linked:
        raise ValueError("ROUTE_CLOCK")
    value = {
        "schema": "crypto-v3-average-route-v1",
        "route": ROUTE,
        "model": MODEL,
        "model_role": research_model_role(MODEL),
        "decision_id": decision["decision_id"],
        "request_sha256": S.sha(request_raw),
        "payload_sha256": payload_sha,
        "completion_sha256": completion_sha,
        "invoked_at": invoked.isoformat(),
        "linked_at": linked.isoformat(),
        "route_source_originals": sources,
        "paper_eligible": False,
        "execution_authority": False,
        "forecast_status": decision["journal_completion"]["status"],
    }
    root.mkdir(exist_ok=True)
    directory = root / decision["decision_id"]
    directory.mkdir(exist_ok=False)
    try:
        raw = S.encode(value)
        _write(directory / "route.json", raw)
        recorded = now()
        if not linked <= recorded or sources != route_sources():
            raise ValueError("ROUTE_PUBLICATION_CLOCK_OR_SOURCE")
        _write(
            directory / "completion.json",
            S.encode({"route_sha256": S.sha(raw), "recorded_after_route": recorded.isoformat()}),
        )
        if not recorded <= now() or sources != route_sources():
            raise ValueError("ROUTE_PUBLICATION_CLOCK_OR_SOURCE")
    except Exception as error:
        _write(
            directory / "failure.json",
            S.encode({"status": "INCOMPLETE_ROUTE", "reason": str(error)}),
        )
        raise
    return read_route(journal, decision["decision_id"])


def route_status(journal: Path) -> dict:
    # Validate dedicated DB even if there is not yet a route sidecar.
    with closing(S.connect(journal, readonly=True)):
        pass
    root = _root(journal)
    rows = []
    if root.exists():
        directories = list(root.iterdir())
        if len(directories) > 1000:
            raise ValueError("ROUTE_STATUS_BUDGET")
        rows = [read_route(journal, directory.name) for directory in sorted(directories)]
    return {
        "schema": "crypto-v3-average-route-status-v1",
        "route": ROUTE,
        "rows": rows,
        "historical_routed_decisions": len(rows),
        "completed_routed_decisions": sum(
            row["decision"]["journal_completion"]["status"] == "COMPLETE_RESEARCH" for row in rows
        ),
        "historical_events": len({row["decision"]["event"] for row in rows}),
        "paper_eligible": False,
        "execution_authority": False,
        "positive_complete_net_ev": 0,
    }
