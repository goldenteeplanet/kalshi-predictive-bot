# Phase 4LT — Cross-Platform Canonicalization and Time Invariance

## Outcome

Phase 4LT defines and audits a platform-neutral canonical form for Phase 4LR manifests and Phase 4LS
reports. Dictionary and named-artifact ordering, CRLF/LF, composed/decomposed UTF-8, and equivalent
timezone offsets produce the same SHA-256.

## Refusal policy

Windows and WSL runtime paths share one stable refusal signature. Naive, named-zone ambiguous or
nonexistent, excessive-precision, and otherwise noncanonical timestamps fail closed. Floats,
locale-formatted numeric strings, string booleans, integer overflow, normalized-key collisions, and
duplicate named-list entries are also rejected.

## Cross-runtime evidence

The fixed audit returned `PASS` under both the bundled native Windows Python and the WSL runtime.
Both produced baseline canonical SHA-256
`10ef573d60ece7fe77ffb0269e0ca87fa6f9afddabb58b0a9b75a4e61537fe42` and audit SHA-256
`0889e6046ff2af4ca969753faf32ea630d1da3d3c0b6f93c3e107c8b8fa6a1dc`.

## Safety and removal

The audit uses fixed in-memory evidence and cannot mutate files, access a network or database,
control services or WSL, deliver notifications, or create orders. Remove the script, focused test,
and report to roll back.

## Next phase

Phase 4LU — Canonical evidence signing-request and keyless verification contract.
