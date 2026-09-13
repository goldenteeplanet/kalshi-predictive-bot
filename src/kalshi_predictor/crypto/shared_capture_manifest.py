"""Read a bounded opt-in candle manifest; no network or external path access."""

from datetime import datetime
from pathlib import Path

from kalshi_predictor.crypto.doge_range_evidence import DogeOriginal, DogeRangeProof, strict_json
from kalshi_predictor.crypto.shared_capture import CandleOriginal, SharedCryptoInputs
from kalshi_predictor.forecasting.crypto_v3_independent import CryptoTarget


def load_crypto_manifest(path: Path) -> SharedCryptoInputs:
    path = path.resolve()
    if path.stat().st_size > 128_000:
        raise ValueError("MANIFEST_SIZE_CAP")

    value = strict_json(path.read_bytes())
    if not isinstance(value, dict) or set(value) not in (
        {"schema", "target", "originals"},
        {"schema", "target", "originals", "doge_range_proof"},
    ):
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
    proof = None
    if "doge_range_proof" in value:
        spec = value["doge_range_proof"]
        if type(spec) is not dict or set(spec) != {"series", "terms"}:
            raise ValueError("EXACT_DOGE_PROOF_MANIFEST_REQUIRED")
        evidence = []
        for role in ("series", "terms"):
            item = spec[role]
            if type(item) is not dict or set(item) != {"path", "receipt_path"}:
                raise ValueError("EXACT_DOGE_ORIGINAL_PATHS_REQUIRED")
            blobs = []
            for name in ("path", "receipt_path"):
                relative = Path(item[name])
                source = (path.parent / relative).resolve()
                if relative.is_absolute() or not source.is_relative_to(path.parent):
                    raise ValueError("ORIGINAL_OUTSIDE_MANIFEST_DIRECTORY")
                if not source.is_file() or not 0 < source.stat().st_size <= 1_000_000:
                    raise ValueError("ORIGINAL_FILE_SIZE_CAP")
                blobs.append(source.read_bytes())
            evidence.append(DogeOriginal(*blobs))
        proof = DogeRangeProof(*evidence)
    return SharedCryptoInputs(CryptoTarget(**target), tuple(parsed), proof)
