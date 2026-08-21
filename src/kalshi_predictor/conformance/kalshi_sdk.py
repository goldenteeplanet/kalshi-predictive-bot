from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.kalshi.orderbook import LocalOrderbook

SDK_VERSION = "10.0.0"
SDK_SOURCE_COMMIT = "a5ef152a9e0a266ade2cf73cef950825fe0421c1"

_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "kalshi-access-key",
    "kalshi-access-signature",
    "kalshi-access-timestamp",
    "private_key",
    "private_key_path",
    "token",
}
_PRIVATE_PATH_PARTS = ("/portfolio", "/orders", "/account", "/subaccounts")


class ConformanceFixtureError(ValueError):
    """Raised when a fixture is not safe for public, read-only conformance tests."""


def assert_public_read_only_fixture(payload: Mapping[str, Any]) -> None:
    """Reject credentials, private endpoints, and mutating requests recursively."""
    for key, value in _walk(payload):
        normalized_key = key.lower().replace("_", "-")
        if normalized_key in _SENSITIVE_KEYS:
            raise ConformanceFixtureError(f"Sensitive fixture field is forbidden: {key}")
        if normalized_key == "method" and str(value).upper() not in {"GET", "HEAD"}:
            raise ConformanceFixtureError(f"Mutating fixture method is forbidden: {value}")
        if normalized_key in {"path", "url"}:
            path = str(value).lower()
            if any(part in path for part in _PRIVATE_PATH_PARTS):
                raise ConformanceFixtureError(f"Private fixture endpoint is forbidden: {value}")


def compare_orderbook_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Compare our local orderbook parser with kalshi-sdk's typed snapshot model."""
    assert_public_read_only_fixture(payload)
    snapshot_model = _snapshot_model()
    sdk_snapshot = snapshot_model.model_validate(payload)
    ticker = sdk_snapshot.msg.market_ticker
    local = LocalOrderbook(ticker)
    local.apply_snapshot(payload)
    sdk_yes = dict(sdk_snapshot.msg.yes)
    sdk_no = dict(sdk_snapshot.msg.no)
    if local.yes != sdk_yes or local.no != sdk_no:
        raise AssertionError("Local orderbook levels diverge from kalshi-sdk parsing.")
    return {
        "ticker": ticker,
        "sequence": sdk_snapshot.seq,
        "yes": _string_levels(sdk_yes),
        "no": _string_levels(sdk_no),
        "best_yes_bid": _string(local.best_yes_bid),
        "best_yes_ask": _string(local.best_yes_ask),
    }


def compare_orderbook_delta(
    snapshot: Mapping[str, Any],
    delta: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a delta with the SDK and compare its effect on our local book."""
    assert_public_read_only_fixture(snapshot)
    assert_public_read_only_fixture(delta)
    sdk_snapshot = _snapshot_model().model_validate(snapshot)
    sdk_delta = _delta_model().model_validate(delta)
    local = LocalOrderbook(sdk_snapshot.msg.market_ticker)
    local.apply_snapshot(snapshot)
    local.apply_delta(delta)
    side = sdk_delta.msg.side
    levels = local.yes if side == "yes" else local.no
    expected = sdk_snapshot.msg.yes if side == "yes" else sdk_snapshot.msg.no
    expected = dict(expected)
    price = Decimal(sdk_delta.msg.price)
    quantity = expected.get(price, Decimal(0)) + Decimal(sdk_delta.msg.delta)
    if quantity <= 0:
        expected.pop(price, None)
    else:
        expected[price] = quantity
    if levels != expected:
        raise AssertionError("Local delta application diverges from kalshi-sdk parsing.")
    return {
        "ticker": sdk_delta.msg.market_ticker,
        "sequence": sdk_delta.seq,
        "side": side,
        "price": str(price),
        "quantity_after": str(levels.get(price, Decimal(0))),
    }


def fixture_manifest(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ConformanceFixtureError("Conformance fixture root must be an object.")
    assert_public_read_only_fixture(payload)
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "sdk_version": SDK_VERSION,
        "sdk_source_commit": SDK_SOURCE_COMMIT,
        "access": "PUBLIC_READ_ONLY",
    }


def _snapshot_model() -> Any:
    try:
        from kalshi.ws.models.orderbook_delta import OrderbookSnapshotMessage
    except ImportError as exc:
        raise RuntimeError(
            "Install the optional conformance dependency with "
            "`pip install -e '.[conformance]'`; it is not a runtime dependency."
        ) from exc
    return OrderbookSnapshotMessage


def _delta_model() -> Any:
    try:
        from kalshi.ws.models.orderbook_delta import OrderbookDeltaMessage
    except ImportError as exc:
        raise RuntimeError(
            "Install the optional conformance dependency with "
            "`pip install -e '.[conformance]'`; it is not a runtime dependency."
        ) from exc
    return OrderbookDeltaMessage


def _walk(payload: Mapping[str, Any]) -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    for key, value in payload.items():
        rows.append((str(key), value))
        if isinstance(value, Mapping):
            rows.extend(_walk(value))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    rows.extend(_walk(item))
    return rows


def _string_levels(levels: Mapping[Decimal, Decimal]) -> list[list[str]]:
    return [[str(price), str(levels[price])] for price in sorted(levels, reverse=True)]


def _string(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
