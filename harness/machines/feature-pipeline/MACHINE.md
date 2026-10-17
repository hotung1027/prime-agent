---
name: feature-pipeline
description: "Run one task through a bounded FSM: plan, implement, adversarially review, independently verify, then merge. A failed review (findings) or verification (failures) returns the task to the implementer; merge runs only after both pass."
version: 1
author: randyt
---

# feature-pipeline

## What this machine does

Runs **one task** as a finite state machine, mirroring the orchestration pattern in Prime
Intellect's "Rewriting Prime Agent in Rust" blog: the root agent is the *orchestrator* (it plans
the task list and merges finished work), and this machine is the *one state machine per task*
that moves each task through four roles plus a merge.

- `plan` (entry) - the **planner** reads the task brief at `.prime-pipeline/task.md`, inspects the
  repo, and writes the full spec (intended change, ground truth, and the objective acceptance
  check that decides success) to `.prime-pipeline/spec.md`. Returns `{"spec_path": "..."}`.
- `implement` - the **implementer** reads the spec, writes the change on a branch/worktree, and
  addresses the review findings (`.prime-pipeline/review.md`) and verification failures
  (`.prime-pipeline/verify.md`) bound from the previous round. Returns `{"impl": {...}}`.
- `review` - the **reviewer** inspects the diff adversarially, in a separate context, on a
  a fresh context, with high reasoning. A different reviewer model is preferable but not set here: on this account only `deepseek-v4.1-flash` and `aion-labs/aion-3.0` answer (Anthropic/OpenAI/Google return 403, and aion-3.0 emitted no final text), so the reviewer inherits the session default. Writes findings to `.prime-pipeline/review.md`. Returns
  `{"verdict": {"approved": bool, "notes": "..."}}`.
- `verify` - the **verifier** runs the acceptance check from the spec independently, in a fresh
  context. Writes failures to `.prime-pipeline/verify.md`. Returns
  `{"check": {"passed": bool, "notes": "..."}}`.
- `merge` - the **merger** integrates the change and confirms the check on the integrated result.

Transitions: `plan -> implement -> review`; review loops back to `implement` on
`approved: false`, else goes to `verify`; verify loops back to `implement` on `passed: false`,
else goes to `merge`. Loops are bounded (`max_entries: 4`) and the run uses the `escalate`
failure policy, so a stuck run pauses for a human decision instead of failing silently.

Generation and verification are deliberately separate agents, and the reviewer runs on a
different model, because an agent that wrote the code is a biased judge of it.

## How to run it

1. Write the task brief (goal, scope, constraints) to `.prime-pipeline/task.md` in the working
   tree the children should use.
2. `res = await rlm.run_factory("feature-pipeline", name="<task>")` - returns immediately.
3. `await rlm.watch_factory(res["run_id"], 30)` to follow progress; `rlm.status_factory(run_id)`
   for per-state status; `rlm.graph_factory(run_id)` for the fused structure+state view.
4. `await rlm.stop_factory(run_id)` cancels; `await rlm.resume_factory(run_id)` continues a paused
   run.

The machine takes no input parameters, so the task travels through `.prime-pipeline/task.md`
(and the spec through `.prime-pipeline/spec.md`). Run one task per run; for many tasks, loop over
runs from the orchestrator session.

## Port binding rules (learned the hard way)

- A `json` output named `X` binds only from a reply whose JSON object has a top-level key `X`
  (e.g. output `verdict` needs `{"verdict": {...}}`). A reply without that key fails the
  dependent state with "no JSON object containing output ...".
- Captured answers are ~160-character previews and ports bind from them. Keep every returned
  object far under 160 characters and put the detail in files
  (`.prime-pipeline/spec.md`, `review.md`, `verify.md`).
- A child's captured value is its **final assistant message**, so each role ends with only the json
  object (no prose summary, no code fence). Do NOT send an `agent_message`: a child is marked
  settled when it replies, so a reply sent before the final message makes the capture race and the
  port comes back empty.
- States set no explicit child `name`: a fixed name collides on a second run under the same
  parent. The executor's generated labels (`sw-<state>-<run>-iN`) are unique per run.

