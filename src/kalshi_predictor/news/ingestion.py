import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.news.providers import (
    configured_rss_feeds,
    fetch_rss_feed,
    parse_rss_feed,
)
from kalshi_predictor.news.repository import upsert_news_item


@dataclass(frozen=True)
class NewsIngestionSummary:
    source: str
    items_seen: int = 0
    items_inserted: int = 0
    duplicates_skipped: int = 0
    feeds_attempted: int = 0
    feeds_succeeded: int = 0
    failed_feeds: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    message: str = ""


def ingest_news_file(session: Session, input_file: str | Path) -> NewsIngestionSummary:
    path = Path(input_file)
    if not path.exists():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        items = _load_json_items(path)
    elif suffix == ".csv":
        items = _load_csv_items(path)
    else:
        raise ValueError("News input file must be .json or .csv.")
    return ingest_news_items(session, items, source=f"file:{path.name}")


def ingest_news_rss(
    session: Session,
    *,
    settings: Settings | None = None,
) -> NewsIngestionSummary:
    resolved = settings or get_settings()
    feeds, config_errors = configured_rss_feeds(resolved)
    if not feeds:
        return NewsIngestionSummary(source="rss", errors=config_errors, message=config_errors[0])

    all_items: list[dict[str, Any]] = []
    failed: list[str] = []
    for feed in feeds:
        try:
            xml_text = fetch_rss_feed(feed, resolved)
            items = parse_rss_feed(
                xml_text,
                source_name=feed.name,
                category=feed.category,
                source_url=feed.url,
                limit=resolved.news_max_items_per_feed,
            )
            all_items.extend(items)
        except Exception as exc:  # noqa: BLE001 - feed failures should be non-fatal.
            failed.append(f"{feed.name}: {exc}")

    summary = ingest_news_items(session, all_items, source="rss")
    return NewsIngestionSummary(
        source="rss",
        items_seen=summary.items_seen,
        items_inserted=summary.items_inserted,
        duplicates_skipped=summary.duplicates_skipped,
        feeds_attempted=len(feeds),
        feeds_succeeded=len(feeds) - len(failed),
        failed_feeds=failed,
        errors=[*config_errors, *failed],
        message=(
            "RSS ingestion completed."
            if all_items or failed
            else "No RSS items were returned by configured feeds."
        ),
    )


def ingest_news_items(
    session: Session,
    items: list[dict[str, Any]],
    *,
    source: str = "manual",
) -> NewsIngestionSummary:
    inserted = 0
    duplicates = 0
    errors: list[str] = []
    for item in items:
        try:
            _, created = upsert_news_item(session, item)
        except Exception as exc:  # noqa: BLE001 - bad rows should not stop the batch.
            errors.append(f"{item.get('title', 'untitled')}: {exc}")
            continue
        if created:
            inserted += 1
        else:
            duplicates += 1
    return NewsIngestionSummary(
        source=source,
        items_seen=len(items),
        items_inserted=inserted,
        duplicates_skipped=duplicates,
        errors=errors,
        message="News ingestion completed.",
    )


def _load_json_items(path: Path) -> list[dict[str, Any]]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(decoded, list):
        return [dict(item) for item in decoded if isinstance(item, dict)]
    if isinstance(decoded, dict):
        for key in ("items", "news", "news_items"):
            value = decoded.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, dict)]
        return [decoded]
    raise ValueError("News JSON must be an object, a list, or an object with an items list.")


def _load_csv_items(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader]
