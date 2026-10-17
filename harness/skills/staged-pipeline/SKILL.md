---
name: staged-pipeline
description: Staged delivery pipeline (research/plan/implement/review/verify/gates/docs/merge)
---

Run the staged multi-agent delivery pipeline: research -> plan -> implement/debug -> review ->
verify(tests) -> stage gates -> document -> merge, with bounded loops and hooks.

Call from the Python kernel:
    import pipeline
    result = await pipeline.run_pipeline("<task brief>", stage="dev")
    # stage: dev (visual gate) | test (+security, cicd) | staging (+profiling) | prod (+infra)
Signature: run_pipeline(task_brief, *, stage="dev", work_dir=Path(".prime-pipeline"), max_rounds=3,
keep_children=False, cfg=None, model=None, reviewer_model=None, gate_model=None, linear_id=None,
task_kind=None). Returns {"ok":bool, "stage":..., "rounds":..., "gates":..., "log":[...]}.

Each role is a real child agent; the runner waits until the answer appears in the child's session
file (never trusts collect().settled, which can fire while a child still works) and uses an
INACTIVITY timeout, so long builds and suspended machines do not fail a working child.
Per-role models and fallback chains come from ~/.prime/agent/pipelines/pipeline.json
(models.roles.<role>); the researcher routes by task kind (websearch | scientific | default).
Hooks fire shell / MCP / agent actions on run_start, planned, implemented, file_changed, reviewed,
verified, gate, documented, merged, run_end, failure.

Pitfalls: a role's answer is its FINAL assistant message (or an agent_message reply) - prose
closings, a turn ending on a tool call, or >160-char payloads lose the value. Do not run two
pipelines against the same project directory at once.
