# Phase 4LE — Evidence Schema Forward-Compatibility Envelope

## Outcome

Phase 4LE defines explicit compatibility contracts for every evidence family introduced in Phases
4KX–4LD. Required, optional, immutable, extensible, and prohibited surfaces are machine-checkable,
with support for bounded additive minor revisions and refusal of unknown major versions.

## Safety and refusal coverage

The envelope refuses required-field removal, exact-type changes, immutable semantic changes, version
rollback, unknown families or contracts, non-canonical serialization, unsafe extension aliases,
conflicting top-level fields, and nested extensions that request database writes, service control,
network access, writer locks, artifact publication, exchange access, or trading/order capability.

## Removal

The implementation is an in-memory JSON validator with no runtime or trading integrations. Remove
the script, focused test, and report to roll back.

## Next phase

Phase 4LF — Evidence migration differential simulator.
