from __future__ import annotations

import copy
import math
from decimal import Decimal

from scripts.local.phase4nk_portable_replay import (
    canonicalize_artifact,
    prove_portable_replay,
)
from tests.test_phase4mz_adversarial_backtest import _records


def _platform_variants():
    linux = _records()
    windows = copy.deepcopy(linux)
    for row in windows:
        for field in ("decision_time", "quote_time", "settlement_time"):
            row[field] = row[field].replace("Z", "+00:00")
        row["feature_times"] = [value.replace("Z", "+00:00") for value in row["feature_times"]]
    linux[0]["ticker"] = "Caf\u00e9\nRain"
    windows[0]["ticker"] = "Cafe\u0301\r\nRain"
    return linux, windows


def test_platform_variants_have_identical_bytes_hashes_outputs_and_verdicts() -> None:
    linux, windows = _platform_variants()
    proof = prove_portable_replay(linux, windows, seed=1729)
    assert proof["verdict"] == "PASS"
    assert proof["canonical_sha256"]
    assert proof["bundle_sha256"]
    assert proof["replay_outputs_sha256"]


def test_decimals_unicode_line_endings_and_timestamps_are_canonical() -> None:
    left = {"caf\u00e9": Decimal("1.2300"), "text": "a\r\nb", "time": "2026-08-01T07:00:00-05:00"}
    right = {"cafe\u0301": "1.23", "text": "a\nb", "time": "2026-08-01T12:00:00Z"}
    first, second = canonicalize_artifact(left), canonicalize_artifact(right)
    assert first["verdict"] == second["verdict"] == "PASS"
    assert first["canonical_hex"] == second["canonical_hex"]
    assert first["canonical_sha256"] == second["canonical_sha256"]


def test_timezone_ambiguity_and_nonfinite_numbers_refuse() -> None:
    assert "TIMEZONE_AMBIGUOUS" in canonicalize_artifact("2026-08-01T12:00:00")["errors"]
    for value in (math.nan, math.inf, Decimal("NaN")):
        assert "NON_FINITE_NUMBER" in canonicalize_artifact(value)["errors"]


def test_duplicate_normalized_keys_and_locale_numbers_refuse() -> None:
    duplicate = {"caf\u00e9": 1, "cafe\u0301": 2}
    assert "DUPLICATE_NORMALIZED_KEY" in canonicalize_artifact(duplicate)["errors"]
    assert "LOCALE_DEPENDENT_NUMBER" in canonicalize_artifact("1,25")["errors"]


def test_unsupported_encoding_path_leakage_and_platform_metadata_refuse() -> None:
    assert "UNSUPPORTED_ENCODING" in canonicalize_artifact(b"\xff")["errors"]
    for path in (r"C:\\Users\\user\\secret", "/home/user/secret"):
        assert "PATH_LEAKAGE_DETECTED" in canonicalize_artifact(path)["errors"]
    assert "PLATFORM_METADATA_PRESENT" in canonicalize_artifact({"hostname": "worker-1"})["errors"]


def test_canonicalization_version_drift_and_representation_difference_refuse() -> None:
    assert "CANONICALIZATION_VERSION_DRIFT" in canonicalize_artifact({}, version="v2")["errors"]
    proof = prove_portable_replay(_records(), _records()[:-1])
    assert proof["verdict"] == "REFUSE"
    assert "CANONICAL_BYTES_MISMATCH" in proof["errors"]


def test_portability_layer_has_no_execution_capability() -> None:
    safety = canonicalize_artifact({})["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
