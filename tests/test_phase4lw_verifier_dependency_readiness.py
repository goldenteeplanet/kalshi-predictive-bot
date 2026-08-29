from __future__ import annotations

import copy

import pytest

from scripts.local.phase4lw_verifier_dependency_readiness import evaluate_readiness, make_component


def _component(scope="FIXTURE"):
    artifact = "a" * 64
    return make_component(
        name="cryptography",
        version="50.0.0",
        artifact_type="wheel",
        artifact_sha256=artifact,
        license="APACHE-2.0 OR BSD-3-CLAUSE",
        source_provenance_sha256="b" * 64,
        build_attestation_sha256="c" * 64,
        vulnerability_scan={
            "scanned_at": "2026-08-28T18:00:00Z",
            "status": "PASS",
            "scanner_sha256": "d" * 64,
        },
        algorithms=["ECDSA-P256-SHA256", "ED25519", "RSA-PSS-SHA256"],
        runtime={"python_abi": "cp312", "platform_tag": "manylinux_2_34_x86_64"},
        offline_install={"verified": True, "artifact_sha256": artifact, "source_only": False},
        capabilities={
            "key_generation": False,
            "private_key_loading": False,
            "network_trust": False,
            "os_certificate_store": False,
            "service_control": False,
            "trading": False,
        },
        attestation_scope=scope,
    )


def _evaluate(component=None, at="2026-08-28T20:00:00Z", platform="manylinux_2_34_x86_64"):
    return evaluate_readiness(
        [component or _component()],
        evaluated_at=at,
        required_platform_tag=platform,
        required_python_abi="cp312",
    )


def _rehash(component):
    from scripts.local.phase4lw_verifier_dependency_readiness import _digest

    component["component_sha256"] = _digest(
        {key: value for key, value in component.items() if key != "component_sha256"}
    )


def test_fixture_is_deterministic_pass_but_production_refuses() -> None:
    first = _evaluate()
    assert first == _evaluate()
    assert first["fixture_readiness"] == "PASS"
    assert first["production_readiness"] == "REFUSE"


def test_self_declared_independent_attestation_still_refuses_production() -> None:
    result = _evaluate(_component("INDEPENDENT_PRODUCTION"))
    assert result["fixture_readiness"] == "PASS"
    assert result["production_readiness"] == "REFUSE"
    assert "INDEPENDENT_ATTESTATION_AUTHENTICITY_GATE_REQUIRED" in result["production_reasons"]


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("version", "latest", "VERSION_MUTABLE_OR_INVALID"),
        ("artifact_type", "sdist", "ARTIFACT_TYPE_INVALID"),
        ("artifact_sha256", "bad", "HASH_INVALID:artifact_sha256"),
        ("license", "UNKNOWN", "LICENSE_NOT_REVIEWED"),
        ("algorithms", ["RSA-SHA1"], "ALGORITHM_SET_INVALID"),
    ],
)
def test_mutable_source_only_hash_license_and_algorithm_failures(field, value, error: str) -> None:
    component = _component()
    component[field] = value
    _rehash(component)
    assert error in _evaluate(component)["components"][0]["errors"]


def test_stale_failing_and_future_scans_refuse() -> None:
    for scanned_at, status in (
        ("2026-08-01T00:00:00Z", "PASS"),
        ("2026-08-29T00:00:00Z", "PASS"),
        ("2026-08-28T18:00:00Z", "FAIL"),
    ):
        component = _component()
        component["vulnerability_scan"].update(scanned_at=scanned_at, status=status)
        _rehash(component)
        assert _evaluate(component)["verdict"] == "REFUSE"


def test_platform_mismatch_offline_hash_and_source_only_refuse() -> None:
    assert _evaluate(platform="win_amd64")["verdict"] == "REFUSE"
    component = _component()
    component["offline_install"].update(artifact_sha256="f" * 64, source_only=True)
    _rehash(component)
    errors = _evaluate(component)["components"][0]["errors"]
    assert "OFFLINE_WHEEL_NOT_VERIFIED" in errors
    assert "OFFLINE_ARTIFACT_HASH_MISMATCH" in errors


@pytest.mark.parametrize(
    "capability",
    [
        "key_generation",
        "private_key_loading",
        "network_trust",
        "os_certificate_store",
        "service_control",
        "trading",
    ],
)
def test_every_forbidden_capability_refuses(capability: str) -> None:
    component = _component()
    component["capabilities"][capability] = True
    _rehash(component)
    assert "FORBIDDEN_CAPABILITY_PRESENT" in _evaluate(component)["components"][0]["errors"]


def test_duplicate_dependency_confusion_and_unknown_package_refuse() -> None:
    component = _component()
    duplicate = copy.deepcopy(component)
    result = evaluate_readiness(
        [component, duplicate],
        evaluated_at="2026-08-28T20:00:00Z",
        required_platform_tag="manylinux_2_34_x86_64",
        required_python_abi="cp312",
    )
    assert any("DUPLICATE_PACKAGE" in error for error in result["errors"])
    unknown = _component()
    unknown["name"] = "crypt0graphy"
    _rehash(unknown)
    assert "NAME_NOT_ALLOWLISTED" in _evaluate(unknown)["components"][0]["errors"]


def test_component_tampering_and_missing_exact_set_refuse() -> None:
    component = _component()
    component["version"] = "49.0.0"
    assert "COMPONENT_HASH_MISMATCH" in _evaluate(component)["components"][0]["errors"]
    result = evaluate_readiness(
        [],
        evaluated_at="2026-08-28T20:00:00Z",
        required_platform_tag="manylinux_2_34_x86_64",
        required_python_abi="cp312",
    )
    assert result["verdict"] == "REFUSE"


def test_gate_has_no_install_network_key_or_action_capability() -> None:
    safety = _evaluate()["safety"]
    assert safety["validation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "validation_only")
