from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.data.repositories import insert_market_snapshot
from kalshi_predictor.kalshi.client import KalshiClient
from kalshi_predictor.kalshi.orderbook import LocalOrderbook, OrderbookSequenceGap
from kalshi_predictor.opportunities.market_identity import kalshi_api_market_url
from kalshi_predictor.utils.time import parse_datetime, utc_now

DEFAULT_WS_URL = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
WS_SIGNING_PATH = "/trade-api/ws/v2"


class WebSocketConnection(Protocol):
    async def send(self, message: str) -> None: ...

    def __aiter__(self) -> AsyncIterator[str]: ...


class WebSocketContext(Protocol):
    async def __aenter__(self) -> WebSocketConnection: ...

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None: ...


WebSocketConnector = Callable[..., WebSocketContext]
WriterMonitor = Callable[[], dict[str, Any]]


@dataclass(frozen=True)
class StreamSummary:
    messages_seen: int
    snapshots_seen: int
    deltas_applied: int
    sequence_recoveries: int
    staged_files: tuple[str, ...]
    errors: tuple[str, ...]
    timed_out: bool = False


class ReadOnlyOrderbookWebSocketAdapter:
    """Authenticated market-data stream with no order or portfolio operations."""

    def __init__(
        self,
        *,
        tickers: Sequence[str],
        auth_headers: Mapping[str, str],
        staging_dir: Path,
        rest_client: KalshiClient,
        connector: WebSocketConnector | None = None,
        ws_url: str = DEFAULT_WS_URL,
        persist_every_deltas: int = 25,
    ) -> None:
        self.tickers = tuple(dict.fromkeys(str(ticker).strip() for ticker in tickers if ticker))
        if not self.tickers:
            raise ValueError("At least one ticker is required.")
        self.auth_headers = _validated_auth_headers(auth_headers)
        self.staging_dir = staging_dir
        self.rest_client = rest_client
        self.connector = connector or _default_connector
        self.ws_url = ws_url
        self.persist_every_deltas = max(1, persist_every_deltas)
        self.books = {ticker: LocalOrderbook(ticker) for ticker in self.tickers}

    async def run(
        self, *, max_messages: int | None = None, max_seconds: float | None = None
    ) -> StreamSummary:
        messages_seen = snapshots_seen = deltas_applied = recoveries = 0
        staged: list[str] = []
        errors: list[str] = []
        timed_out = False
        deadline = asyncio.get_running_loop().time() + max_seconds if max_seconds else None
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        async with self.connector(
            self.ws_url,
            additional_headers=dict(self.auth_headers),
        ) as websocket:
            await websocket.send(json.dumps(self._subscribe_message(), sort_keys=True))
            iterator = websocket.__aiter__()
            while True:
                try:
                    if deadline is None:
                        raw_message = await anext(iterator)
                    else:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            timed_out = True
                            break
                        raw_message = await asyncio.wait_for(anext(iterator), timeout=remaining)
                except asyncio.TimeoutError:
                    timed_out = True
                    break
                except StopAsyncIteration:
                    break
                messages_seen += 1
                try:
                    message = json.loads(raw_message)
                    message_type = str(message.get("type") or "")
                    payload = message.get("msg") if isinstance(message.get("msg"), dict) else {}
                    ticker = str(payload.get("market_ticker") or "")
                    if message_type == "orderbook_snapshot" and ticker in self.books:
                        self.books[ticker].apply_snapshot(message)
                        snapshots_seen += 1
                        staged.append(str(self._stage(ticker, reason="websocket_snapshot")))
                    elif message_type == "orderbook_delta" and ticker in self.books:
                        try:
                            self.books[ticker].apply_delta(message)
                            deltas_applied += 1
                        except OrderbookSequenceGap as gap:
                            self._recover_from_gap(ticker, message, gap)
                            recoveries += 1
                            staged.append(str(self._stage(ticker, reason="sequence_gap_recovery")))
                        else:
                            if deltas_applied % self.persist_every_deltas == 0:
                                staged.append(str(self._stage(ticker, reason="delta_checkpoint")))
                    elif message_type == "error":
                        errors.append(str(payload.get("msg") or payload or "WebSocket error"))
                except Exception as exc:
                    errors.append(str(exc))
                if max_messages is not None and messages_seen >= max_messages:
                    break
        return StreamSummary(
            messages_seen=messages_seen,
            snapshots_seen=snapshots_seen,
            deltas_applied=deltas_applied,
            sequence_recoveries=recoveries,
            staged_files=tuple(staged),
            errors=tuple(errors),
            timed_out=timed_out,
        )

    def _subscribe_message(self) -> dict[str, Any]:
        return {
            "id": 1,
            "cmd": "subscribe",
            "params": {"channels": ["orderbook_delta"], "market_tickers": list(self.tickers)},
        }

    def _recover_from_gap(
        self,
        ticker: str,
        message: Mapping[str, Any],
        gap: OrderbookSequenceGap,
    ) -> None:
        # A current REST snapshot already includes the state represented by the
        # missing deltas, so the triggering delta must not be applied again.
        orderbook = self.rest_client.get_orderbook(ticker)
        self.books[ticker].apply_rest_snapshot(
            orderbook,
            resume_sequence=gap.actual,
            sid=_int_or_none(message.get("sid")),
        )

    def _stage(self, ticker: str, *, reason: str) -> Path:
        market = dict(self.rest_client.get_market(ticker))
        market["source"] = "kalshi_rest_market_snapshot"
        market["source_observed_at"] = utc_now().isoformat()
        market["kalshi_api_url"] = kalshi_api_market_url(ticker)
        book = self.books[ticker]
        payload = {
            "category": "websocket_orderbook_snapshot",
            "version": "gh1_v1",
            "staged_at": utc_now().isoformat(),
            "reason": reason,
            "ticker": ticker,
            "market": market,
            "orderbook": book.as_orderbook_json(),
            "safety": {
                "read_only_websocket": True,
                "filesystem_stage_only": True,
                "database_write": False,
                "execution_enabled": False,
                "orders_submitted": 0,
            },
        }
        safe_ticker = re.sub(r"[^A-Za-z0-9_.-]+", "_", ticker)
        sequence = book.sequence if book.sequence is not None else 0
        unique_suffix = time.time_ns()
        path = self.staging_dir / (
            f"{safe_ticker}_{sequence:020d}_{unique_suffix}_{reason}.json"
        )
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path


