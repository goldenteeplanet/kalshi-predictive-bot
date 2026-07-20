import json
from pathlib import Path

import pytest

from kalshi_predictor.provenance.export_adapter import (
    SCHEMA_NORMALIZED_V3,
    SCHEMA_PROV2_ENVELOPE_V1,
    SCHEMA_RUNTIME_EVENT_V2,
    adapt_runtime_provenance_export,
    normalize_runtime_provenance_export,
)

FIXTURES = Path(__file__).parent / "fixtures" / "prov15c"


@pytest.mark.parametrize(
    ("name", "schema"),
    [
        ("v1.json", SCHEMA_PROV2_ENVELOPE_V1),
        ("v2.json", SCHEMA_RUNTIME_EVENT_V2),
        ("v3.json", SCHEMA_NORMALIZED_V3),
    ],
)
def test_prov15c_normalizes_all_supported_schema_versions(name, schema):
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    result = normalize_runtime_provenance_export(payload)
    assert result["source_schema"] == schema
    assert result["normalized_schema"] == SCHEMA_NORMALIZED_V3
    assert result["normalized_row_count"] == 1
    assert result["compatible"] is True
    assert result["rows"][0] == json.loads(
        (FIXTURES / "normalized_row.json").read_text(encoding="utf-8")
    )


def test_prov15c_reports_malformed_mixed_and_unsupported_inputs():
    mixed = json.loads((FIXTURES / "mixed_malformed.json").read_text(encoding="utf-8"))
    result = normalize_runtime_provenance_export(mixed)
    assert result["compatible"] is False
    assert [row["code"] for row in result["diagnostics"]] == [
        "MIXED_ROW_SCHEMAS", "MALFORMED_ROW"
    ]
    unsupported = normalize_runtime_provenance_export({
        "schema_version": "99", "rows": mixed["rows"][:1]
    })
    assert unsupported["diagnostics"][0]["code"] == "UNSUPPORTED_SCHEMA_VERSION"
    with pytest.raises(ValueError, match="MIXED_ROW_SCHEMAS"):
        adapt_runtime_provenance_export(mixed)


def test_prov15c_compatibility_result_is_deterministic():
    payload = json.loads((FIXTURES / "v2.json").read_text(encoding="utf-8"))
    first = normalize_runtime_provenance_export(payload)
    second = normalize_runtime_provenance_export(payload)
    assert first == second
