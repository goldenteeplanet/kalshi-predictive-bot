# Phase 4KF: Post-boot database-readability verification

Phase 4KF extends the post-boot chain with a deterministic, read-only evaluation of the existing database-readability classifier decision. A pass requires the Phase 4KE scheduler prerequisite and fresh, coherent, integrity-validated proof that the database connection, schema, and protected query are readable.

Unreadability fails the gate. Unknown or incomplete evidence blocks it, and future-dated evidence is treated as tampering. The content-addressed result grants no database-write, recovery, restart, or execution authority.

The phase evaluates already-captured evidence only. It does not open a database connection, execute a query, mutate state, control a service, or restart the host.
