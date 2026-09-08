# Phase 4DE — Forecast Determinism Matrix

Phase 4DE evaluates the same supplied synthetic forecast rows across declared repeat,
process-count, input-order, locale, timezone, and supported dependency-version contexts.
It canonicalizes result order and uses exact Decimal arithmetic, requiring every context
to produce the same canonical result hash.

Contexts are data, not host mutations: the tool does not change locale, timezone, process
settings, or installed dependencies. Unsupported environments or versions, malformed
matrix entries, duplicate identities, invalid decimals, and hash tampering fail closed.

The deterministic, hash-protected report is published atomically. The module has no
database, network, forecast persistence, exchange, service-control, or trading capability.
