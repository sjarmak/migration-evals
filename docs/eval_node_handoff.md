# Eval-node handoff — agent-pipeline workflow integration

Reference artifact for the human handoff tracked by bead
`migration_evals-vj9.7`. The actual PR ships in the agent pipeline's own
workflow runtime repo (separate codebase); per the `never-open-prs-in-sourcegraph`
guardrail, no agent in this repo opens that PR. This document describes
the contract, a reference Python skeleton, and the integration test the
PR should land with, so the human handoff is mechanical translation
rather than design from scratch.

Cross-references in this repo:
- [docs/integration_guide.md](integration_guide.md) — end-to-end smoke
  walkthrough; the eval node performs step "5. Run the funnel against
  your changeset" on every iteration.
- [docs/oracle_funnel.md §CI feedback loop integration](oracle_funnel.md#ci-feedback-loop-integration)
  — narrative description of the read/write hook this node implements.
- `src/migration_evals/funnel.py` — `run_funnel(...)` entrypoint.
- `src/migration_evals/oracles/verdict.py` — `FunnelResult.to_dict()`
  payload shape (the on-the-wire schema between this eval node and the
  agent's next prompt iteration).

---

## 1. Where the node sits in the agent pipeline

```
[ workflow start ]
        │
        ▼
[ changeset-creation node ]   ← agent emits result.diff
        │
        ▼
[ eval-node ]   ← THIS NODE: runs funnel, writes last_oracle_verdict
        │
        ▼
[ branching: verdict.passed? ]
        │      │
        Y      N → loop back to changeset-creation with verdict in prompt
        │
        ▼
[ auto-publish / handoff to human reviewer ]
```

The node is purely a verdict producer. It does not modify the diff and
does not gate the loop on its own — the gate decision lives in the
downstream branching node, which reads `last_oracle_verdict.passed`.
Keeping verdict and gate separate means an experiment can re-route the
gate (e.g. "publish only when judge is also passed") without touching
the node.

---

## 2. Inputs / outputs / failure modes

### Inputs

| Variable             | Source                                  | Type   | Notes |
|----------------------|-----------------------------------------|--------|-------|
| `repo_path`          | upstream changeset-creation node        | `Path` | Working tree the agent already mutated. |
| `recipe`             | workflow config (per-experiment static) | dict   | YAML-loaded; passed through to `run_funnel`. |
| `stages`             | workflow config (per-experiment static) | tuple\|None | Defaults to `("compile_only","tests","judge")` for the CI loop; full cascade is reserved for offline calibration. |
| `iteration_idx`      | workflow runtime                        | int    | Used as the key when writing `last_oracle_verdict_history`. |

### Outputs (workflow variables)

| Variable                        | Type                  | Lifetime         |
|---------------------------------|-----------------------|------------------|
| `last_oracle_verdict`           | `FunnelResult.to_dict()` | overwrites each iteration |
| `last_oracle_verdict_history`   | `dict[int, FunnelResult.to_dict()]` | appends each iteration, keyed by `iteration_idx` |

The `_history` variable is a deliberate redundancy: agents that look
back across iterations (e.g. "have we hit the same compile error
twice?") need it; the single-snapshot `last_oracle_verdict` exists for
agents that only condition on the most recent verdict.

### Failure modes

The node MUST emit a verdict on every code path. Three failure shapes,
all surfaced via `failure_class`:

| `failure_class`   | Cause                                  | Loop policy             |
|-------------------|----------------------------------------|-------------------------|
| `None`            | All executed tiers passed              | continue → publish      |
| `"agent_error"`   | A tier failed (compile / tests / judge) | loop back with verdict in next prompt |
| `"harness_error"` | Tier 1 (compile) failed OR no tier ran | abort loop; surface to human |

Subprocess invocation that crashes before emitting JSON is treated as
`"harness_error"` with a synthetic verdict — see §4.

---

## 3. Two implementation options

### Option A — Python import (preferred when sourcegraph runtime can pin migration-evals as a dep)

```python
# pseudocode for the workflow node body
from pathlib import Path

from migration_evals.funnel import run_funnel
from migration_evals.recipe import load_recipe        # loads workflow config dict → Recipe
from migration_evals.adapters_docker import DockerSandboxAdapter
from migration_evals.adapters_claude_code import ClaudeCodeAnthropicAdapter

def run(node_inputs: dict, workflow: WorkflowContext) -> dict:
    repo_path = Path(node_inputs["repo_path"])
    recipe = load_recipe(node_inputs["recipe"])
    stages = tuple(node_inputs.get("stages") or ("compile_only", "tests", "judge"))

    adapters = {
        "sandbox": DockerSandboxAdapter(image=node_inputs.get("sandbox_image", "golang:1.22")),
        "anthropic": ClaudeCodeAnthropicAdapter(),  # OAuth, not paid API — see migration_evals-anthropic-providers memory
        "enable_daikon": False,                     # never enable daikon in CI loop — too slow
    }

    funnel_result = run_funnel(
        repo_path,
        recipe,
        adapters,
        is_synthetic=False,
        stages=stages,
    )
    payload = funnel_result.to_dict()

    workflow.set_variable("last_oracle_verdict", payload)
    history = workflow.get_variable("last_oracle_verdict_history") or {}
    history[node_inputs["iteration_idx"]] = payload
    workflow.set_variable("last_oracle_verdict_history", history)

    return {"verdict": payload}
```

**Why this is the preferred shape:**
- No JSON serialization/deserialization overhead.
- Adapters can be configured once at workflow-definition time and reused.
- Stack traces from migration_evals propagate naturally (no
  subprocess-stderr-tail munging).

### Option B — Subprocess to the `migration-evals` CLI (when the runtime can't embed Python deps)

```python
import json
import subprocess
import tempfile
from pathlib import Path

def run(node_inputs: dict, workflow: WorkflowContext) -> dict:
    repo_path = Path(node_inputs["repo_path"])
    with tempfile.TemporaryDirectory() as out_dir:
        # Wrap the single repo in a "repos" directory so we can reuse the existing CLI shape.
        repos_root = Path(out_dir) / "repos"
        (repos_root / repo_path.name).mkdir(parents=True, exist_ok=True)
        # Hardlink or bind-mount repo_path → repos_root/<name>; copy is acceptable for small trees.
        ...

        config = _write_run_config(repos_root, recipe=node_inputs["recipe"], out_dir=Path(out_dir))
        proc = subprocess.run(
            ["migration-evals", "run", str(config),
             "--repos", str(repos_root),
             "--out", str(Path(out_dir) / "results"),
             "--stage", node_inputs.get("stage", "all")],
            capture_output=True, text=True, timeout=900,
        )
        if proc.returncode != 0:
            payload = _harness_error_payload(stderr_tail=proc.stderr[-2000:])
        else:
            payload = json.loads(
                (Path(out_dir) / "results" / repo_path.name / "result.json").read_text()
            )["funnel_result"]

    workflow.set_variable("last_oracle_verdict", payload)
    return {"verdict": payload}
```

Subprocess timeout MUST be set (the example uses 15 min). Tier 2
container builds occasionally hang on network egress; an unbounded
subprocess will pin the workflow runtime indefinitely.

The `_harness_error_payload` helper produces a synthetic
`failure_class="harness_error"` verdict so downstream gating logic
behaves identically whether the funnel ran cleanly or the subprocess
crashed:

```python
def _harness_error_payload(stderr_tail: str) -> dict:
    return {
        "per_tier_verdict": [],
        "final_verdict": {
            "tier": "none",
            "passed": False,
            "cost_usd": 0.0,
            "details": {"reason": "subprocess crashed", "stderr_tail": stderr_tail},
        },
        "total_cost_usd": 0.0,
        "failure_class": "harness_error",
        "quality_verdicts": [],
    }
```

---

## 4. Integration test (acceptance criterion on this bead)

The PR must include an integration test that exercises the read/write
of `last_oracle_verdict` end-to-end. Suggested skeleton (workflow-runtime
test framework will dictate the exact shape):

```python
def test_eval_node_writes_and_propagates_verdict(tmp_path):
    repo = _seed_fixture_repo(tmp_path)              # known-good Go repo
    recipe = _trivial_compile_recipe(repo)           # exercises only T1
    workflow = build_test_workflow(
        nodes=[
            ChangesetCreationNode(diff=_no_op_diff()),
            EvalNode(stages=("compile_only",)),
        ],
    )
    workflow.run(repo_path=repo, recipe=recipe, iteration_idx=0)

    verdict = workflow.get_variable("last_oracle_verdict")
    assert verdict is not None
    assert verdict["final_verdict"]["tier"] == "compile_only"
    assert verdict["final_verdict"]["passed"] is True
    assert verdict["failure_class"] is None

    history = workflow.get_variable("last_oracle_verdict_history")
    assert set(history.keys()) == {0}
    assert history[0] == verdict
```

A second test should cover the failure path — flip the diff to
something that breaks compile, assert `failure_class == "agent_error"`
and that `details["stderr_tail"]` carries the compiler error so the
agent's next-iteration prompt has actionable feedback.

---

## 5. Open questions for the human PR author

1. **Adapter sourcing.** Option A imports `migration_evals.adapters_*`
   directly. Does the workflow runtime have a way to pin the
   `migration-evals` Python package? If not, Option B is forced and the
   PR shape is meaningfully different.
2. **Cassette mode for tests.** The integration test above uses real
   docker / claude-code adapters. The `migration-evals` CLI honors
   `MIGRATION_EVAL_FAKE_SANDBOX_CASSETTE_DIR` and
   `MIGRATION_EVAL_FAKE_JUDGE_CASSETTE_DIR` env vars to swap in cassette
   adapters; the workflow test harness should set these to deterministic
   fixtures for CI rather than building real containers.
3. **Tier 3 backend in the loop.** Per memory
   `migration_evals-anthropic-providers`, the default for live runs is
   `claude_code` (OAuth via `claude -p`), not the paid SDK. The eval
   node must NOT default to `provider="sdk"` — that path is reserved for
   offline CI runners with `ANTHROPIC_API_KEY` set.
4. **Cost cap.** The CI loop will run this node N times per agent
   iteration. Per-tier costs are documented in
   [docs/oracle_funnel.md](oracle_funnel.md#per-tier-cost-targets) — the
   PR should expose a workflow config knob for max iterations so a
   thrashing agent doesn't burn the budget.

---

## 6. Bead acceptance

When the workflow-runtime PR lands, close `migration_evals-vj9.7` with
the merged-PR URL. The bead's acceptance is "PR landed in the agent
pipeline's repo introducing the eval node; an integration test
exercises the read/write of `last_oracle_verdict` end-to-end" — both
conditions must be in the linked PR.
