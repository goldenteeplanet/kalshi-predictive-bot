# Phase 4IA — Database-readability classifier

Phase 4IA classifies captured database probe evidence without opening or querying a database. Readability requires coherent proof that the connection opened, the schema was readable, a protected read query succeeded, and no error occurred.

Known failures classify as `UNREADABLE`; contradictory or unrecognized evidence is `UNKNOWN`; incomplete, future, stale, malformed, or tampered evidence fails closed. Evidence is fresh through the exact 120-second endpoint.

Database unreadability is never restart-eligible. All non-readable outcomes require operator alerting and none authorize recovery, service control, WSL or Windows restart, order creation, or execution.
