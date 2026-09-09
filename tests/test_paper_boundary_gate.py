from pathlib import Path

import pytest

from kalshi_predictor.overnight_paper.boundary_gate import (
    audit_local_call_path,
    verify_coordinator_boundary,
)


def tree(tmp_path: Path, **modules: str) -> Path:
    package = tmp_path / "src" / "kalshi_predictor"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    for name, source in modules.items():
        (package / (name + ".py")).write_text(source)
    return tmp_path


def audit(root: Path):
    return audit_local_call_path(root, entrypoints=(("kalshi_predictor.entry", "run"),))


def test_local_data_call_chain_passes(tmp_path):
    root = tree(
        tmp_path,
        entry=(
            "from kalshi_predictor.helpers import size as calculate\n"
            "def run(value):\n"
            " return calculate(value)\n"
        ),
        helpers="def size(value):\n return max(0, int(value))\n",
    )
    result = audit(root)
    assert result.passed, result.blockers
    assert "kalshi_predictor.helpers:size" in result.visited_callables
    assert len(result.source_hashes) == 3


def test_unused_network_function_is_not_a_called_capability(tmp_path):
    root = tree(
        tmp_path,
        entry="from kalshi_predictor.helpers import size\ndef run(value):\n return size(value)\n",
        helpers=(
            "import httpx\n"
            "def size(value):\n"
            " return int(value)\n"
            "def unused():\n"
            " return httpx.post('https://example.test/orders')\n"
        ),
    )
    assert audit(root).passed


@pytest.mark.parametrize(
    "body",
    [
        "import httpx as transport\ndef run(value):\n return transport.post('https://example.test/orders')\n",
        "def run(client):\n return client.cancel_order('one')\n",
        "def run(client):\n return client.replace_order('one')\n",
        "def run(client):\n return client.mutate_portfolio()\n",
        "def run(callback):\n return callback()\n",
        "def run(*, callback):\n return callback()\n",
        "def run(client):\n method = getattr(client, 'post')\n return method('url')\n",
        "import os\ndef run(value):\n return os.startfile('program.exe')\n",
        "def run(client):\n return getattr(client, 'post')('url')\n",
        "def run(value):\n return __import__('httpx').post('url')\n",
        "import subprocess as process\ndef run(value):\n return process.run(['curl', 'url'])\n",
        "from mystery_sdk import Client\ndef run(value):\n return Client()\n",
    ],
)
def test_unsafe_capabilities_fail_closed(tmp_path, body):
    assert not audit(tree(tmp_path, entry=body)).passed


def test_transitive_aliased_network_call_fails(tmp_path):
    root = tree(
        tmp_path,
        entry=(
            "from kalshi_predictor.helpers import send as write\n"
            "def run(value):\n"
            " return write(value)\n"
        ),
        helpers="import httpx\ndef send(value):\n return httpx.post('url', json=value)\n",
    )
    assert any("NETWORK" in reason for reason in audit(root).blockers)


def test_network_callback_cannot_hide_behind_local_alias(tmp_path):
    root = tree(
        tmp_path,
        entry=(
            "import httpx\n"
            "def send(value):\n"
            " return httpx.post('url')\n"
            "def run(values):\n"
            " alias = send\n"
            " return list(map(alias, values))\n"
        ),
    )
    assert not audit(root).passed


def test_module_initializer_is_a_reachable_capability(tmp_path):
    root = tree(
        tmp_path,
        entry="from kalshi_predictor.helpers import size\ndef run(value):\n return size(value)\n",
        helpers="import httpx\nhttpx.post('url')\ndef size(value):\n return value\n",
    )
    assert not audit(root).passed


def test_imported_default_function_not_executed_on_import(tmp_path):
    root = tree(
        tmp_path,
        entry="from kalshi_predictor.helpers import size\ndef run(value):\n return size(value)\n",
        helpers=(
            "import httpx\n"
            "def fetch():\n"
            " return httpx.post('url')\n"
            "def unused(callback=fetch):\n"
            " return callback()\n"
            "def size(value):\n"
            " return value\n"
        ),
    )
    assert audit(root).passed


def test_source_modification_changes_evidence_and_blocks(tmp_path):
    root = tree(tmp_path, entry="def run(value):\n return value\n")
    before = audit(root)
    (root / "src/kalshi_predictor/entry.py").write_text(
        "def run(client):\n return client.cancel_order('id')\n"
    )
    after = audit(root)
    assert before.passed and not after.passed
    assert before.source_hashes != after.source_hashes


def test_production_gate_cannot_select_a_fixture_entrypoint(tmp_path):
    result = verify_coordinator_boundary(
        repository=tmp_path, code_sha="0" * 40, settings={}, coordinator_symbol="harmless_fixture"
    )
    assert not result.passed
    assert "UNREVIEWED_COORDINATOR_ENTRYPOINT" in result.blockers


def test_class_method_decorator_executes_during_import(tmp_path):
    root = tree(
        tmp_path,
        entry=(
            "import httpx\nclass Unused:\n @httpx.post('url')\n"
            " def method(self):\n  return 1\ndef run(value):\n return value\n"
        ),
    )
    assert not audit(root).passed


def test_methods_of_typed_project_argument_are_reviewed(tmp_path):
    root = tree(
        tmp_path,
        entry=(
            "from kalshi_predictor.helpers import Client\n"
            "def run(client: Client):\n return client.send()\n"
        ),
        helpers=("import httpx\nclass Client:\n def send(self):\n  return httpx.post('url')\n"),
    )
    assert not audit(root).passed


def test_missing_fee_module_pin_fails_before_runtime_verification(tmp_path, monkeypatch):
    from kalshi_predictor.overnight_paper import provenance

    manifest = dict(provenance.AUDITED_BOUNDARY_SHA256)
    assert "kalshi_predictor.paper.fees" in manifest
    manifest.pop("kalshi_predictor.paper.fees")
    monkeypatch.setattr(provenance, "AUDITED_BOUNDARY_SHA256", manifest)

    def unexpected_runtime_call(**kwargs):
        raise AssertionError("Incomplete fee boundary must fail before Git/runtime verification")

    monkeypatch.setattr(provenance, "verify_local_boundary", unexpected_runtime_call)
    result = verify_coordinator_boundary(repository=tmp_path, code_sha="0" * 40, settings={})
    assert not result.passed
    assert result.blockers == ("COORDINATOR_AUDIT_MANIFEST_INCOMPLETE",)
