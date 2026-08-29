# Phase 4MM — Handoff Parser Fuzzing and Resource-Bound Certification

## Outcome

Phase 4MM adds a deterministic lexical resource gate before JSON and Phase 4ML semantic decoding.
It bounds input bytes, nesting depth, structural tokens, string bytes, integer digits, and total scan
work; duplicate keys and non-finite numeric constants are explicitly refused.

## Fuzz and minimization

The bounded corpus covers malformed encodings and syntax, deep and oversized structures, numeric
edges, duplicate keys, truncation boundaries, count and expansion claims, normalized and confusable
paths, hash substitution, and type confusion. Representative failures are minimized only while
preserving their refusal class, with a hard attempt limit.

## Reproducible evidence

- Real-package fuzz cases refused: 22 of 22 across 15 surfaces.
- Corpus SHA-256: `892b81229a32ed78d7f7e6cc67d041f07a938e8db26adb0ae5ade633941ef4bb`
- Minimization SHA-256:
  `16504f16067537cc2099b87f40188f391b039b676e862d4d553bcbb21d920a06`
- Deterministic resource-envelope SHA-256:
  `1184b1693330175fca8ea93370ebf965c0d576c5adc76f3e7da66b64e39a0130`
- Complete fuzz certification SHA-256:
  `ee044cf8b25fe638506e3db70a17532c7e209628dd9a0be996342330897c89ac`

## Safety and removal

All processing is offline, deterministic, read-only, and in memory. Exceptions are contained. No
package is written or extracted; no network, runtime, WSL, service, or order capability exists.
Remove the script, focused test, and report to roll back.

## Next phase

Phase 4MN — Handoff-verifier differential implementation and consensus gate.
