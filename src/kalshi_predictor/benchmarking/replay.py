from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from kalshi_predictor.kalshi.orderbook import LocalOrderbook
from kalshi_predictor.utils.time import parse_datetime


@dataclass(frozen=True)
class ReplayEvent:
    timestamp: datetime
    ticker: str
    kind: str
    message: dict[str, Any]


@dataclass(frozen=True)
class SyntheticEpisode:
    episode_id: str
    category: str
    events: tuple[ReplayEvent, ...]
    settlements: dict[str, str]


@dataclass(frozen=True)
class ReplayFrame:
    timestamp: datetime
    ticker: str
    sequence: int | None
    orderbook: dict[str, Any]


def load_synthetic_episode(payload: dict[str, Any]) -> SyntheticEpisode:
    events = []
    for row in payload.get("events", []):
        timestamp = parse_datetime(row.get("timestamp"))
        ticker = str(row.get("ticker") or "")
        kind = str(row.get("kind") or "")
        message = row.get("message")
        if timestamp is None or not ticker or kind not in {"snapshot", "delta"}:
            raise ValueError("Synthetic replay event is missing exact timestamp/ticker/kind")
        if not isinstance(message, dict):
            raise ValueError("Synthetic replay event message must be an object")
        events.append(ReplayEvent(timestamp, ticker, kind, message))
    events.sort(key=lambda event: (event.timestamp, event.ticker, event.message.get("seq", -1)))
    return SyntheticEpisode(
        episode_id=str(payload.get("episode_id") or "synthetic"),
        category=str(payload.get("category") or "unknown"),
        events=tuple(events),
        settlements={str(k): str(v).lower() for k, v in payload.get("settlements", {}).items()},
    )


def replay_episode(episode: SyntheticEpisode) -> list[ReplayFrame]:
    books: dict[str, LocalOrderbook] = {}
    frames = []
    for event in episode.events:
        book = books.setdefault(event.ticker, LocalOrderbook(event.ticker))
        if event.kind == "snapshot":
            book.apply_snapshot(event.message)
        else:
            book.apply_delta(event.message)
        frames.append(ReplayFrame(
            timestamp=event.timestamp, ticker=event.ticker, sequence=book.sequence,
            orderbook=book.as_orderbook_json(),
        ))
    return frames


def replay_digest(frames: list[ReplayFrame]) -> str:
    payload = [{
        "timestamp": frame.timestamp.isoformat(), "ticker": frame.ticker,
        "sequence": frame.sequence, "orderbook": frame.orderbook,
    } for frame in frames]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
