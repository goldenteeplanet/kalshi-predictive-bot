# Formatting patch — scoped static acceptance

Reviewed FORMATTING_DIFF.patch and finish-formatting.py without executing either, importing development modules or running tests. No blocking semantic change found in the reviewed arithmetic, identity/commit/deadline guards, process cleanup, manual SQL and subprocess-code literal splits.

The two ledger SQL literals use adjacent Python string concatenation; splitting inside SQL identifiers adds no whitespace or character changes. The TERM-resistant subprocess fixture likewise concatenates its split code literal back to the prior command. Replacing the module-level clock lambda with def preserves its call result (function introspection name changes, unused here). datetime.UTC replaces timezone.utc and requires Python 3.11 or newer; this is compatible with the Ubuntu 24.04 CI target but is a compatibility change for older interpreters.

Independent static text reconstruction of the capacity-planner-v1 patch against the retained original, using LF and a terminal newline entirely in memory, produces SHA256 `d657de722423e417dc0696bb97a3f2a6d31bb365733c8782835262b43873c64d`. Both ledger copies' PLANNER_PIN additions match this value; their planner patch content is consistent. This was text/hash inspection, not Python execution or a behavior test.

finish-formatting.py is a one-use targeted source transformer, not an idempotent general formatter. It assumes the original unsplit lines exist and contains no preimage guard or transactional rollback. Do not rerun it against already formatted files: its next-line search can find the first already split SQL chunk. The final patch is the review artifact; preserve the historical sources and completed attempt receipts separately as root reports.

The historical job.input.json pins are intentionally unchanged and should reject new source bytes. Do not update those historical inputs to make old dispatchers runnable. Static formatting review does not authorize a historical dispatcher, timer, provider call or old cloud attempt replay.

New CI on the final formatted commit is required. The prior 34-test result belongs to prior bytes; Ruff success establishes lint acceptance only. CI must retain its source hashes, exact 8/11/15 suite coverage, actual resource properties and whole-cgroup cleanup evidence before the formatted revision is considered validated. No runtime validation is claimed by this review.
