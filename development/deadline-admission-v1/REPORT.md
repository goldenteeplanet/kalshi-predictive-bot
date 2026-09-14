# Deadline admission fixture V1 — UNTESTED

New development source and eight new focused tests are ready for root's single cloud execution. No local Python/test process, provider call, host command, daemon, production path or old suite was executed by this task.

Source SHA256: `13583252402acb768e4f43f337e65a042645f40a1cb42e2782a9b7ed99afa1e8`

Test SHA256: `4bb01e5813ec5b93e845e9e4d5c3cbca74eb734d82397b0ab3185b102402b94a`

`effective_runtime(now, cutoff, required_seconds, maximum_seconds, stop_grace_seconds, safety_margin_seconds)` returns `effective_runtime_seconds`, `status` (`TIME_AVAILABLE` or `INSUFFICIENT_TIME`), required seconds and absolute cutoff. Explicit UTC datetimes and exact positive integer budgets are required. Runtime floors remaining seconds and subtracts both stop grace and safety before applying the maximum. Insufficient work is refused rather than extending the cutoff.

`dispatch_once` exclusively consumes an identity directory, fsyncs its parent, preserves source/input pins and original input, then fsyncs an intent containing the actual selected runtime and absolute expiry. It checks a fresh clock after persistence and again after source/original verification. Either check refuses if the original recorded runtime no longer fits; it conservatively does not rewrite that intent to create another attempt. Callback exceptions retain an uncertain receipt and the consumed identity. A callback return explicitly does not establish worker completion.

`worker_start` checks an externally pinned intent and source/input context, then uses its actual start clock to reduce runtime or reject insufficient remaining work. It is a second time gate, not a launcher or a separate worker identity registry. Root's separate runner owns the absolute hard watchdog and owned process group termination.

The eight authored cases cover exact fit and persisted originals visible inside the callback; delayed dispatch with zero callbacks; delay after the second gate; uncertain callback with refusal on second attempt; delayed worker with zero workload; valid later worker start with reduced runtime; future/naive/invalid budget refusal; and one-second-short/pin/input-size refusal. Fixtures use a temporary directory beneath the current working directory, intended to be `/output` while source remains under read-only `/work`.

Limits: this is a cooperative local namespace, with no authenticated clock, source provenance, globally unique registry, hostile filesystem race protection, OS quota or hard runtime enforcement. POSIX file/directory fsync is required; unsupported directory fsync fails closed. Namespace custody and preserved originals must remain under root's external ownership. A process interruption may leave a consumed directory without a complete receipt; it must not be retried. The worker gate does not prevent multiple calls with the same intent. External execution must enforce that separately. The final gate and callback cannot be atomic with wall-clock time; the worker-start gate and external absolute watchdog remain mandatory. Bounded authored byte limits do not establish total RSS or hostile concurrent file-growth bounds.

Next step: independent root review, then one newly admitted cloud-only test job within root's fresh cutoff, 60-second maximum, 256 MiB memory, 25% CPU and 16-task ceiling. Record the actual result without changing these source/test pins. No production readiness or deployment is claimed.
