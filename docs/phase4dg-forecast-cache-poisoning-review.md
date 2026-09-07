# Phase 4DG — Forecast Cache Poisoning Review

Phase 4DG independently reviews supplied cache-entry artifacts without consuming or
repairing them. It recomputes each content address from schema, market, ticker, model,
feature, evidence, and result identity and compares that address with the claimed key.

The review detects model or market substitution, wrong tickers, stale evidence, expired
entries, schema drift, artifact tampering, content-address mismatch, duplicate identities,
and key aliasing indicators. Well-formed adversarial entries produce explicit `UNSAFE`
findings; malformed outer evidence fails closed.

The report is deterministic, hash-protected, and atomically published. No cache entry is
read from or written to a connected store, and the module has no database, network,
forecast creation, exchange, service-control, or trading capability.
