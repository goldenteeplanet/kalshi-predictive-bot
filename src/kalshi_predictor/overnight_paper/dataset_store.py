"""Durable prospective dataset in the existing isolated ledger journal.

The caller owns the SQLite BEGIN IMMEDIATE transaction. No second connection,
schema, model, or paper ledger is created. Payload bytes and sequence hashes are
verified on every read; replay is idempotent and never rewrites prior records.
"""

import json
import re
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from kalshi_predictor.overnight_paper.evaluation_dataset import append_record, read_records
from kalshi_predictor.overnight_paper.provenance import Artifact
from kalshi_predictor.overnight_paper.store import encode


def _prefix(dataset: str) -> str:
    if not re.fullmatch(r"[a-z0-9-]{1,64}", dataset):
        raise ValueError("INVALID_DATASET_NAME")
    return "release-dataset:" + dataset + ":"


def load_dataset(session: Session, *, dataset: str) -> tuple[Artifact, ...]:
    prefix = _prefix(dataset)
    rows = session.execute(
        text("SELECT id,payload FROM overnight_sprint_cycles WHERE id LIKE :prefix ORDER BY id"),
        {"prefix": prefix + "%"},
    ).all()
    records = []
    for index, (key, payload) in enumerate(rows):
        if key != prefix + f"{index:012d}":
            raise ValueError("DATASET_SEQUENCE_GAP")
        envelope = json.loads(payload)
        if envelope.get("kind") != "PAPER_RELEASE_DATASET_RECORD":
            raise ValueError("DATASET_RECORD_KIND_MISMATCH")
        artifact = Artifact(envelope["sha256"], envelope["original_utf8"].encode("utf-8"))
        artifact.decode()
        records.append(artifact)
    result = tuple(records)
    read_records(result)
    return result


def persist_dataset_record(
    session: Session, *, dataset: str, record: Artifact, recorded_at: datetime
) -> str:
    """Append on the caller's writer transaction; return stored chain digest."""
    if not session.in_transaction():
        raise ValueError("DATASET_WRITER_TRANSACTION_REQUIRED")
    records = load_dataset(session, dataset=dataset)
    updated = append_record(records, record, recorded_at=recorded_at)
    if len(updated) == len(records):
        return next(
            entry.sha256 for entry in records if entry.decode()["record_sha256"] == record.sha256
        )
    entry = updated[-1]
    session.execute(
        text(
            "INSERT INTO overnight_sprint_cycles(id,captured_at,payload) VALUES(:id,:at,:payload)"
        ),
        {
            "id": _prefix(dataset) + f"{len(records):012d}",
            "at": recorded_at.isoformat(),
            "payload": encode(
                {
                    "kind": "PAPER_RELEASE_DATASET_RECORD",
                    "sha256": entry.sha256,
                    "original_utf8": entry.payload.decode("utf-8"),
                }
            ),
        },
    )
    return entry.sha256
