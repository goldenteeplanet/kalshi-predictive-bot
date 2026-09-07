"""Offline fail-closed scan for high-confidence credential assignments."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

EXCLUDED = {".git", ".pytest_cache", ".ruff_cache", ".mypy_cache", "__pycache__"}
PATTERNS = (
    re.compile(
        r"(?i)(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"][A-Za-z0-9_+/=-]{20,}['\"]"
    ),
    re.compile(r"gh[opusr]_[A-Za-z0-9]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)


def scan(root: Path) -> list[str]:
    findings = []
    for path in sorted(root.rglob("*")):
        if (
            not path.is_file()
            or any(x in EXCLUDED for x in path.parts)
            or path.suffix.lower() in {".png", ".jpg", ".pdf", ".sqlite", ".db"}
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if "pragma: allowlist secret" in line:
                continue
            if any(pattern.search(line) for pattern in PATTERNS):
                findings.append(f"{path.relative_to(root).as_posix()}:{number}")
    return findings


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--root", type=Path, required=True)
    x = a.parse_args()
    findings = scan(x.root)
    if findings:
        raise SystemExit("credential-like values found:\n" + "\n".join(findings))


if __name__ == "__main__":
    main()
