# Phase 4HT — Alert delivery audit export

Phase 4HT provides a deterministic, bounded, redacted export of alert delivery evidence. Records contain only SHA-256 identifiers, a constrained channel code, outcome, timestamp, completeness marker, and integrity hash. Raw incident, event, operator, message, address, and endpoint values are excluded.

Exports are canonically sorted and serialized, bind an inclusive time window, and include content and export hashes. Duplicate events, incomplete records, records outside the window, malformed fields, excess volume, or tampering fail closed.

The exporter is a pure in-memory function. It cannot send alerts, write files or databases, control services, restart WSL or Windows, create orders, or authorize recovery or execution. Persistence or transmission of a validated bundle requires a separate explicitly gated component.
