# Phase 4HZ — Partial-evidence refusal

Phase 4HZ requires one complete and verified artifact for dependency allowlisting, failure quorum, failure persistence, and failure classification. The canonical envelope is accepted only when the exact required set is present.

Missing or incomplete evidence yields `INCOMPLETE`, unverified evidence yields `REFUSED`, and duplicate or unknown artifact types yield `TAMPERED`. References and the complete envelope are integrity-bound and order-independent.

`COMPLETE` means only that later classifiers may inspect the evidence. It grants no recovery, service-control, WSL or Windows restart, order, or execution authority.