```machine-spec
{
  "run": {
    "budget_ms": 1800000,
    "max_parallel": 4,
    "max_transitions": 48,
    "failure_policy": "escalate"
  },
  "states": [
    {
      "id": "plan",
      "entry": true,
      "subagent": {
        "prompt": "You are the planner in a task pipeline. Read the task brief at `.prime-pipeline/task.md`.\n\nDefine the change spec *before* any code is written:\n- Inspect the repo and locate the files and current behavior the task touches.\n- State the intended change and the ground truth (expected behavior) it must produce.\n- Define the objective acceptance check that decides success: the exact command(s) to run and the expected result. This is what the verifier will run.\n\nWrite the full spec to `.prime-pipeline/spec.md` (overwrite it). Your result is exactly:\n\n{\"spec_path\": \".prime-pipeline/spec.md\"}\n\n\nReply with only that json object as your final message: no prose, no summary, no code fence, and do not send an agent message. The pipeline reads your final message."
      },
      "outputs": [
        {
          "name": "spec_path",
          "type": "json"
        }
      ],
      "budget_ms": 300000
    },
    {
      "id": "implement",
      "subagent": {
        "prompt": "You are the implementer in a task pipeline. Read the spec at `{spec_path}` and implement it.\n\nReview verdict (null if none yet): {verdict}\nVerification check (null if none yet): {check}\n\nIf `verdict` is not null, read `.prime-pipeline/review.md` and address every finding.\nIf `check` is not null, read `.prime-pipeline/verify.md` and fix every failure.\nThen re-check the spec, and run the relevant tests.\n\nYour result is exactly this json shape:\n\n{\"impl\": {\"branch\": \"<branch or none>\", \"head\": \"<short commit or none>\"}}\n\n\nReply with only that json object as your final message: no prose, no summary, no code fence, and do not send an agent message. The pipeline reads your final message."
      },
      "inputs": [
        {
          "name": "spec_path",
          "type": "json",
          "from": "plan.spec_path"
        },
        {
          "name": "verdict",
          "type": "json",
          "from": "review.verdict",
          "optional": true
        },
        {
          "name": "check",
          "type": "json",
          "from": "verify.check",
          "optional": true
        }
      ],
      "outputs": [
        {
          "name": "impl",
          "type": "json"
        }
      ],
      "max_entries": 4,
      "budget_ms": 420000
    },
    {
      "id": "review",
      "subagent": {
        "prompt": "You are the reviewer in a task pipeline. Review the change adversarially and in a context separate from the implementer: look for reasons it is wrong, not reasons it is right.\n\nSpec path: {spec_path}\nImplementation: {impl}\n\nCheck the diff against the spec: correctness, unhandled error paths, regressions, missing tests, and whether the acceptance check actually covers the task.\n\nWrite the full findings to `.prime-pipeline/review.md` (one bullet per finding; write \"none\" if there are none) and overwrite it. Your result is exactly this json shape, with `notes` under 60 characters:\n\n{\"verdict\": {\"approved\": <true|false>, \"notes\": \"<one short line>\"}}\n\n\nReply with only that json object as your final message: no prose, no summary, no code fence, and do not send an agent message. The pipeline reads your final message.",
        "thinking": "high"
      },
      "inputs": [
        {
          "name": "spec_path",
          "type": "json",
          "from": "plan.spec_path"
        },
        {
          "name": "impl",
          "type": "json",
          "from": "implement.impl"
        }
      ],
      "outputs": [
        {
          "name": "verdict",
          "type": "json"
        }
      ],
      "max_entries": 4,
      "budget_ms": 300000
    },
    {
      "id": "verify",
      "subagent": {
        "prompt": "You are the verifier in a task pipeline. Run the acceptance check from the spec yourself, in a fresh context, on the implemented work. Do not trust the implementer's or reviewer's summary; run the commands.\n\nSpec path: {spec_path}\nImplementation: {impl}\n\nRun the exact acceptance command(s) from the spec plus neighbouring tests. Report only what you observed.\n\nWrite the full failures to `.prime-pipeline/verify.md` (one bullet per failure; write \"none\" if the check passed) and overwrite it. Your result is exactly this json shape, with `notes` under 60 characters:\n\n{\"check\": {\"passed\": <true|false>, \"notes\": \"<one short line>\"}}\n\n\nReply with only that json object as your final message: no prose, no summary, no code fence, and do not send an agent message. The pipeline reads your final message."
      },
      "inputs": [
        {
          "name": "spec_path",
          "type": "json",
          "from": "plan.spec_path"
        },
        {
          "name": "impl",
          "type": "json",
          "from": "implement.impl"
        }
      ],
      "outputs": [
        {
          "name": "check",
          "type": "json"
        }
      ],
      "max_entries": 4,
      "budget_ms": 300000
    },
    {
      "id": "merge",
      "subagent": {
        "prompt": "You are the merger in a task pipeline. The reviewed and verified change is ready to integrate.\n\nSpec path: {spec_path}\nImplementation: {impl}\nReview verdict: {verdict}\nVerification check: {check}\n\nIntegrate the change (merge the branch / apply the diff), confirm the acceptance check still passes on the integrated result, and clean up temporary worktrees or branches.\n\nReply with only that one line (at most 120 characters) as your final message: no prose, no summary, and do not send an agent message.\n"
      },
      "inputs": [
        {
          "name": "spec_path",
          "type": "json",
          "from": "plan.spec_path"
        },
        {
          "name": "impl",
          "type": "json",
          "from": "implement.impl"
        },
        {
          "name": "verdict",
          "type": "json",
          "from": "review.verdict"
        },
        {
          "name": "check",
          "type": "json",
          "from": "verify.check"
        }
      ],
      "outputs": [
        {
          "name": "report",
          "type": "text"
        }
      ],
      "budget_ms": 180000
    }
  ],
  "transitions": [
    {
      "from": "plan",
      "to": "implement"
    },
    {
      "from": "implement",
      "to": "review"
    },
    {
      "from": "review",
      "to": "implement",
      "when": {
        "output": "verdict",
        "path": "approved",
        "op": "eq",
        "value": false
      }
    },
    {
      "from": "review",
      "to": "verify",
      "when": {
        "output": "verdict",
        "path": "approved",
        "op": "eq",
        "value": true
      }
    },
    {
      "from": "verify",
      "to": "implement",
      "when": {
        "output": "check",
        "path": "passed",
        "op": "eq",
        "value": false
      }
    },
    {
      "from": "verify",
      "to": "merge",
      "when": {
        "output": "check",
        "path": "passed",
        "op": "eq",
        "value": true
      }
    }
  ]
}
```
