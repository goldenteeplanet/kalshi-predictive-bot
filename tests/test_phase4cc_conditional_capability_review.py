from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4cc_conditional_capability_review.py"
    spec = importlib.util.spec_from_file_location("phase4cc_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _evidence(module):
    status = {
        "Coinbase Exchange": {"CURSOR": "SUPPORTED"},
        "Kalshi": {"CURSOR": "SUPPORTED"},
        "NWS": {},
    }
    urls = {
        "Coinbase Exchange": "https://docs.cdp.coinbase.com/exchange/rest-api/pagination",
        "Kalshi": "https://docs.kalshi.com/getting_started/pagination",
        "NWS": "https://www.weather.gov/documentation/services-web-api",
    }
    providers = []
    for provider in sorted(status):
        capabilities = []
        for mechanism in module.MECHANISMS:
            row_status = status[provider].get(mechanism, "UNDOCUMENTED")
            capabilities.append(
                {
                    "mechanism": mechanism,
                    "status": row_status,
                    "authoritative_url": urls[provider],
                    "evidence_hash": module.canonical_hash([provider, mechanism, row_status]),
                }
            )
        providers.append({"provider": provider, "capabilities": capabilities})
    evidence = {"schema": module.INPUT_SCHEMA, "providers": providers}
    evidence["artifact_hash"] = module._hash(evidence)
    return evidence


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_proposal_is_deterministic_conservative_and_non_authorizing():
    module = _module()
    report = module.build_proposal(_evidence(module))
    assert report == module.build_proposal(_evidence(module))
    assert report["supported_candidates"] == [
        {"provider": "Coinbase Exchange", "mechanism": "CURSOR"},
        {"provider": "Kalshi", "mechanism": "CURSOR"},
    ]
    assert report["unknown_support_is_disabled"] is True
    assert report["collector_changes_applied"] == 0
    assert report["execution_authorized"] is False


def test_undocumented_and_unsupported_capabilities_are_not_candidates():
    module = _module()
    evidence = _evidence(module)
    evidence["providers"][0]["capabilities"][0]["status"] = "UNSUPPORTED"
    _rehash(module, evidence)
    report = module.build_proposal(evidence)
    actions = [
        row["proposal_action"]
        for provider in report["providers"]
        for row in provider["capabilities"]
        if row["status"] != "SUPPORTED"
    ]
    assert set(actions) == {"DO_NOT_IMPLEMENT"}


@pytest.mark.parametrize(
    "kind", ("provider_order", "duplicate", "coverage", "status", "url", "hash", "fields")
)
def test_malformed_evidence_fails_closed(kind: str):
    module = _module()
    evidence = _evidence(module)
    if kind == "provider_order":
        evidence["providers"].reverse()
    elif kind == "duplicate":
        evidence["providers"][1]["provider"] = evidence["providers"][0]["provider"]
    elif kind == "coverage":
        evidence["providers"][0]["capabilities"].pop()
    elif kind == "status":
        evidence["providers"][0]["capabilities"][0]["status"] = "ASSUMED"
    elif kind == "url":
        evidence["providers"][0]["capabilities"][0]["authoritative_url"] = "http://bad"
    elif kind == "hash":
        evidence["providers"][0]["capabilities"][0]["evidence_hash"] = "bad"
    else:
        evidence["providers"][0]["extra"] = True
    _rehash(module, evidence)
    with pytest.raises(ValueError):
        module.build_proposal(evidence)


def test_outer_tampering_fails_closed():
    module = _module()
    evidence = _evidence(module)
    evidence["extra"] = True
    with pytest.raises(ValueError, match="HASH"):
        module.build_proposal(evidence)


def test_atomic_publication_round_trip(tmp_path: Path):
    module = _module()
    report = module.build_proposal(_evidence(module))
    output = tmp_path / "proposal.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_database_service_network_or_exchange_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4cc_conditional_capability_review.py"
    ).read_text()
    for token in (
        "sqlite3",
        "subprocess",
        "requests",
        "systemctl",
        "exchange_client",
        "/home/james",
    ):
        assert token not in source
