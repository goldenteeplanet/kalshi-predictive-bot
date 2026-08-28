# Phase 4LG — Evidence Parser Resource-Bound and Adversarial-Input Audit

## Outcome

Phase 4LG adds a strict UTF-8 JSON parser with pre-parse document and structural-depth bounds plus
post-parse limits for collection fan-out, strings, numeric tokens and range, scalars, receipts,
paths, and extensions. The CLI reads at most one byte beyond the document limit.

## Refusal coverage

The parser refuses duplicate keys, trailing data, non-finite and abusive numbers, malformed UTF-8,
oversized documents or strings, excessive nesting/fan-out/scalars, Unicode control and bidi text,
unsafe or non-normalized paths, and excessive receipt, path, or extension collections.

## Safety and removal

Parsing is deterministic, bounded, and read-only. There is no database, network, service, lock,
artifact-publication, decompression, object construction hook, or trading capability. Remove the
script, focused test, and report to roll back.

## Next phase

Phase 4LH — Evidence parser fuzz corpus and deterministic minimizer.
