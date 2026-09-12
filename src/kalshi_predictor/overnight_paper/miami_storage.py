"""Active ownership and exact authorized file binding for Miami preparation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from weakref import WeakValueDictionary

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from kalshi_predictor.data.schema import Base

from .boundary import ExecutionMode, LocalPaperAuthorization, authorization_fingerprint
from .coordinator import _owned_session_factory, _verify_database
from .runtime_owner import RuntimeOwner, validate_runtime_owner
from .source_health import aware

_ISSUED: WeakValueDictionary[int, MiamiOwnedStorage] = WeakValueDictionary()


@dataclass(frozen=True)
class MiamiOwnedStorage:
    """Live process handle, not serializable authority or a replacement for writer gates."""

    database_path: Path
    owner: RuntimeOwner
    authorization: LocalPaperAuthorization
    engine: Engine

    def __reduce__(self):
        raise TypeError("MIAMI_STORAGE_IS_NOT_SERIALIZABLE")


def owned_miami_factory(
    supplied: sessionmaker[Session],
    *,
    database_path: Path,
    owner: RuntimeOwner,
    authorization: LocalPaperAuthorization,
) -> tuple[sessionmaker[Session], MiamiOwnedStorage]:
    validate_runtime_owner(owner, database_path)
    path = database_path.resolve(strict=True)
    factory = _owned_session_factory(supplied, path)
    # SQLAlchemy sessionmaker normally creates its own Session subclass. Use
    # the exact plain class on our newly issued factory, never the supplied one.
    factory.class_ = Session
    storage = MiamiOwnedStorage(path, owner, authorization, factory.kw["bind"])
    _ISSUED[id(storage)] = storage
    return factory, storage


def verify_miami_storage(session: Session, storage: MiamiOwnedStorage, *, now: datetime):
    if (
        type(storage) is not MiamiOwnedStorage
        or _ISSUED.get(id(storage)) is not storage
        or type(session) is not Session
    ):
        raise ValueError("CONCRETE_MIAMI_OWNED_STORAGE_REQUIRED")
    path, auth = storage.database_path, storage.authorization
    validate_runtime_owner(storage.owner, path)
    if (
        type(auth) is not LocalPaperAuthorization
        or auth.mode != ExecutionMode.LOCAL_PAPER
        or not auth.database_id
        or not auth.isolated_database_path
        or Path(auth.isolated_database_path).resolve(strict=True) != path
        or not aware(auth.created_at) <= aware(now) < aware(auth.expires_at)
    ):
        raise ValueError("MIAMI_STORAGE_AUTHORIZATION_IDENTITY_OR_TIME")
    if (
        type(storage.engine) is not Engine
        or session.get_bind() is not storage.engine
        or storage.engine.url.get_backend_name() != "sqlite"
        or storage.engine.url.get_driver_name() != "pysqlite"
        or storage.engine.url.query
        or not storage.engine.url.database
        or Path(storage.engine.url.database).resolve(strict=True) != path
        or any(
            session.get_bind(mapper=m.class_) is not storage.engine for m in Base.registry.mappers
        )
    ):
        raise ValueError("MIAMI_STORAGE_ENGINE_BINDING")
    if session.new or session.dirty or session.deleted:
        raise ValueError("UNFLUSHED_CALLER_CHANGES_FORBIDDEN")
    _verify_database(session, path)
    raw = session.execute(
        text("SELECT payload FROM overnight_sprint_cycles WHERE id=:key"),
        dict(key="authorization-baseline:" + auth.database_id),
    ).scalar_one_or_none()
    if raw is None:
        raise ValueError("MIAMI_STORAGE_AUTHORIZATION_BASELINE_REQUIRED")
    baseline = json.loads(raw)
    if (
        baseline.get("kind") != "LOCAL_PAPER_AUTHORIZATION_BASELINE_V1"
        or baseline.get("database_id") != auth.database_id
        or Path(baseline.get("database_path", "")).resolve() != path
        or baseline.get("authorization_sha256") != authorization_fingerprint(auth)
        or baseline.get("objective_sha256") != auth.objective_sha256
    ):
        raise ValueError("MIAMI_STORAGE_AUTHORIZATION_BASELINE_MISMATCH")
    validate_runtime_owner(storage.owner, path)
    return session.connection()
