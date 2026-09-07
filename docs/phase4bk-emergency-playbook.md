# Phase 4BK — Emergency Abort and Recovery Playbook

Phase 4BK validates a complete, ordered catalog of nine emergency scenarios and emits a
hash-protected playbook plus a non-executing recovery proof. Every action is a declarative enum;
the evaluator never controls a process, service, lock, database, or exchange.

The scenarios cover aborts before a transaction, during validation, and during disposable
simulation; ambiguous completion; missing receipts; mismatched artifact pairs; changed database
identity; operator revocation; and a suspected competing writer. Missing, reordered, malformed,
or tampered evidence fails closed.

## Command

```text
python scripts/local/phase4bk_emergency_playbook.py \
  --emergency-scenarios scenarios.json \
  --evaluation-time 2026-08-25T12:00:00Z \
  --playbook-output playbook.json \
  --proof-output recovery-proof.json
```

The two output files are published atomically as a bound pair. They grant no authority and are
usable only for offline evidence handling and safe refusal.
