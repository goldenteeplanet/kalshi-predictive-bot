"""Phase 4BC deterministic source-level build identity for guarded offline tooling."""

from __future__ import annotations

import argparse
import platform
import re
import sys
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCHEMA = "phase4bc.reproducible-build-identity.v1"
PROOF_SCHEMA = "phase4bc.build-reproducibility-proof.v1"
SCHEMA_PATTERN = re.compile(r"phase4[a-z]{2}\.[a-z0-9._-]+\.v[0-9]+")


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _files(root: Path, paths: list[Path], label: str) -> list[dict[str, Any]]:
    resolved_root = root.resolve(strict=True)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for supplied in sorted(paths, key=lambda item: item.as_posix()):
        path = supplied if supplied.is_absolute() else root / supplied
        if path.is_symlink():
            raise ValueError(f"PHASE4BC_{label}_SYMLINK_REFUSED")
        resolved = path.resolve(strict=True)
        try:
            relative = resolved.relative_to(resolved_root).as_posix()
        except ValueError as exc:
            raise ValueError(f"PHASE4BC_{label}_OUTSIDE_ROOT") from exc
        if relative in seen or not resolved.is_file():
            raise ValueError(f"PHASE4BC_{label}_DUPLICATE_OR_INVALID")
        seen.add(relative)
        content = resolved.read_bytes()
        rows.append(
            {"path": relative, "size": len(content), "sha256": canonical_hash(content.hex())}
        )
    if not rows:
        raise ValueError(f"PHASE4BC_{label}_MISSING")
    return rows


def build(
    root: Path,
    *,
    source_paths: list[Path],
    dependency_lock_paths: list[Path],
    test_paths: list[Path],
    build_options: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(build_options, dict) or any(
        not isinstance(key, str) or not isinstance(value, (str, int, bool))
        for key, value in build_options.items()
    ):
        raise ValueError("PHASE4BC_BUILD_OPTIONS_INVALID")
    sources = _files(root, source_paths, "SOURCE")
    locks = _files(root, dependency_lock_paths, "DEPENDENCY_LOCK")
    tests = _files(root, test_paths, "TEST")
    source_text = "\n".join((root / row["path"]).read_text(encoding="utf-8") for row in sources)
    schemas = sorted(set(SCHEMA_PATTERN.findall(source_text)))
    capabilities = {
        "production_executor_present": False,
        "sandbox_executor_present": "phase4at_sandbox_executor"
        in " ".join(row["path"] for row in sources),
        "service_control_present": "systemctl" in source_text,
        "exchange_interface_present": any(
            token in source_text for token in ("KalshiClient(", "create_order(", "requests.post(")
        ),
        "production_writer_lock_present": "writer_lock" in source_text,
        "production_path_literal_present": "/home/james/kalshi-runtime-src" in source_text,
    }
    if any(
        capabilities[field]
        for field in (
            "service_control_present",
            "exchange_interface_present",
            "production_writer_lock_present",
            "production_path_literal_present",
        )
    ):
        raise ValueError("PHASE4BC_PROHIBITED_BUILD_CAPABILITY")
    identity: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4BC",
        "source_files": sources,
        "source_tree_hash": canonical_hash(sources),
        "dependency_lock_files": locks,
        "dependency_lock_hash": canonical_hash(locks),
        "interpreter_identity": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "cache_tag": sys.implementation.cache_tag,
        },
        "platform_identity": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python_compiler": platform.python_compiler(),
        },
        "build_options": dict(sorted(build_options.items())),
        "test_files": tests,
        "test_suite_identity": canonical_hash(tests),
        "artifact_schema_versions": schemas,
        "artifact_schema_versions_hash": canonical_hash(schemas),
        "safety_capability_manifest": capabilities,
        "binary_published": False,
        "deployed": False,
        "execution_authorized": False,
    }
    identity["artifact_hash"] = _hash(identity)
    proof: dict[str, Any] = {
        "schema": PROOF_SCHEMA,
        "phase": "4BC",
        "build_identity_hash": identity["artifact_hash"],
        "deterministic_inputs_hash": canonical_hash(
            {
                "sources": sources,
                "locks": locks,
                "tests": tests,
                "options": identity["build_options"],
                "interpreter": identity["interpreter_identity"],
                "platform": identity["platform_identity"],
            }
        ),
        "source_level_identity_only": True,
        "binary_published": False,
        "deployed": False,
        "production_database_mutated": False,
        "execution_authorized": False,
    }
    proof["artifact_hash"] = _hash(proof)
    return identity, proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, action="append", required=True)
    parser.add_argument("--dependency-lock", type=Path, action="append", required=True)
    parser.add_argument("--test", type=Path, action="append", required=True)
    parser.add_argument("--build-option", action="append", default=[])
    parser.add_argument("--identity-output", type=Path, required=True)
    parser.add_argument("--proof-output", type=Path, required=True)
    args = parser.parse_args()
    options: dict[str, str] = {}
    for option in args.build_option:
        if "=" not in option:
            raise ValueError("PHASE4BC_BUILD_OPTION_INVALID")
        key, value = option.split("=", 1)
        if not key or key in options:
            raise ValueError("PHASE4BC_BUILD_OPTION_INVALID")
        options[key] = value
    identity, proof = build(
        args.repository_root,
        source_paths=args.source,
        dependency_lock_paths=args.dependency_lock,
        test_paths=args.test,
        build_options=options,
    )
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.identity_output, args.proof_output, identity, proof)
    print(identity["artifact_hash"], proof["artifact_hash"])


if __name__ == "__main__":
    main()
