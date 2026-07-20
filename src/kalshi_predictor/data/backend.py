from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.engine import URL, make_url

from kalshi_predictor.config import Settings, get_settings

SQLITE = "sqlite"
POSTGRES = "postgres"


def detect_backend(settings: Settings | None = None, db_url: str | None = None) -> str:
    if db_url:
        return _backend_from_url(db_url)
    resolved = settings or get_settings()
    if resolved.db_backend in {SQLITE, POSTGRES}:
        return resolved.db_backend
    return _backend_from_url(database_url_from_settings(resolved))


def is_sqlite(settings: Settings | None = None, db_url: str | None = None) -> bool:
    return detect_backend(settings, db_url) == SQLITE


def is_postgres(settings: Settings | None = None, db_url: str | None = None) -> bool:
    return detect_backend(settings, db_url) == POSTGRES


def database_url_from_settings(settings: Settings | None = None) -> str:
    resolved = settings or get_settings()
    configured_url = resolved.kalshi_db_url
    if resolved.db_backend == POSTGRES and _backend_from_url(configured_url) != POSTGRES:
        url = (
            URL.create(
                "postgresql+psycopg",
                username=resolved.postgres_user,
                password=resolved.postgres_password,
                host=resolved.postgres_host,
                port=resolved.postgres_port,
                database=resolved.postgres_db,
            )
        )
        return url.render_as_string(hide_password=False)
    return configured_url


def warn_if_sqlite_on_onedrive(
    settings: Settings | None = None,
    db_url: str | None = None,
) -> str | None:
    url = db_url or database_url_from_settings(settings)
    if not is_sqlite(db_url=url):
        return None
    path = sqlite_path_from_url(url)
    if path and _contains_onedrive(path):
        return (
            "SQLite on OneDrive is unsafe for overnight learning. Move to PostgreSQL "
            "or Linux-local path."
        )
    return None


def require_postgres_for_overnight_if_configured(settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    if resolved.require_postgres_for_overnight and not is_postgres(resolved):
        raise RuntimeError(
            "PostgreSQL is required for overnight runs. Set DB_BACKEND=postgres and "
            "DATABASE_URL to a PostgreSQL URL."
        )


def redact_database_url(db_url: str) -> str:
    url = make_url(db_url)
    if url.password is None:
        return str(url)
    return str(url.set(password="***"))


def sqlite_path_from_url(db_url: str) -> Path | None:
    url = make_url(db_url)
    if not url.drivername.startswith("sqlite") or not url.database:
        return None
    if url.database == ":memory:":
        return Path(":memory:")
    return Path(url.database).expanduser().resolve()


def backend_label(settings: Settings | None = None, db_url: str | None = None) -> str:
    return "PostgreSQL" if is_postgres(settings, db_url) else "SQLite"


def _backend_from_url(db_url: str) -> str:
    driver = make_url(db_url).drivername.lower()
    if driver.startswith("postgresql") or driver.startswith("postgres"):
        return POSTGRES
    return SQLITE


def _contains_onedrive(path: Path) -> bool:
    normalized = str(path).replace("\\", "/").lower()
    return "onedrive" in normalized or (
        "/mnt/c/users/" in normalized and "/onedrive/" in normalized
    )


def backend_config_payload(settings: Settings | None = None) -> dict[str, Any]:
    resolved = settings or get_settings()
    url = database_url_from_settings(resolved)
    return {
        "backend": detect_backend(resolved),
        "database_url": redact_database_url(url),
        "sqlite_on_onedrive_warning": warn_if_sqlite_on_onedrive(resolved),
        "require_postgres_for_overnight": resolved.require_postgres_for_overnight,
    }
