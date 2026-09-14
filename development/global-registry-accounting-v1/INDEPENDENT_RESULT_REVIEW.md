# Independent result review — tests pass, wrapper fails

Read-only review of retained results-registry-accounting-v1, run_registry.py, run_registry_tests.py and the proposed run-native-ci-registry.sh. No execution or replay occurred.

The retained stderr log names all fifteen new test cases as ok and records `Ran 15 tests in 2.812s` followed by OK. The suite/result.json and job-result.json agree: substantive child PID 1765187 exited zero; the owned runner reaped descendant PID 1765191; status was DESCENDANTS_TERMINATED. Recorded elapsed time was 5.757112 seconds, finishing 2026-09-14T14:47:24.155410Z before the 14:53:00Z cutoff. The effective-property receipt reports 256 MiB MemoryMax, 250ms CPUQuotaPerSecUSec, 16 tasks, 1-minute runtime and control-group cleanup.

run_registry.py accepts only COMPLETED and therefore returns exit 1 for this recorded result. The accurate classification is **fifteen module tests PASS; conservative integrated wrapper/native job FAIL**. Child return code zero alone must not replace the wrapper's actual acceptance rule. The recorded descendant PID does not identify its executable, parent history or purpose. A multiprocessing helper is a possible explanation, not an established fact; do not label it a resource tracker.

Root separately reports whole-cgroup absence and all exact owned units stopped by 14:48:16. That host observation is not re-established by the suite result JSON alone. Preserve its original lifecycle evidence alongside these receipts. The test log and wrapper failure should both remain in reporting.

These observations do not indicate an accounting-module correction. Publication can use a new native CI invocation with the existing external whole-cgroup boundary: the proposed script adds only global-registry-accounting-v1 source/test inventory and fifteen expected cases, producing four suite counts 8+11+15+15=49. It retains per-unit exit handling, effective resource checks and exact cgroup absence requirements. This new validation is distinct from replaying or retroactively accepting the consumed cloud wrapper. Do not edit that old wrapper, job input or receipts to manufacture a successful historical run.

Publication readiness still depends on lint and the new CI run against final published source hashes. No new CI success, global singleton authority, authenticated observation, restore safety or operational admission is established by this review.
