# Flaky-test policy

The CI quality-gates workflow runs the full unit suite once. If a test fails, it reruns only the failed test once without coverage instrumentation. The job summary reports the initial and rerun outcomes; a rerun pass is recorded as a flake and does not block the unit-test gate. A rerun failure remains blocking unless it has an active quarantine entry.

## Persistent log and alert signal

The cache-backed `.state/flaky-tests.json` log is retained across CI runs. It records each test's total initial failures, rerun passes and rerun failures. The workflow summary exposes these counts for every failed test, providing the structured recurring-flake signal for the existing observability/alerting ingestion path. The log is never removed merely because a test is quarantined.

## Quarantine

`.github/flaky-test-quarantine.json` is the reviewed quarantine registry. Each entry uses the full unittest id and a positive tracking issue number:

```json
{
  "tests": {
    "tests.ops.test_example.ExampleTests.test_unstable": {"issue": 123}
  }
}
```

CI fetches open issues at runtime. A quarantined test still runs and its outcomes stay in the flake log and job summary. It is excluded from the blocking verdict only when its tracking issue is currently open. Missing, malformed or closed issue references are blocking. Remove the registry entry when the issue is resolved.

Coverage, Ruff lint, Ruff format and mypy run independently and remain strict blocking gates.
