from __future__ import annotations

import json

import pytest

from scripts.local.phase4lg_bounded_evidence_parser import Limits, parse_evidence


def _errors(document, limits=None):
    return parse_evidence(document, limits or Limits())[1]["errors"]


def test_valid_evidence_passes_deterministically() -> None:
    document = json.dumps({"schema": "example.v1", "owned_paths": ["docs/safe.md"]})
    first = parse_evidence(document)
    second = parse_evidence(document.encode())
    assert first == second
    assert first[1]["verdict"] == "PASS"


def test_document_byte_boundary() -> None:
    limits = Limits(document_bytes=16)
    assert parse_evidence(" " * 16, limits)[1]["document_bytes"] == 16
    assert "DOCUMENT_SIZE_EXCEEDED" in _errors(" " * 17, limits)


def test_duplicate_keys_and_trailing_data_refuse() -> None:
    assert any(error.startswith("DUPLICATE_KEY") for error in _errors('{"a":1,"a":2}'))
    assert "MALFORMED_JSON" in _errors('{"a":1} trailing')


def test_depth_and_collection_bounds() -> None:
    assert "NESTING_DEPTH_EXCEEDED" in _errors("[[[0]]]", Limits(nesting_depth=2))
    assert "COLLECTION_SIZE_EXCEEDED" in _errors("[1,2,3]", Limits(collection_items=2))


def test_string_and_scalar_bounds() -> None:
    assert "UNSAFE_OR_OVERSIZED_STRING" in _errors('"abcd"', Limits(string_characters=3))
    assert "SCALAR_COUNT_EXCEEDED" in _errors("[1,2,3]", Limits(scalar_count=2))


@pytest.mark.parametrize("document", ["1e100", "123456", "NaN", "Infinity"])
def test_numeric_abuse_refuses(document: str) -> None:
    limits = Limits(numeric_token_characters=5, numeric_absolute_max=10)
    assert _errors(document, limits)


def test_malformed_utf8_and_unicode_controls_refuse() -> None:
    assert "MALFORMED_UTF8" in _errors(b'"\xff"')
    assert "UNSAFE_OR_OVERSIZED_STRING" in _errors('"line\\nfeed"')
    assert "UNSAFE_OR_OVERSIZED_STRING" in _errors('"bidi\\u202etxt"')


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a\\\\b", "a/./b"])
def test_path_confusion_refuses(path: str) -> None:
    document = json.dumps({"owned_paths": [path]})
    assert "UNSAFE_PATH" in _errors(document)


def test_specialized_collection_limits() -> None:
    receipts = json.dumps({"receipts": [{}, {}]})
    assert "RECEIPT_COUNT_EXCEEDED" in _errors(receipts, Limits(receipt_count=1))
    extensions = json.dumps({"extensions": {"a.x": 1, "b.x": 2}})
    assert "EXTENSION_COUNT_EXCEEDED" in _errors(extensions, Limits(extension_count=1))


@pytest.mark.parametrize(
    "capability",
    ["database_write", "service-control", "writer_lock", "order_capability"],
)
def test_capability_bearing_extensions_refuse(capability: str) -> None:
    document = json.dumps({"extensions": {"vendor.capabilities": {capability: True}}})
    assert "FORBIDDEN_EXTENSION_CAPABILITY" in _errors(document)


def test_maximum_document_read_is_bounded_in_cli_contract() -> None:
    assert Limits().document_bytes == 65_536
    assert Limits().nesting_depth == 32
    assert Limits().scalar_count == 5_000
