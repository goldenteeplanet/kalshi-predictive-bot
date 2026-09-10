"""Actual migration traversal on caller-owned disposable SQLite connections."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations

from kalshi_predictor.data.migration_compat import (
    LANE_CHECK_NAME,
    LANE_CHECK_SQL,
    ensure_canonical_lane_check,
    ensure_compatible_column,
)

ROOT = Path(__file__).resolve().parents[1]


def config(connection):
    result = Config()
    result.set_main_option("script_location", str(ROOT / "alembic"))
    result.set_main_option("sqlalchemy.url", "sqlite:///DO_NOT_OPEN_CONFIG_DATABASE.db")
    result.attributes["connection"] = connection
    return result


def operations(connection):
    return Operations(MigrationContext.configure(connection))


def forbidden(*args, **kwargs):
    pytest.fail("Injected migration connection must bypass settings and engine creation")


def test_actual_empty_to_head_and_second_run_bypass_settings(tmp_path, monkeypatch):
    monkeypatch.setattr("kalshi_predictor.config.get_settings", forbidden)
    monkeypatch.setattr(sa, "engine_from_config", forbidden)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    with engine.begin() as connection:
        command.upgrade(config(connection), "head")
        assert (
            connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            == "20260909_0024"
        )
        before = connection.execute(
            sa.text("SELECT name,sql FROM sqlite_master ORDER BY name")
        ).all()
        command.upgrade(config(connection), "head")
        assert (
            connection.execute(sa.text("SELECT name,sql FROM sqlite_master ORDER BY name")).all()
            == before
        )
        assert not connection.closed
        assert connection.execute(sa.text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert connection.execute(sa.text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()


@pytest.mark.parametrize("supplied", [None, object(), "sqlite:///not-a-connection.db"])
def test_invalid_injected_connection_never_falls_back(supplied, monkeypatch):
    monkeypatch.setattr("kalshi_predictor.config.get_settings", forbidden)
    monkeypatch.setattr(sa, "engine_from_config", forbidden)
    with pytest.raises(ValueError, match="EXPLICIT_OPEN"):
        command.upgrade(config(supplied), "head")


def test_mixed_0010_traversal_preserves_rows_constraints_and_lineage(tmp_path):
    from kalshi_predictor.data.schema import Base

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mixed.db'}")
    with engine.begin() as connection:
        Base.metadata.create_all(connection)
        op = operations(connection)
        with op.batch_alter_table("research_checkpoints") as batch:
            batch.drop_column("disposition_counts_json")
        with op.batch_alter_table("canonical_evaluations") as batch:
            batch.drop_constraint(LANE_CHECK_NAME, type_="check")
        connection.execute(
            sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
        )
        connection.execute(sa.text("INSERT INTO alembic_version VALUES ('20260623_0010')"))
        # Sentinel rows satisfy current metadata only in this disposable fixture.
        for name in (
            "research_runs",
            "research_checkpoints",
            "canonical_evaluations",
            "prospective_capture_runs",
            "prospective_paired_captures",
            "prospective_pair_evaluations",
        ):
            columns = sa.inspect(connection).get_columns(name)
            values = {}
            for column in columns:
                if column["name"] == "source_lane":
                    value = "HISTORICAL_REPLAY"
                elif column["name"] == "id":
                    value = 1
                elif isinstance(column["type"], sa.Integer):
                    value = 0
                elif isinstance(column["type"], sa.DateTime):
                    value = "2026-09-09 00:00:00"
                else:
                    value = "sentinel"
                if not column["nullable"] or column["name"] in {"capture_id", "run_id"}:
                    values[column["name"]] = value
            names = ",".join('"' + key + '"' for key in values)
            binds = ",".join(":" + key for key in values)
            connection.execute(sa.text(f'INSERT INTO "{name}" ({names}) VALUES ({binds})'), values)
        before = {
            name: connection.execute(sa.text(f'SELECT * FROM "{name}"')).mappings().all()
            for name in (
                "research_checkpoints",
                "canonical_evaluations",
                "prospective_pair_evaluations",
            )
        }
        command.upgrade(config(connection), "head")
        for name, records in before.items():
            after = connection.execute(sa.text(f'SELECT * FROM "{name}"')).mappings().all()
            assert [{key: row[key] for key in records[0]} for row in after] == records
        assert (
            connection.execute(
                sa.text("SELECT disposition_counts_json FROM research_checkpoints")
            ).scalar_one()
            == "{}"
        )
        columns = {
            col["name"]: col
            for col in sa.inspect(connection).get_columns("prospective_pair_evaluations")
        }
        assert columns["settlement_hash"]["nullable"] is False
        assert columns["settlement_updated_at"]["nullable"] is False
        assert (
            sa.inspect(connection).get_check_constraints("canonical_evaluations")[0]["sqltext"]
            == LANE_CHECK_SQL
        )
        assert connection.execute(sa.text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert connection.execute(sa.text("PRAGMA foreign_key_check")).all() == []
        command.upgrade(config(connection), "head")
    engine.dispose()


@pytest.mark.parametrize(
    "declaration", ["INTEGER", "TEXT NOT NULL DEFAULT 'wrong'", "TEXT", "TEXT NOT NULL PRIMARY KEY"]
)
def test_incompatible_checkpoint_shape_fails(declaration):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE research_checkpoints (disposition_counts_json " + declaration + ")"
            )
        )
        with pytest.raises(ValueError, match="INCOMPATIBLE_EXISTING"):
            ensure_compatible_column(
                operations(connection),
                "research_checkpoints",
                sa.Column(
                    "disposition_counts_json", sa.Text(), nullable=False, server_default="{}"
                ),
                allow_absent_server_default=True,
            )
    engine.dispose()


def test_partial_columns_added_and_stronger_lineage_recorded():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE prospective_pair_evaluations (settlement_hash VARCHAR(64) NOT NULL)"
            )
        )
        op = operations(connection)
        result = ensure_compatible_column(
            op,
            "prospective_pair_evaluations",
            sa.Column("settlement_hash", sa.String(64), nullable=True),
            allow_stronger_not_null=True,
        )
        assert result["stronger_not_null"] is True
        result = ensure_compatible_column(
            op,
            "prospective_pair_evaluations",
            sa.Column("settlement_updated_at", sa.DateTime(timezone=True), nullable=True),
            allow_stronger_not_null=True,
        )
        assert result["action"] == "ADDED"
    engine.dispose()


@pytest.mark.parametrize("expression", [
    "length(payload)>0",
    "length(payload)>0 AND payload != 'CHECK(fake))'",
    "(length(payload)>0) /* CHECK(ignored) */",
])
def test_lane_check_preserves_unnamed_check_unique_index_and_rows(expression):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE canonical_evaluations (id INTEGER PRIMARY KEY, "
                f"source_lane TEXT NOT NULL, payload TEXT UNIQUE, CHECK({expression}))"
            )
        )
        connection.execute(
            sa.text("CREATE INDEX ix_lane_payload ON canonical_evaluations(payload)")
        )
        connection.execute(
            sa.text("INSERT INTO canonical_evaluations VALUES (1,'SHADOW','original')")
        )
        op = operations(connection)
        assert ensure_canonical_lane_check(op)["action"] == "ADDED_CHECK"
        assert ensure_canonical_lane_check(op)["action"] == "VERIFIED_EXISTING_CHECK"
        inspector = sa.inspect(connection)
        assert len(inspector.get_check_constraints("canonical_evaluations")) == 2
        assert len(inspector.get_unique_constraints("canonical_evaluations")) == 1
        assert inspector.get_indexes("canonical_evaluations")[0]["name"] == "ix_lane_payload"
        assert connection.execute(sa.text("SELECT * FROM canonical_evaluations")).all() == [
            (1, "SHADOW", "original")
        ]
    engine.dispose()


@pytest.mark.parametrize("wrong_check", [False, True])
def test_invalid_lane_or_conflicting_check_never_rewrites(wrong_check):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        check = f", CONSTRAINT {LANE_CHECK_NAME} CHECK(source_lane='OTHER')" if wrong_check else ""
        connection.execute(
            sa.text("CREATE TABLE canonical_evaluations (source_lane TEXT" + check + ")")
        )
        connection.execute(sa.text("INSERT INTO canonical_evaluations VALUES ('OTHER')"))
        before = connection.execute(
            sa.text("SELECT sql FROM sqlite_master WHERE name='canonical_evaluations'")
        ).scalar_one()
        with pytest.raises(ValueError, match="LANE"):
            ensure_canonical_lane_check(operations(connection))
        assert (
            connection.execute(
                sa.text("SELECT source_lane FROM canonical_evaluations")
            ).scalar_one()
            == "OTHER"
        )
        assert (
            connection.execute(
                sa.text("SELECT sql FROM sqlite_master WHERE name='canonical_evaluations'")
            ).scalar_one()
            == before
        )
    engine.dispose()


def test_literal_whitespace_is_not_semantic_equivalence():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        sql = LANE_CHECK_SQL.replace("HISTORICAL_REPLAY", "HISTORICAL_ REPLAY")
        connection.execute(
            sa.text(
                "CREATE TABLE canonical_evaluations (source_lane TEXT, "
                f"CONSTRAINT {LANE_CHECK_NAME} CHECK({sql}))"
            )
        )
        with pytest.raises(ValueError, match="CHECK_CONFLICT"):
            ensure_canonical_lane_check(operations(connection))
    engine.dispose()


def test_closed_connection_never_falls_back(monkeypatch):
    monkeypatch.setattr("kalshi_predictor.config.get_settings", forbidden)
    monkeypatch.setattr(sa, "engine_from_config", forbidden)
    engine = sa.create_engine("sqlite://")
    connection = engine.connect()
    connection.close()
    with pytest.raises(ValueError, match="EXPLICIT_OPEN"):
        command.upgrade(config(connection), "head")
    engine.dispose()
