# Phase 4EV — Safety-Critical Mutation Scanner Expansion

Phase 4EV discovers every Phase 4EA–4EU risk and paper-routing Python artifact and requires
an exact, hash-bound manifest. Missing, extra, duplicate, oversized, malformed, or changed
files fail closed before scanning. This prevents a newly added adapter from escaping review.

The AST scanner detects direct ORM/database/order/service calls, writable SQLite connections,
subprocess control, `eval`/`exec`, dynamic imports, calls through `getattr`, registry or mapping
dispatch, mutation-callable aliases, indirect exchange/execution/order/writer adapter imports,
and mutation-capable function definitions. Findings are deterministic, file- and line-bound,
and always close advancement.

The scanner is read-only apart from atomic publication of its requested report. It does not
acquire a writer lock, mutate a database, control services, contact an exchange, create an
order, or authorize execution.
