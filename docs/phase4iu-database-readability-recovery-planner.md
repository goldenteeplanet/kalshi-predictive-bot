# Phase 4IU — Database-readability recovery planner

Phase 4IU is a read-only database-readability check planner bound to a valid Phase 4IP `DATABASE_READABILITY_CHECK` capability, classifier evidence, and a hashed target. Unreadable state yields symbolic path-metadata verification, read-only integrity checking, diagnostic capture, and operator escalation. Readable state yields verification only.

Unknown or tampered classification is denied; incomplete classification or evidence remains incomplete. Wrong capability, binding mismatch, malformed data, or tampering fails closed. The plan inherits the 30-second ceiling and exactly-one-attempt rule.

This phase does not plan repair. It forbids database writes, migrations, deletion, restore, service control, and restart, and grants no order or execution authority.
