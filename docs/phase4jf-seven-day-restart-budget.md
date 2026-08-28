# Phase 4JF — Seven-day restart budget

Phase 4JF evaluates an integrity-bound restart history against a rolling 604,800-second window. Fewer than two recorded restarts leaves budget available; two or more exhausts it. Events exactly at the window boundary are aged out, and output ordering is canonical.

Incomplete or unverified state denies restart. Duplicate, future-dated, malformed, tampered, or over-bound history fails closed. The decision exposes the in-window count and remaining budget without reading an implicit clock.

`AVAILABLE` is budget evidence only and grants no restart, service-control, or execution authority. The evaluator performs no persistence, notification, process, service, or restart operation.
