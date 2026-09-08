# Phase 4JC — Restart cancellation token

Phase 4JC defines a single-use, integrity-bound cancellation token tied to one incident, warning decision, and restart intent. Tokens are valid only during their explicit warning window, for at most 300 seconds, and are evaluated against an explicitly supplied time.

Binding mismatches are `TAMPERED`; incomplete, future, expired, excessive-TTL, non-single-use, and malformed tokens fail closed. Re-evaluating a consumed token is an idempotent `USED` cancellation result.

Validation keeps restart denied and grants no restart, service-control, or execution authority. The token evaluator is read-only and performs no persistence, notification, process, or restart operation.
