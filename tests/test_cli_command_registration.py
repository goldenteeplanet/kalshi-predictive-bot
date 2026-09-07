"""Catch silent command replacement before importing the full CLI graph."""

import ast
from collections import Counter
from pathlib import Path


def test_root_cli_commands_are_registered_once() -> None:
    source = Path(__file__).parents[1] / "src/kalshi_predictor/cli.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    names = []
    for node in module.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "app"
                and decorator.func.attr == "command"
            ):
                name = decorator.args[0] if decorator.args else next(
                    (item.value for item in decorator.keywords if item.arg == "name"), None
                )
                if isinstance(name, ast.Constant) and isinstance(name.value, str):
                    names.append(name.value)
                elif name is None:
                    names.append(node.name.replace("_", "-"))
    duplicates = {name: count for name, count in Counter(names).items() if count > 1}
    assert not duplicates, f"CLI commands silently replace earlier handlers: {duplicates}"
