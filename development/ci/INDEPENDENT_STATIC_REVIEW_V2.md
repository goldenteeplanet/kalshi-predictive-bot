# Revised CI shell — scoped static acceptance

Reviewed run-native-ci.sh SHA256 `9dbb5ad92c11aef36715fa722349187b49e6e8eb5eccd55bc9d0d037ec7c78c0`. No execution or cloud mutation. All three prior findings are addressed for the selected development suites.

Required source/test files are checked and hashed, followed by exact unittest discovery counts of 8, 11 and 15 before execution. Nested shell/Python quoting is valid on static inspection: the inner shell receives ci-worker as $0, unit as $1 and expected count as $2; Python receives that count as argv[1]. Missing files, changed counts and unsuccessful results prevent the PASS marker. The tests remain the selected development suites, not all campaign proofs.

The EXIT trap captures the incoming exit status, retains stop status, verifies exact owned cgroup absence and forces failure if the group persists. It does not erase an earlier failing status when cleanup succeeds. Explicit success-path cleanup remains in place. CPUQuotaPerSecUSec is captured and required to equal 500ms, alongside the other existing resource assertions.

Remaining scope: source hashes are recorded, not compared to a fixed pin manifest inside this script. The publication commit and root's external staging manifest supply source identity. Cgroup absence is observed immediately after synchronous stop; transient persistence causes conservative failure rather than a false success. SIGKILL/runner loss can bypass the shell trap, and no universal cancellation guarantee is claimed. Actual native CI results are still required before declaring runtime validation successful.
