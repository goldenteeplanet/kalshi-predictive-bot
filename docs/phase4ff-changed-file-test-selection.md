# Phase 4FF — Conservative Changed-File Test Selection

Known source files select their mapped focused tests for early feedback. Any unmapped file, dependency/lock configuration, workflow, or script selects the full suite. Paths are repository-relative and traversal is rejected. Focused selection never replaces the required complete pre-merge suite.
