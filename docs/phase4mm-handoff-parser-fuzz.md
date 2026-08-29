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
- Corpus SHA-256: `ec6085afeec291717bd7d5cc3a248fd96ce6cd5f42edc597577034d09f839452`
- Minimization SHA-256:
  `16504f16067537cc2099b87f40188f391b039b676e862d4d553bcbb21d920a06`
- Deterministic resource-envelope SHA-256:
  `4386381a20afa05cdf11389e84ef483f59960bdb48e3ad94f5b368f143678483`
- Complete fuzz certification SHA-256:
  `ef3645a298a901d09f1284157939b288dc7965952d0be727fd962dc5b5ef1c3f`

## Safety and removal

All processing is offline, deterministic, read-only, and in memory. Exceptions are contained. No
package is written or extracted; no network, runtime, WSL, service, or order capability exists.
Remove the script, focused test, and report to roll back.

## Next phase

Phase 4MN — Handoff-verifier differential implementation and consensus gate.
