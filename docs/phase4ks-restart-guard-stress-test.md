# Phase 4KS: Cooldown and loop-breaker stress test

Phase 4KS stress-tests the composed restart guards over the exact six-hour cooldown boundary, the two-restarts-per-seven-days budget boundary, and the zero-versus-consumed incident-attempt boundary. Every trial compares observed eligibility with the combined fail-closed rule.

Missing boundary coverage is incomplete and any mismatch fails the report. The test is deterministic and read-only, grants no restart or execution authority, and invokes no host command.