def websocket_auth_headers(*, key_id: str, private_key_path: Path) -> dict[str, str]:
    """Create Kalshi's authenticated handshake headers without exposing key material."""

    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise RuntimeError(
            "Install the cryptography dependency to sign WebSocket handshakes."
        ) from exc
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}GET{WS_SIGNING_PATH}".encode()
    private_key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("ascii"),
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }


def adapter_from_settings(
    *,
    settings: Settings,
    tickers: Sequence[str],
    rest_client: KalshiClient,
    connector: WebSocketConnector | None = None,
    persist_every_deltas: int = 25,
) -> ReadOnlyOrderbookWebSocketAdapter:
    if not settings.kalshi_websocket_enabled:
        raise RuntimeError(
            "GH-1 WebSocket ingestion is disabled by KALSHI_WEBSOCKET_ENABLED=false."
        )
    if not settings.kalshi_api_key_id or not settings.kalshi_private_key_path:
        raise RuntimeError("GH-1 requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH.")
    return ReadOnlyOrderbookWebSocketAdapter(
        tickers=tickers,
        auth_headers=websocket_auth_headers(
            key_id=settings.kalshi_api_key_id,
            private_key_path=Path(settings.kalshi_private_key_path),
        ),
        staging_dir=Path(settings.kalshi_websocket_staging_dir),
        rest_client=rest_client,
        connector=connector,
        ws_url=settings.kalshi_websocket_url,
        persist_every_deltas=persist_every_deltas,
    )


def drain_staged_websocket_orderbooks(
    *,
    session_factory: Callable[[], Session],
    staging_dir: Path,
    settings: Settings | None = None,
    writer_monitor_fn: WriterMonitor | None = None,
) -> dict[str, Any]:
    """Drain staged stream snapshots through one guarded database writer."""

    monitor = writer_monitor_fn or (lambda: db_writer_monitor(settings=settings))
    writer = monitor()
    if not bool(writer.get("safe_to_start_write", True)):
        return {
            "status": "BLOCKED_ACTIVE_WRITER",
            "files_seen": 0,
            "snapshots_inserted": 0,
            "writer_monitor": writer,
            "execution_enabled": False,
        }
    files = sorted(staging_dir.glob("*.json")) if staging_dir.exists() else []
    inserted = 0
    committed_files: list[Path] = []
    errors: list[str] = []
    with session_factory() as session:
        try:
            for path in files:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("category") != "websocket_orderbook_snapshot":
                    continue
                market = payload.get("market")
                orderbook = payload.get("orderbook")
                if not isinstance(market, dict) or not isinstance(orderbook, dict):
                    errors.append(f"{path}: missing market/orderbook objects")
                    continue
                insert_market_snapshot(
                    session,
                    market_json=market,
                    orderbook_json=orderbook,
                    captured_at=parse_datetime(payload.get("staged_at")) or utc_now(),
                )
                inserted += 1
                committed_files.append(path)
            session.commit()
        except Exception:
            session.rollback()
            raise
    archive_dir = staging_dir / "drained"
    archived_files: list[str] = []
    if committed_files:
        archive_dir.mkdir(parents=True, exist_ok=True)
        for path in committed_files:
            destination = archive_dir / path.name
            path.replace(destination)
            archived_files.append(str(destination))
    return {
        "status": "COMPLETE_WITH_ERRORS" if errors else "COMPLETE",
        "files_seen": len(files),
        "snapshots_inserted": inserted,
        "files_archived": len(archived_files),
        "archived_files": archived_files,
        "errors": errors,
        "writer_monitor": writer,
        "single_writer_session_count": 1,
        "execution_enabled": False,
        "orders_submitted": 0,
    }


def _validated_auth_headers(headers: Mapping[str, str]) -> dict[str, str]:
    required = {
        "KALSHI-ACCESS-KEY",
        "KALSHI-ACCESS-SIGNATURE",
        "KALSHI-ACCESS-TIMESTAMP",
    }
    normalized = {str(key).upper(): str(value) for key, value in headers.items() if value}
    missing = sorted(required - normalized.keys())
    if missing:
        raise ValueError(f"Missing WebSocket authentication headers: {', '.join(missing)}")
    return normalized


def _default_connector(*args: Any, **kwargs: Any) -> WebSocketContext:
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise RuntimeError("Install the websockets dependency to use the GH-1 adapter.") from exc
    return websockets.connect(*args, **kwargs)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
