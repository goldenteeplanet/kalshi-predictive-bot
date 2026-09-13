"""Durability checks on an isolated fixture ledger, no runtime data writes."""

import json
import shutil
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from test_overnight_activation import baseline_template  # noqa: F401
from test_paper_release_dataset import observation, stamp

from kalshi_predictor.overnight_paper.dataset_store import load_dataset, persist_dataset_record
from kalshi_predictor.overnight_paper.store import initialize_store


@pytest.fixture
def factory(tmp_path, baseline_template):  # noqa: F811
    path = tmp_path / "dataset.db"
    shutil.copyfile(baseline_template, path)
    initialize_store(path)
    engine = create_engine(f"sqlite:///{path}")
    yield sessionmaker(engine)
    engine.dispose()


def test_commit_restart_and_duplicate_keep_identical_originals(factory):
    item = observation(9)
    with factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        key = persist_dataset_record(session, dataset="fixture", record=item, recorded_at=stamp(9))
        session.commit()
    with factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        assert (
            persist_dataset_record(
                session, dataset="fixture", record=item, recorded_at=stamp(9) + timedelta(minutes=5)
            )
            == key
        )
        session.commit()
    with factory() as session:
        stored = load_dataset(session, dataset="fixture")
        assert len(stored) == 1
        assert stored[0].decode()["record"] == item.decode()
        assert stored[0].decode()["record_sha256"] == item.sha256


def test_interrupted_transaction_is_not_a_prospective_record(factory):
    with factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        persist_dataset_record(
            session, dataset="fixture", record=observation(9), recorded_at=stamp(9)
        )
        session.rollback()
    with factory() as session:
        assert load_dataset(session, dataset="fixture") == ()
        session.rollback()
        session.execute(text("BEGIN IMMEDIATE"))
        with pytest.raises(ValueError, match="PROSPECTIVE_APPEND_DELAY"):
            persist_dataset_record(
                session,
                dataset="fixture",
                record=observation(9),
                recorded_at=stamp(9) + timedelta(minutes=5),
            )


def test_modified_original_rejected_on_read(factory):
    with factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        persist_dataset_record(
            session, dataset="fixture", record=observation(9), recorded_at=stamp(9)
        )
        payload = json.loads(
            session.execute(text("SELECT payload FROM overnight_sprint_cycles")).scalar_one()
        )
        payload["original_utf8"] += " "
        session.execute(
            text("UPDATE overnight_sprint_cycles SET payload=:p"), {"p": json.dumps(payload)}
        )
        session.commit()
    with factory() as session:
        with pytest.raises(ValueError, match="ARTIFACT_HASH_MISMATCH"):
            load_dataset(session, dataset="fixture")


def test_missing_sequence_rejected(factory):
    with factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        for day in (9, 10):
            persist_dataset_record(
                session, dataset="fixture", record=observation(day), recorded_at=stamp(day)
            )
        session.execute(
            text(
                "DELETE FROM overnight_sprint_cycles "
                "WHERE id='release-dataset:fixture:000000000000'"
            )
        )
        session.commit()
    with factory() as session:
        with pytest.raises(ValueError, match="DATASET_SEQUENCE_GAP"):
            load_dataset(session, dataset="fixture")


def test_missing_transaction_rejected(factory):
    with factory() as session:
        with pytest.raises(ValueError, match="DATASET_WRITER_TRANSACTION_REQUIRED"):
            persist_dataset_record(
                session, dataset="fixture", record=observation(9), recorded_at=stamp(9)
            )
