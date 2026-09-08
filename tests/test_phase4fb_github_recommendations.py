from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCRIPT = Path(__file__).parents[1] / "scripts/local/phase4fb_github_recommendations.py"
SPEC = importlib.util.spec_from_file_location("phase4fb", SCRIPT)
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def seal(v):
    v.pop("artifact_hash", None)
    v["artifact_hash"] = canonical_hash(v)
    return v


def payload():
    rows = []
    for ident in reversed(M.REQUIRED):
        rows.append(
            {
                "id": ident,
                "availability": "UNKNOWN" if ident == "openai_codex_github" else "AVAILABLE",
                "purpose": "Reduce feedback latency safely.",
                "required_permissions": ["contents:read"],
                "scope": "READ_ONLY",
                "data_leaving_repository": ["repository metadata"],
                "secret_requirements": ["none"],
                "cost_plan_uncertainty": "Verify account plan before approval.",
                "security_risks": ["third-party processing"],
                "reversibility": "Disable and revoke the integration.",
                "recommendation": "VERIFY_BEFORE_APPROVAL"
                if ident == "openai_codex_github"
                else "OPTIONAL_DEFER",
                "source_urls": ["https://docs.github.com/"],
                "explicit_approval_required": True,
            }
        )
    return seal(
        {
            "schema": M.INPUT_SCHEMA,
            "repository": "owner/repo",
            "inventory_hash": "a" * 64,
            "recommendations": rows,
        }
    )


def test_complete_report_is_ordered_and_hash_protected():
    r = M.build_report(payload())
    assert [x["id"] for x in r["recommendations"]] == list(M.REQUIRED)
    assert r["artifact_hash"] == canonical_hash(
        {k: v for k, v in r.items() if k != "artifact_hash"}
    )


@pytest.mark.parametrize("ident", M.REQUIRED)
def test_each_required_integration_is_present(ident):
    assert ident in {x["id"] for x in M.build_report(payload())["recommendations"]}


@pytest.mark.parametrize("field", M.FIELDS)
def test_missing_recommendation_field_fails_closed(field):
    v = payload()
    v["recommendations"][0].pop(field)
    seal(v)
    with pytest.raises(ValueError):
        M.build_report(v)


def test_missing_integration_fails_closed():
    v = payload()
    v["recommendations"].pop()
    seal(v)
    with pytest.raises(ValueError, match="INTEGRATION_SET"):
        M.build_report(v)


def test_duplicate_integration_fails_closed():
    v = payload()
    v["recommendations"][-1] = dict(v["recommendations"][0])
    seal(v)
    with pytest.raises(ValueError, match="INTEGRATION_SET"):
        M.build_report(v)


def test_approval_cannot_be_disabled():
    v = payload()
    v["recommendations"][0]["explicit_approval_required"] = False
    seal(v)
    with pytest.raises(ValueError, match="APPROVAL_GATE"):
        M.build_report(v)


def test_input_tampering_fails_closed():
    v = payload()
    v["repository"] = "attacker/repo"
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        M.build_report(v)


@pytest.mark.parametrize(
    "field",
    [
        "integration_changes_made",
        "apps_installed",
        "apps_authorized",
        "repository_settings_changed",
        "secrets_created_or_read",
        "production_database_mutated",
        "services_controlled",
        "exchange_requests_made",
        "execution_authorized",
    ],
)
def test_report_attests_no_mutation(field):
    assert M.build_report(payload())[field] is False


def test_atomic_publication(tmp_path):
    out = tmp_path / "report.json"
    r = M.build_report(payload())
    M.publish(out, r)
    assert json.loads(out.read_text()) == r and not list(tmp_path.glob(".*"))


def test_no_connected_write_surface():
    s = SCRIPT.read_text()
    for token in ("requests.", "urllib.", "subprocess", "gh api", "/home/james"):
        assert token not in s
