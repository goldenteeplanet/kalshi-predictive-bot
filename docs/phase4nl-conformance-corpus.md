# Phase 4NL — Golden Corpus and Verifier Conformance

## Outcome

Phase 4NL provides a versioned executable corpus covering valid replay, canonical boundary values,
bundle corruption, schema/model/scenario drift, nondeterminism, output mismatch, unsafe capability
mutation, timestamp and numeric failures, Unicode-key collisions, encoding, path and platform leaks,
unsupported shapes, and platform representation mismatch.

Every vector has a stable identifier, expected verdict and exact error set, result hash, source
fixture hash, evaluator provenance, and vector hash. The corpus manifest binds the ordered IDs and
hashes. Its runner refuses missing or duplicate vectors, vector or manifest corruption, version or
envelope drift, changed verifier behavior, and incomplete refusal-class coverage.

## Verification evidence

- Focused Phase 4NJ–4NL suite: `21 passed`
- Executable vectors: `23`
- Required refusal classes covered: `22` of `22`
- Corpus SHA-256: `18b1ba91f119413bd649ebfa5fe40caee8cdc12692d1e0ab80f1f29b3e0056c1`
- Manifest SHA-256: `8edbb4b7d1822801087225d9fbd73793b659d3945a698538ec63d96ce7ddbf64`
- Conformance verdict: `PASS`
- Conformance SHA-256: `096c9f801fbb0b2681ba39bdc0866b8815c8ce0c040b54fdfe9e2b6d7311605a`

## Safety and removal

The corpus and runner remain offline, non-persistent, and incapable of creating orders or enabling
paper, demo, live, or autopilot execution. Remove the three phase files to roll back.

## Next phase

Phase 4NM — Differential verifier implementation and cross-check proof.
