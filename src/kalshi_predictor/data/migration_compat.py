"""Explicit compatibility checks for mixed metadata/migration schema histories.

Call only with a caller-owned Alembic connection. Existing evidence is never
rewritten to make it compatible, and SQL exceptions are never treated as success.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import sqlalchemy as sa

logger = logging.getLogger(__name__)
LANE_CHECK_NAME = "ck_canonical_evaluations_source_lane"
LANE_CHECK_SQL = "source_lane IN ('HISTORICAL_REPLAY', 'SHADOW', 'GUARDED_PAPER')"


def _default(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    # PostgreSQL reflection includes a type cast on literal text defaults.
    if normalized in {"'{}'", "'{}'::text", "('{}')"}:
        return "{}"
    return normalized


def _same_type(actual: Any, expected: Any) -> bool:
    if isinstance(expected, sa.Text):
        return isinstance(actual, sa.Text)
    if isinstance(expected, sa.String):
        return (
            isinstance(actual, sa.String)
            and not isinstance(actual, sa.Text)
            and actual.length == expected.length
        )
    if isinstance(expected, sa.DateTime):
        # SQLite does not preserve timezone=True in its declared DATETIME type.
        return isinstance(actual, sa.DateTime)
    return False


def ensure_compatible_column(
    operations: Any,
    table: str,
    column: sa.Column,
    *,
    allow_stronger_not_null: bool = False,
    allow_absent_server_default: bool = False,
) -> dict[str, Any]:
    """Add an absent column or accept only the explicitly compatible existing shape."""
    inspector = sa.inspect(operations.get_bind())
    if not inspector.has_table(table):
        raise ValueError("MIGRATION_REQUIRED_TABLE_MISSING:" + table)
    existing = next(
        (item for item in inspector.get_columns(table) if item["name"] == column.name), None
    )
    if existing is None:
        with operations.batch_alter_table(table) as batch:
            batch.add_column(column)
        return {"table": table, "column": column.name, "action": "ADDED"}
    expected_default = (
        None if column.server_default is None else _default(column.server_default.arg)
    )
    actual_default = _default(existing.get("default"))
    defaults = {expected_default}
    if allow_absent_server_default:
        defaults.add(None)
    nullable_matches = existing["nullable"] == column.nullable or (
        allow_stronger_not_null and column.nullable and not existing["nullable"]
    )
    if (
        not _same_type(existing["type"], column.type)
        or not nullable_matches
        or actual_default not in defaults
        or existing.get("computed") is not None
        or existing.get("identity") is not None
        or column.name in inspector.get_pk_constraint(table).get("constrained_columns", [])
        or (
            isinstance(column.type, sa.DateTime)
            and inspector.bind.dialect.name != "sqlite"
            and getattr(existing["type"], "timezone", None) != column.type.timezone
        )
    ):
        raise ValueError("MIGRATION_INCOMPATIBLE_EXISTING_COLUMN:" + table + "." + column.name)
    accepted = {
        "table": table,
        "column": column.name,
        "action": "VERIFIED_EXISTING",
        "type": str(existing["type"]),
        "nullable": existing["nullable"],
        "server_default": actual_default,
        "stronger_not_null": bool(column.nullable and not existing["nullable"]),
    }
    logger.info("Migration accepted existing compatible shape: %s", accepted)
    return accepted


def _check_expression(value: str) -> str:
    # Only whitespace normalization; never rewrite operators, values or Boolean logic.
    return "".join(
        part if part.startswith("'") else re.sub(r"\s+", "", part)
        for part in re.split(r"('(?:''|[^'])*')", value)
    )


def ensure_canonical_lane_check(operations: Any) -> dict[str, Any]:
    bind = operations.get_bind()
    inspector = sa.inspect(bind)
    table = "canonical_evaluations"
    if not inspector.has_table(table):
        raise ValueError("MIGRATION_REQUIRED_TABLE_MISSING:" + table)
    checks = inspector.get_check_constraints(table)
    expected = _check_expression(LANE_CHECK_SQL)
    equivalent = []
    for check in checks:
        same = _check_expression(check.get("sqltext") or "") == expected
        if check.get("name") == LANE_CHECK_NAME and not same:
            raise ValueError("MIGRATION_EXISTING_LANE_CHECK_CONFLICT")
        if same:
            equivalent.append(check.get("name"))
    invalid = bind.execute(
        sa.text(
            "SELECT count(*) FROM canonical_evaluations WHERE source_lane IS NULL "
            "OR source_lane NOT IN ('HISTORICAL_REPLAY', 'SHADOW', 'GUARDED_PAPER')"
        )
    ).scalar_one()
    if invalid:
        raise ValueError("MIGRATION_INVALID_CANONICAL_LANE_ROWS:" + str(invalid))
    if equivalent:
        accepted = {"table": table, "action": "VERIFIED_EXISTING_CHECK", "names": equivalent}
        logger.info("Migration accepted existing equivalent constraint: %s", accepted)
        return accepted
    # Explicit copy_from preserves unnamed CHECK constraints, which Alembic's
    # default reflected-batch path deliberately drops. Refuse unreflected objects.
    reflected = sa.Table(table, sa.MetaData(), autoload_with=bind)
    if bind.dialect.name == "sqlite":
        triggers = bind.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=:table"),
            {"table": table},
        ).all()
        if triggers:
            raise ValueError("MIGRATION_UNSUPPORTED_TABLE_TRIGGERS")
        indexes = (
            bind.execute(
                sa.text(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name=:table AND sql IS NOT NULL"
                ),
                {"table": table},
            )
            .scalars()
            .all()
        )
        if set(indexes) != {index.name for index in reflected.indexes}:
            raise ValueError("MIGRATION_UNREFLECTED_TABLE_INDEX")
    with operations.batch_alter_table(table, copy_from=reflected) as batch:
        batch.create_check_constraint(LANE_CHECK_NAME, LANE_CHECK_SQL)
    return {"table": table, "action": "ADDED_CHECK", "name": LANE_CHECK_NAME}
