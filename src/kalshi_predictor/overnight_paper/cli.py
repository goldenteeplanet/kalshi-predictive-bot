"""Public evidence commands; no paper activation is implied by a candidate scan."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer


def register_commands(app: typer.Typer) -> None:
    @app.command("paper-settlement-cycles")
    def paper_settlement_cycles(
        database: Annotated[Path, typer.Option(help="Existing isolated sprint database.")],
        cycles: Annotated[int, typer.Option(min=1, max=60)] = 1,
        interval_seconds: Annotated[int, typer.Option(min=60, max=60)] = 60,
    ) -> None:
        """Monitor public results and reconcile existing local paper/shadow positions."""
        import sqlite3
        from dataclasses import asdict

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from kalshi_predictor.overnight_paper.settlement_runner import run_settlement_cycles

        path = database.absolute()
        # mode=rw cannot silently create a fresh ledger on a mistyped path.
        engine = create_engine(
            "sqlite://",
            creator=lambda: sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=0),
        )
        try:
            result = run_settlement_cycles(
                session_factory=sessionmaker(engine),
                database_path=path,
                cycles=cycles,
                interval_seconds=interval_seconds,
            )
            typer.echo(json.dumps(asdict(result), indent=2, default=str))
        finally:
            engine.dispose()

    @app.command("fast-paper-candidates")
    def fast_paper_candidates(
        archive_root: Annotated[Path, typer.Option(help="New isolated raw-evidence directory.")],
        max_pages: Annotated[int, typer.Option(min=1, max=100)] = 40,
        max_book_requests: Annotated[int, typer.Option(min=1, max=100)] = 24,
        resume_from: Annotated[
            Path | None, typer.Option(help="Verified census archive to resume.")
        ] = None,
    ) -> None:
        """Discover fast public markets and report candidate-specific blockers; no orders."""
        from kalshi_predictor.overnight_paper.discovery import run_discovery

        result = run_discovery(
            archive_root,
            max_pages=max_pages,
            max_book_requests=max_book_requests,
            resume_from=resume_from,
        )
        typer.echo(
            json.dumps(
                {key: value for key, value in result.items() if key != "rows"},
                indent=2,
                default=str,
            )
        )

    @app.command("paper-observation-cycles")
    def paper_observation_cycles(
        database: Annotated[Path, typer.Option(help="Initialized isolated sprint database.")],
        archive_root: Annotated[Path, typer.Option(help="Isolated public evidence directory.")],
        run_id: Annotated[str, typer.Option(help="Checkpoint identity for safe resume.")],
        cycles: Annotated[int, typer.Option(min=1, max=24)] = 1,
        interval_seconds: Annotated[int, typer.Option(min=60, max=3600)] = 60,
        max_pages: Annotated[int, typer.Option(min=1, max=100)] = 100,
        max_book_requests: Annotated[int, typer.Option(min=1, max=100)] = 24,
    ) -> None:
        """Bounded public research capture; never activates paper or qualifies a soak."""
        from kalshi_predictor.overnight_paper.runner import run_observation_cycles

        result = run_observation_cycles(
            database,
            archive_root,
            run_id=run_id,
            cycles=cycles,
            interval_seconds=interval_seconds,
            max_pages=max_pages,
            max_book_requests=max_book_requests,
        )
        typer.echo(json.dumps(result, indent=2))
