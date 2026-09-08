"""Callable-scoped local-paper capability audit.

This is a trusted-process source audit, not a Python sandbox. Imported unused
functions are not called capabilities. Module initializers ARE included. Standard
library data operations, SQLAlchemy's SQLite Session and validated model/data
objects are trusted. Arbitrary caller-supplied executable objects are outside this
proof and must never be passed into the production coordinator.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import subprocess
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kalshi_predictor.overnight_paper.provenance import Verification
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BoundaryAudit:
    passed: bool
    blockers: tuple[str, ...]
    source_hashes: tuple[tuple[str, str], ...]
    visited_callables: tuple[str, ...]
    scope: str = "TRUSTED_PROCESS_LOCAL_CALL_GRAPH_V1"


_NETWORK = frozenset(
    {
        "httpx",
        "requests",
        "socket",
        "aiohttp",
        "urllib.request",
        "urllib.error",
        "ftplib",
        "smtplib",
        "websocket",
        "websockets",
    }
)
_DYNAMIC = frozenset({"eval", "exec", "compile", "__import__", "setattr", "delattr"})
_SENSITIVE = frozenset(
    {
        "post",
        "put",
        "patch",
        "delete",
        "request",
        "urlopen",
        "create_order",
        "submit_order",
        "place_order",
        "cancel_order",
        "cancel_orders",
        "replace_order",
        "amend_order",
        "batch_create_orders",
        "transfer",
        "withdraw",
        "deposit",
        "set_balance",
        "mutate_portfolio",
    }
)
_FORBIDDEN_MODULE_PARTS = frozenset(
    {"execution", "execution_gateway", "private_client", "authenticated_client", "live_trading"}
)


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return (base or "<data>") + "." + node.attr
    return None


def audit_local_call_path(
    repository: Path,
    *,
    entrypoints: tuple[tuple[str, str], ...],
) -> BoundaryAudit:
    """Compute evidence from code, never from a caller's verdict or hash list.

    Entry points are explicit for analysis/testing. Production registration must
    supply the fixed coordinator entry point, not an input-selected function.
    Conservative unresolved project symbols fail closed. HTTP imports alone do
    not fail; constructing/calling an HTTP capability or initializer does.
    """
    root = repository.resolve() / "src"
    blockers: set[str] = set()
    trees: dict[str, ast.Module] = {}
    hashes: dict[str, str] = {}
    aliases: dict[str, dict[str, str]] = {}
    definitions: dict[str, dict[str, ast.AST]] = {}
    visited: set[tuple[str, str]] = set()
    pending = list(entrypoints)

    def fail(message: str, module: str, node: ast.AST | None = None) -> None:
        blockers.add(f"{message}:{module}:{getattr(node, 'lineno', 0)}")

    def load(module: str) -> bool:
        if module in trees:
            return True
        path = root.joinpath(*module.split(".")).with_suffix(".py")
        package = False
        if not path.is_file():
            path = root.joinpath(*module.split("."), "__init__.py")
            package = True
        if not path.is_file() or not path.resolve().is_relative_to(root):
            fail("UNRESOLVED_PROJECT_MODULE", module)
            return False
        raw = path.read_bytes().replace(b"\r\n", b"\n")
        tree = ast.parse(raw, filename=str(path))
        trees[module] = tree
        hashes[module] = hashlib.sha256(raw).hexdigest()
        aliases[module] = {}
        definitions[module] = {}
        node: ast.AST
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                definitions[module][node.name] = node
        # Function-local imports are resolved too. Conflicting aliases fail closed.
        for imported_node in ast.walk(tree):
            node = imported_node
            pairs: list[tuple[str, str]] = []
            if isinstance(node, ast.Import):
                pairs = [
                    (a.asname or a.name.split(".")[0], a.name if a.asname else a.name.split(".")[0])
                    for a in node.names
                ]
            elif isinstance(node, ast.ImportFrom):
                origin = node.module or ""
                if node.level:
                    base = module if package else module.rpartition(".")[0]
                    origin = importlib.util.resolve_name("." * node.level + origin, base)
                pairs = [(a.asname or a.name, origin + "." + a.name) for a in node.names]
                if origin.startswith("kalshi_predictor"):
                    pending.append((origin, "<module>"))
            for short, full in pairs:
                if short == "*":
                    fail("STAR_IMPORT_UNRESOLVED", module, node)
                previous = aliases[module].get(short)
                if previous is not None and previous != full:
                    fail("AMBIGUOUS_IMPORT_ALIAS", module, node)
                aliases[module][short] = full
                if isinstance(node, ast.Import) and full.startswith("kalshi_predictor"):
                    pending.append((full, "<module>"))
        parts = module.split(".")
        for size in range(1, len(parts)):
            pending.append((".".join(parts[:size]), "<module>"))
        pending.append((module, "<module>"))
        return True

    def resolve_call(module: str, name: str, node: ast.Call) -> None:
        head, _, tail = name.partition(".")
        full = aliases[module].get(head, head) + ("." + tail if tail else "")
        if head in _DYNAMIC or full.startswith(("importlib.", "runpy.", "ctypes.")):
            fail("DYNAMIC_EXECUTION_CAPABILITY", module, node)
            return
        if any(full == item or full.startswith(item + ".") for item in _NETWORK):
            fail("NETWORK_CALL_CAPABILITY=" + full, module, node)
            return
        if full.startswith(
            (
                "os.system",
                "os.popen",
                "os.exec",
                "os.spawn",
                "os.startfile",
                "subprocess.",
                "asyncio.create_subprocess",
                "multiprocessing.",
                "concurrent.futures.ProcessPoolExecutor",
                "pickle.loads",
                "marshal.loads",
            )
        ):
            # Existing exact-SHA checks run only git reads; they are source-pinned
            # separately by verify_local_boundary. Every other process call fails.
            if module not in {
                "kalshi_predictor.overnight_paper.activation",
                "kalshi_predictor.overnight_paper.provenance",
            }:
                fail("PROCESS_EXECUTION_CAPABILITY=" + full, module, node)
            return
        if any(part in _FORBIDDEN_MODULE_PARTS for part in full.split(".")):
            fail("EXCHANGE_EXECUTION_CAPABILITY=" + full, module, node)
            return
        terminal = name.rsplit(".", 1)[-1]
        if terminal in _SENSITIVE:
            # SQLAlchemy delete is a local expression, not an HTTP DELETE.
            if full != "sqlalchemy.delete":
                fail("SENSITIVE_CALL_CAPABILITY=" + full, module, node)
                return
        if head in aliases[module] and not full.startswith("kalshi_predictor."):
            dependency = full.split(".")[0]
            if dependency not in sys.stdlib_module_names | {
                "sqlalchemy",
                "pydantic",
                "pydantic_settings",
                "rich",
            }:
                fail("UNREVIEWED_EXTERNAL_CAPABILITY=" + full, module, node)
                return
        if head in definitions[module]:
            pending.append((module, head))
        elif full.startswith("kalshi_predictor."):
            # Resolve imported classes/functions before their chained methods.
            parts = full.split(".")
            found = False
            for size in range(len(parts) - 1, 0, -1):
                target = ".".join(parts[:size])
                path = root.joinpath(*parts[:size]).with_suffix(".py")
                if path.is_file() and load(target):
                    symbol = parts[size]
                    if symbol in definitions[target]:
                        pending.append((target, symbol))
                        found = True
                        break
            if not found:
                fail("UNRESOLVED_PROJECT_CALL=" + full, module, node)

    def scan(
        module: str, node: ast.AST, parameters: set[str], follow_references: bool = True
    ) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            # Nested functions become reachable only through actual named calls;
            # their bodies are conservatively scanned when the enclosing callable
            # is reachable, including callbacks supplied to trusted libraries.
            parameters = parameters | {
                arg.arg
                for arg in (*node.args.args, *node.args.posonlyargs, *node.args.kwonlyargs)
                if arg.arg != "cls"
            }
            for arg in (*node.args.args, *node.args.posonlyargs, *node.args.kwonlyargs):
                if arg.annotation is not None:
                    for reference in ast.walk(arg.annotation):
                        if isinstance(reference, ast.Name) and (
                            reference.id in definitions[module]
                            or aliases[module].get(reference.id, "").startswith("kalshi_predictor.")
                        ):
                            scan(module, reference, parameters)
            for child in (*node.body, *node.decorator_list, *node.args.defaults):
                scan(module, child, parameters)
            return
        if follow_references and isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            # Also follow functions passed as callbacks or assigned to aliases.
            if node.id in definitions[module]:
                pending.append((module, node.id))
            full = aliases[module].get(node.id, "")
            if full.startswith("kalshi_predictor."):
                target, _, symbol = full.rpartition(".")
                if load(target) and symbol in definitions[target]:
                    pending.append((target, symbol))
            if any(full == item or full.startswith(item + ".") for item in _NETWORK):
                fail("NETWORK_CAPABILITY_REFERENCE=" + full, module, node)
        if isinstance(node, ast.Attribute) and node.attr in {
            "__subclasses__",
            "__globals__",
            "__builtins__",
            "__code__",
        }:
            fail("REFLECTIVE_EXECUTION_CAPABILITY", module, node)
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name == "getattr" and len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                if node.args[1].value in _SENSITIVE:
                    fail("REFLECTIVE_SENSITIVE_CAPABILITY", module, node)
            reviewed_path_probe = (module, ast.unparse(node)) in {
                (
                    "kalshi_predictor.overnight_paper.activation",
                    "getattr(item, 'is_junction', lambda: False)()",
                ),
                (
                    "kalshi_predictor.overnight_paper.coordinator",
                    "getattr(p, 'is_junction', lambda: False)()",
                ),
            }
            if (
                name is None
                or (
                    isinstance(node.func, ast.Call)
                    and name in {"getattr", "globals", "locals", "vars"}
                )
            ) and not reviewed_path_probe:
                fail("DYNAMIC_CALL_TARGET", module, node)
            elif name in parameters:
                # Reviewed ORM capabilities. Production pins these source files;
                # the coordinator owns the concrete SQLite sessionmaker and model
                # constructors are selected internally by memory repository code.
                if (module, name) not in {
                    ("kalshi_predictor.overnight_paper.activation", "session_factory"),
                    ("kalshi_predictor.overnight_paper.coordinator", "session_factory"),
                    ("kalshi_predictor.memory.repository", "model"),
                }:
                    fail("CALLER_SUPPLIED_CALLABLE=" + name, module, node)
            elif name is not None:
                resolve_call(module, name, node)
        for child in ast.iter_child_nodes(node):
            scan(module, child, parameters, follow_references)

    try:
        if not entrypoints:
            raise ValueError("ENTRYPOINT_REQUIRED")
        while pending:
            module, symbol = pending.pop()
            if (module, symbol) in visited:
                continue
            visited.add((module, symbol))
            if not module.startswith("kalshi_predictor") or not load(module):
                fail("INVALID_LOCAL_ENTRYPOINT", module)
                continue
            if symbol == "<module>":
                for node in trees[module].body:
                    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                        # Definitions execute decorators/default expressions, not bodies.
                        for decorator in node.decorator_list:
                            scan(module, decorator, set())
                        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                            for default in (*node.args.defaults, *node.args.kw_defaults):
                                if default is not None:
                                    scan(module, default, set(), False)
                        else:
                            for base in node.bases:
                                scan(module, base, set())
                            for keyword in node.keywords:
                                scan(module, keyword.value, set())
                            for item in node.body:
                                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                                    for decorator in item.decorator_list:
                                        scan(module, decorator, set())
                                    for default in (*item.args.defaults, *item.args.kw_defaults):
                                        if default is not None:
                                            scan(module, default, set(), False)
                                else:
                                    scan(module, item, set())
                        continue
                    scan(module, node, set())
            elif module in {
                "kalshi_predictor.overnight_paper.boundary_gate",
                "kalshi_predictor.overnight_paper.provenance",
            }:
                # Runtime verifier separately checks these audit implementations
                # against the committed reviewed manifest before trusting them.
                continue
            elif symbol in definitions[module]:
                scan(module, definitions[module][symbol], set())
            else:
                fail("ENTRYPOINT_SYMBOL_MISSING=" + symbol, module)
    except (OSError, SyntaxError, ValueError, ImportError) as exc:
        blockers.add("BOUNDARY_AUDIT_ERROR:" + str(exc))
    return BoundaryAudit(
        not blockers,
        tuple(sorted(blockers)),
        tuple(sorted(hashes.items())),
        tuple(sorted(m + ":" + s for m, s in visited)),
    )


def verify_coordinator_boundary(
    *,
    repository: Path,
    code_sha: str,
    settings: dict[str, Any],
    coordinator_symbol: str = "admit_prepared_candidate",
) -> Verification:
    """Production gate: pinned runtime plus fixed local coordinator call graph.

    A SQLite ledger must be the only writer capability supplied by the caller.
    This check does not grant authority to arbitrary Python objects or plugins.
    """
    from kalshi_predictor.overnight_paper.provenance import (
        AUDITED_BOUNDARY_SHA256,
        Verification,
        verify_local_boundary,
    )

    scope = "REVIEWED_LOCAL_COORDINATOR_RUNTIME_BOUNDARY"
    if coordinator_symbol != "admit_prepared_candidate":
        return Verification(False, ("UNREVIEWED_COORDINATOR_ENTRYPOINT",), scope=scope)
    required = {
        "kalshi_predictor.overnight_paper.coordinator",
        "kalshi_predictor.overnight_paper.boundary_gate",
        "kalshi_predictor.memory.repository",
    }
    if not required.issubset(AUDITED_BOUNDARY_SHA256):
        return Verification(False, ("COORDINATOR_AUDIT_MANIFEST_INCOMPLETE",), scope=scope)
    baseline = verify_local_boundary(repository=repository, code_sha=code_sha, settings=settings)
    if not baseline.passed:
        return baseline
    audit = audit_local_call_path(
        repository,
        entrypoints=(("kalshi_predictor.overnight_paper.coordinator", coordinator_symbol),),
    )
    if not audit.passed:
        return Verification(False, audit.blockers, scope=scope)
    try:
        paths: list[tuple[str, str, Path, str]] = []
        for module, actual_hash in audit.source_hashes:
            relative = "src/" + module.replace(".", "/") + ".py"
            path = repository / relative
            if not path.is_file():
                relative = "src/" + module.replace(".", "/") + "/__init__.py"
                path = repository / relative
            paths.append((module, relative, path, actual_hash))
        batch = subprocess.check_output(
            ["git", "-C", str(repository), "cat-file", "--batch"],
            input="".join(code_sha + ":" + relative + "\n" for _, relative, _, _ in paths).encode(),
            timeout=30,
        )
        offset = 0
        for module, _, path, actual_hash in paths:
            end = batch.index(b"\n", offset)
            header = batch[offset:end].split()
            if len(header) != 3 or header[1] != b"blob":
                raise ValueError("CALL_GRAPH_COMMIT_SOURCE_MISSING:" + module)
            size = int(header[2])
            committed = batch[end + 1 : end + 1 + size].replace(b"\r\n", b"\n")
            offset = end + 1 + size + 1
            if hashlib.sha256(committed).hexdigest() != actual_hash:
                raise ValueError("CALL_GRAPH_SOURCE_DIFFERS_FROM_COMMIT:" + module)
            loaded = sys.modules.get(module)
            if loaded is not None:
                origin = getattr(loaded, "__file__", None)
                if origin is None or Path(origin).resolve() != path.resolve():
                    raise ValueError("CALL_GRAPH_RUNTIME_ORIGIN_MISMATCH:" + module)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return Verification(False, (str(exc),), scope=scope)
    return Verification(True, (), tuple(value for _, value in audit.source_hashes), scope)
