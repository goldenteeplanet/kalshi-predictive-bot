"""Read a bounded opt-in candle manifest; no network or external path access."""

import json
from datetime import datetime
from pathlib import Path

from kalshi_predictor.crypto.shared_capture import CandleOriginal, SharedCryptoInputs
from kalshi_predictor.forecasting.crypto_v3_independent import CryptoTarget


def load_crypto_manifest(path: Path) -> SharedCryptoInputs:
    path = path.resolve()
    if path.stat().st_size > 128_000:
        raise ValueError("MANIFEST_SIZE_CAP")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("DUPLICATE_MANIFEST_KEY")
            result[key] = value
        return result

    value = json.loads(path.read_bytes(), object_pairs_hook=pairs)
    if not isinstance(value, dict) or set(value) != {"schema", "target", "originals"}:
        raise ValueError("EXACT_CRYPTO_MANIFEST_REQUIRED")
    if value["schema"] != "shared-crypto-input-manifest-v1":
        raise ValueError("UNSUPPORTED_CRYPTO_MANIFEST")
    target = dict(value["target"])
    target["observation_at"] = datetime.fromisoformat(target["observation_at"])
    originals = value["originals"]
    if not isinstance(originals, list) or not 1 <= len(originals) <= 4:
        raise ValueError("BOUNDED_CANDLE_ORIGINALS_REQUIRED")
    parsed = []
    for row in originals:
        if not isinstance(row, dict) or set(row) != {
            "path",
            "sha256",
            "url",
            "received_at",
            "status",
        }:
            raise ValueError("EXACT_CANDLE_RECEIPT_REQUIRED")
        relative = Path(row["path"])
        source = (path.parent / relative).resolve()
        if relative.is_absolute() or not source.is_relative_to(path.parent):
            raise ValueError("ORIGINAL_OUTSIDE_MANIFEST_DIRECTORY")
        if not source.is_file() or source.stat().st_size > 1_000_000:
            raise ValueError("ORIGINAL_FILE_SIZE_CAP")
        parsed.append(
            CandleOriginal(
                source.read_bytes(),
                row["sha256"],
                row["url"],
                datetime.fromisoformat(row["received_at"]),
                row["status"],
            )
        )
    return SharedCryptoInputs(CryptoTarget(**target), tuple(parsed))
