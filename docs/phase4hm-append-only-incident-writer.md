# Phase 4HM — Append-Only Incident Writer

## Outcome and measured evidence

Phase 4HM adds a bounded append-only JSONL writer for Phase 4HL entries. Every append requires an
absolute target beneath an explicit allowed root, validates the complete existing hash chain, checks
the exact next sequence/link, appends one canonical record with OS append semantics, flushes it, and
returns a hash-protected receipt. All mutation tests use pytest temporary directories.

Focused tests cover two-entry append/linkage, dry-run non-mutation, path escape/relative/suffix refusal,
exact file size and line/count bounds, sequence/link failures, corrupt and partial existing journals,
record/receipt/safety tampering, and absence of database/service/notification/restart surfaces.

## Contract, provenance, freshness, and bounds

- Receipt schema: `phase4hm-append-only-incident-writer-receipt-v1`.
- Defaults: at most 10,000 entries, 8 MiB per journal, and 8 KiB per canonical record.
- Dry-run is the default and performs complete validation without creating or changing a file.
- Targets must be absolute `.jsonl` paths strictly below a caller-supplied absolute allowed root.
- Symlink targets, incomplete trailing records, invalid JSON/schema/hashes, and broken chains fail closed.
- Receipts bind a redacted path hash, entry identity, before/after counts and bytes, journal hash, and mode.

## Safety analysis, rejected alternatives, rollback, and next dependency

The only mutation is an explicitly requested local append under the allowed root. The writer cannot
access a database, notify externally, query/control WSL or systemd, control the scheduler, or restart
Windows. Overwriting, truncating, repairing, or silently skipping invalid records was rejected.

Rollback is deletion of the implementation, focused test, and report; fixture journals are disposable.
Phase 4HN should probe Windows-toast capability without sending a notification.
