from __future__ import annotations

import json

import pytest

from scripts.local.phase4ml_recovery_handoff_package import build_package, verify_package


def _inputs():
    inventory = [
        {"path": f"path-{index}", "content_sha256": f"{index:064x}"} for index in range(75)
    ]
    return {
        "certification": {"verdict": "PASS", "certification_sha256": "a" * 64},
        "reproducibility_audit": {"verdict": "PASS", "audit_sha256": "b" * 64},
        "test_evidence": {"verdict": "PASS", "evidence_sha256": "c" * 64},
        "inventory": inventory,
        "schemas": ["phase4lk.runtime-config.v1", "phase4mi.audit.v1"],
        "residual_risks": ["production execution absent"],
        "verification_instructions": ["verify hashes offline", "do not execute repairs"],
        "audience": "dejoia.offline.recovery-reviewer",
        "issued_at": "2026-08-28T22:00:00Z",
        "expires_at": "2026-09-04T22:00:00Z",
        "certified_range_head": "1" * 40,
        "descendant_checkout_head": "2" * 40,
    }


def _package():
    return build_package(**_inputs())


def _verify(package=None, **kwargs):
    return verify_package(
        package or _package(),
        expected_audience=kwargs.get("audience", "dejoia.offline.recovery-reviewer"),
        evaluated_at=kwargs.get("evaluated_at", "2026-08-29T00:00:00Z"),
    )


def _recanonicalize(package):
    manifest = package["manifest"]
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    manifest["manifest_sha256"] = (
        __import__("hashlib")
        .sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
        .hexdigest()
    )
    return json.dumps(package, sort_keys=True, separators=(",", ":")).encode()


def test_package_is_byte_reproducible_and_verifies_offline() -> None:
    first = _package()
    assert first == _package()
    result = _verify(first)
    assert result["verdict"] == "PASS"
    assert result["component_count"] == 7
    assert len(result["extraction_plan"]) == 7
    assert all(row["write_performed"] is False for row in result["extraction_plan"])


@pytest.mark.parametrize(
    "mutation,error",
    [
        (lambda value: value["components"].pop(), "COMPONENT_ORDER_DUPLICATION_OR_OMISSION"),
        (
            lambda value: value["components"].append(value["components"][0]),
            "COMPONENT_ORDER_DUPLICATION_OR_OMISSION",
        ),
        (
            lambda value: value["components"].reverse(),
            "COMPONENT_0_SUBSTITUTED_OR_REORDERED",
        ),
        (
            lambda value: value["components"][0].update(payload={"verdict": "REFUSE"}),
            "COMPONENT_0_SUBSTITUTED_OR_REORDERED",
        ),
        (
            lambda value: value["components"][0].update(path="../escape.json"),
            "COMPONENT_0_PATH_UNSAFE",
        ),
    ],
)
def test_missing_duplicate_reordered_substituted_and_traversal_refuse(mutation, error: str) -> None:
    decoded = json.loads(_package())
    mutation(decoded)
    result = _verify(_recanonicalize(decoded))
    assert result["verdict"] == "REFUSE"
    assert error in result["errors"]


def test_truncation_noncanonical_and_oversized_input_refuse() -> None:
    assert _verify(_package()[:-10])["verdict"] == "REFUSE"
    decoded = json.loads(_package())
    noncanonical = json.dumps(decoded, indent=2).encode()
    assert "PACKAGE_NOT_CANONICAL" in _verify(noncanonical)["errors"]
    oversized = b"x" * 4_000_001
    assert "PACKAGE_TYPE_OR_SIZE_INVALID" in _verify(oversized)["errors"]


def test_audience_expiry_and_not_yet_valid_refuse() -> None:
    assert "AUDIENCE_MISMATCH" in _verify(audience="wrong.audience")["errors"]
    assert "PACKAGE_EXPIRED" in _verify(evaluated_at="2026-09-04T22:00:00Z")["errors"]
    assert "PACKAGE_NOT_YET_VALID" in _verify(evaluated_at="2026-08-28T21:59:59Z")["errors"]


def test_builder_rejects_secret_like_material_and_bad_lifetime() -> None:
    inputs = _inputs()
    inputs["certification"] = {"verdict": "PASS", "api_key": "forbidden"}
    with pytest.raises(ValueError, match="secret"):
        build_package(**inputs)
    inputs = _inputs()
    inputs["expires_at"] = "2026-09-05T22:00:00Z"
    with pytest.raises(ValueError, match="lifetime"):
        build_package(**inputs)


def test_verifier_has_no_write_execution_or_order_capability() -> None:
    safety = _verify()["safety"]
    assert safety["offline"] is True
    assert safety["verification_only"] is True
    assert all(
        value is False
        for key, value in safety.items()
        if key not in {"offline", "verification_only"}
    )
