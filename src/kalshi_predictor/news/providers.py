import json
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree

import httpx

from kalshi_predictor.config import Settings, get_settings


@dataclass(frozen=True)
class NewsFeedConfig:
    name: str
    url: str
    category: str | None = None


def configured_rss_feeds(
    settings: Settings | None = None,
) -> tuple[list[NewsFeedConfig], list[str]]:
    resolved = settings or get_settings()
    raw = resolved.news_rss_feeds_json.strip()
    if not raw:
        return [], [
            "NEWS_RSS_FEEDS_JSON is empty. Add a JSON list of feed objects to ingest RSS news."
        ]
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], [f"NEWS_RSS_FEEDS_JSON could not be parsed: {exc}"]
    if not isinstance(decoded, list):
        return [], ["NEWS_RSS_FEEDS_JSON must be a JSON list."]

    feeds: list[NewsFeedConfig] = []
    errors: list[str] = []
    for item in decoded:
        if not isinstance(item, dict):
            errors.append("Skipped a feed entry because it was not an object.")
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        if not name or not url:
            errors.append("Skipped a feed entry missing name or url.")
            continue
        feeds.append(
            NewsFeedConfig(
                name=name,
                url=url,
                category=str(item.get("category") or "").strip().lower() or None,
            )
        )
    return feeds, errors


def fetch_rss_feed(feed: NewsFeedConfig, settings: Settings | None = None) -> str:
    resolved = settings or get_settings()
    response = httpx.get(
        feed.url,
        headers={"User-Agent": resolved.news_user_agent},
        timeout=15.0,
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.text


def parse_rss_feed(
    xml_text: str,
    *,
    source_name: str,
    category: str | None = None,
    source_url: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    root = ElementTree.fromstring(xml_text)
    channel_items = [_item_payload(item) for item in root.findall(".//item")]
    if channel_items:
        return _with_source(channel_items[:limit], source_name, category, source_url)

    atom_entries = [
        _atom_payload(entry)
        for entry in root.iter()
        if _tag_name(entry.tag) == "entry"
    ]
    return _with_source(atom_entries[:limit], source_name, category, source_url)


def _with_source(
    items: list[dict[str, Any]],
    source_name: str,
    category: str | None,
    source_url: str | None,
) -> list[dict[str, Any]]:
    output = []
    for item in items:
        enriched = {
            "source": source_name,
            "source_url": item.get("source_url") or source_url,
            **item,
            "raw_json": {"provider": "rss", "feed_url": source_url, **item},
        }
        if category and not enriched.get("category"):
            enriched["category"] = category
        output.append(enriched)
    return output


def _item_payload(item: ElementTree.Element) -> dict[str, Any]:
    payload = {
        "title": _child_text(item, "title"),
        "summary": _child_text(item, "description"),
        "body": _child_text(item, "content:encoded") or _child_text(item, "encoded"),
        "source_url": _child_text(item, "link"),
        "published_at": _parse_rss_datetime(_child_text(item, "pubDate")),
        "author": _child_text(item, "author") or _child_text(item, "creator"),
    }
    return {key: value for key, value in payload.items() if value not in {None, ""}}


def _atom_payload(entry: ElementTree.Element) -> dict[str, Any]:
    link = None
    for child in entry:
        if _tag_name(child.tag) == "link":
            link = child.attrib.get("href") or child.text
            break
    payload = {
        "title": _child_text(entry, "title"),
        "summary": _child_text(entry, "summary"),
        "body": _child_text(entry, "content"),
        "source_url": link,
        "published_at": _child_text(entry, "published") or _child_text(entry, "updated"),
        "author": _atom_author(entry),
    }
    return {key: value for key, value in payload.items() if value not in {None, ""}}


def _child_text(parent: ElementTree.Element, name: str) -> str | None:
    wanted = name.split(":")[-1]
    for child in parent.iter():
        if child is parent:
            continue
        if _tag_name(child.tag) == wanted and child.text:
            return child.text.strip()
    return None


def _atom_author(entry: ElementTree.Element) -> str | None:
    for child in entry.iter():
        if _tag_name(child.tag) == "name" and child.text:
            return child.text.strip()
    return None


def _parse_rss_datetime(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return value
    return parsed.isoformat()


def _tag_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":", 1)[-1]
