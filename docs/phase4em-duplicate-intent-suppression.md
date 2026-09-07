# Phase 4EM — Duplicate Intent Suppression

Phase 4EM generates canonical artifact-only idempotency keys spanning database identity,
forecast, immutable portfolio snapshot, ranking, Phase 3M position sizing, Phase 3N advanced
risk, operator approval, and the complete paper-intent terms: market, side, quantity, price,
and expiration.

Intents with identical identity components share a key; the lexicographically smallest
intent identity is the deterministic primary and all others are suppressed. Changing any
single lineage or intent component changes the key. Input order cannot affect the result.
Malformed hashes, terms, timestamps, duplicate identities, schema drift, and artifact
tampering fail closed.

Keys exist only in the output artifact. The tool writes no idempotency or order record,
creates no paper order, has no connected-system capability, and publishes atomically.
