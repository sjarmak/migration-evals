# Failure Classification (PRD M6)

Every trial whose `success=False` is assigned exactly one of four
`FailureClass` values. The four classes partition failures by the *layer* that
caused them, which is the lens that matters most for triage:

1. **infra_error** - sandbox/container/VM never got out of the way.
2. **harness_error** - the migration recipe / test harness collapsed before
   the agent could do real work.
3. **oracle_error** - the agent believed it succeeded but the oracle
   subsystem threw.
4. **agent_error** - everything else (the agent itself failed the task).

The classes are disjoint and mutually exclusive. The classifier in
`src/migration_evals/failure_class.py::classify` checks them in the order
listed above; the first match wins.

## Decision Rules

| Priority | Class          | Trigger                                                                                            |
|---------:|----------------|----------------------------------------------------------------------------------------------------|
| 1        | `infra_error`  | `result.json.infra_error_marker` truthy, **or** `status.txt` / `infra.log` contains `docker` / `container exited` / `sandbox failed` / `image pull` / `oci runtime`. |
| 2        | `harness_error`| `result.json.harness_error_marker` truthy, **or** any of `stderr.log` / `stdout.log` / `harness.log` mentions `recipe failed` / `recipe error` / `harness error` / `harness failed` / `harness timeout` / `recipe not found` / `install failed` / `bootstrap failed`. |
| 3        | `oracle_error` | `result.json.oracle_error_marker` truthy, **or** `result.json.agent_reported_success=true` while `success=false`, **or** a file matching `ast_oracle_trace*` / `judge_error*` / `oracle_trace*` exists in the trial dir. |
| 4        | `agent_error`  | Default when `success=false` and no higher-priority signal fires. Also used when `result.json` is missing or unreadable. |

When `success=true`, `classify()` returns `None` - there is no failure to
classify.

## Example Signatures

### infra_error

- `status.txt` body: `sandbox failed: container exited 137 after 3 retries`
- `infra.log` body: `docker: image pull failed with 403`
- `result.json`: `{"success": false, "infra_error_marker": true, ...}`
- `status.txt` body: `OCI runtime create failed: container exited with 139`
- `status.txt` body: `sandbox failed: docker daemon unreachable`

### harness_error

- `result.json`: `{"success": false, "harness_error_marker": true, ...}`
- `stderr.log` body: `recipe failed: step 'mvn compile' returned 1`
- `stderr.log` body: `harness error: install failed for python 2.7`
- `harness.log` body: `recipe not found: oss-maven-sample-042/java8_17.yaml`
- `stdout.log` body: `bootstrap failed: unable to clone repo`

### oracle_error

- `result.json`: `{"success": false, "agent_reported_success": true, ...}`
- Trial directory contains `ast_oracle_trace.log`
- Trial directory contains `judge_error.json`
- `result.json`: `{"success": false, "oracle_error_marker": true, ...}`
- Trial directory contains `oracle_trace_compile.txt`

### agent_error

- `result.json`: `{"success": false, "failure_class": null, ...}` - vanilla
  agent-failed-the-task trial with no side-channel signals.
- Missing or corrupt `result.json` (cannot prove any other layer failed).
- `result.json`: `{"success": false, "score_post_cutoff": 0.12, ...}`
- `result.json`: `{"success": false, "agent_model": "claude-sonnet-4-6"}`
  with no `agent_reported_success` field.
- `stderr.log` content is agent planner noise with no recipe/harness/infra
  phrases.

## Hand-Off Contract

- `FailureClass` is imported from `migration_evals.types`. Callers and
  tests MUST use that enum; do not redefine.
- Adding a new failure class is a PRD-level change - downstream tools
  (reporting, alerting, IRT calibration) assume the four-way partition.
- Adding new detection *rules* within an existing class is allowed but should
  be reflected here and in `tests/fixtures/failure_class_cases/`.
