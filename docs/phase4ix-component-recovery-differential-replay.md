# Phase 4IX — Component recovery differential replay

Phase 4IX compares hashed baseline and candidate recovery-planner outputs across a bounded set of unique scenarios. Equivalence requires identical status and output hash plus preserved candidate safety proof for every complete case.

Status or output differences yield `DRIFT`; lost or absent candidate safety proof yields `SAFETY_REGRESSION`; empty or incomplete case sets are `INCOMPLETE`; duplicate scenarios, malformed data, or tampering fails closed. Safety regression takes precedence over ordinary drift.

Replay is offline and read-only. It performs no component recovery and grants no service-control, host-restart, order, or execution authority.
