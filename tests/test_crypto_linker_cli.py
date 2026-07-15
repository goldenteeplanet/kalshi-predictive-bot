import json

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market


def test_link_crypto_markets_cli_writes_heartbeat_and_checkpoint(tmp_path) -> None:
    get_settings.cache_clear()
    runner = CliRunner()
    db_path = tmp_path / "crypto_link_cli.db"
    heartbeat_dir = tmp_path / "crypto_link"
    engine = init_db(f"sqlite:///{db_path}")
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        upsert_market(session, {"ticker": "BTC-CLI-LINK", "title": "Will BTC exceed 70000?"})
        session.commit()

    result = runner.invoke(
        app,
        [
            "link-crypto-markets",
            "--limit",
            "1",
            "--progress-every",
            "1",
            "--checkpoint-every",
            "1",
            "--heartbeat-dir",
            str(heartbeat_dir),
        ],
        env={"KALSHI_DB_URL": f"sqlite:///{db_path}"},
    )

    get_settings.cache_clear()
    assert result.exit_code == 0
    assert "Markets processed: 1" in result.output
    assert (heartbeat_dir / "link_crypto_markets_heartbeat.json").exists()
    assert (heartbeat_dir / "link_crypto_markets_checkpoint.json").exists()
    summary = json.loads((heartbeat_dir / "link_crypto_markets_summary.json").read_text())
    assert summary["processed"] == 1
    assert summary["final"] is True
