# Architecture plan v1

Status: proposed. Bottom layer first; each milestone is one commit with its own acceptance test.

## 0. Principles

1. **The kernel is the runtime.** CPython/IPython already has first-class functions, decorators,
   `asyncio`, `subprocess`, `contextvars`. No new daemon.
2. **Events are the only coupling.** A capability does not call another capability; it emits an event.
3. **Cheapest transport that satisfies durability.** inline < subprocess < durable.
4. **Model-free by default.** A model runs only where judgment is genuinely required.
5. **Every decision carries evidence.** A policy without a quoted user phrase or a commit sha is not a policy.
6. **Archive, never delete.**

## 1. Layer 0 - the event bus (kernel substrate)

```python
Event        name, attrs, causation_id, root_id, depth, ts, session
Verdict      allow | block | escalate | modify, reason, continuation
Continuation target_event, attrs        # work handed to another handler
dispatch(e)  -> [Verdict]               # match, sort by delivery, invoke, collect, emit
```

- **Deliveries**: `inline` (plain function), `subprocess` (script: allowlisted, sha256-pinned, argv
  exec, timeout, cwd pinned), `durable` (Temporal signal/workflow).
- **Guards**: depth <= 3, dedupe by content hash in a window, per-root budget, idempotency key
  `hash(event)` for durable handlers.
- **Registration**: `@on(name, when=..., delivery=..., budget=...)`; an empty matcher is a config
  error, because an unmatched `tool_end` fires dozens of times per turn.

Events available today: `turn_end` (signal we send), `commit` (git hook), `stage_end`/`run_start`
(engine hooks), `cron:*` (timer). There is no tool-level hook in the runtime; do not design around one.

## 2. Layer 1 - middleware and tracing (a ~200-line langgraph, not langchain)

Middleware ordering around every handler invocation:

```
budget -> permission -> trace span -> invoke -> record verdict -> emit continuation
```

- **State graph**: nodes are handlers/states, edges are verdict transitions, every loop carries
  `max_iterations` and `on_exhausted: escalate`. That is all the graph machinery we need; langchain's
  dependency weight and hidden prompts are not wanted.
- **Langfuse**: one span per dispatch (`event`, `handlers`, `verdicts`) and one child span per handler
  (`delivery`, `verdict`, `cost`). The local JSONL timeline remains the offline fallback, so tracing
  never blocks work.
- **Why this layer exists**: budgets, redaction and spans must be impossible to forget. Putting them
  in the dispatcher means a new handler inherits them for free.

## 3. Layer 2 - capabilities as handler sets

| capability | registers | delivery |
|---|---|---|
| subagent | `task.request` | durable |
| hooks | any event (matcher + budget) | inline / subprocess |
| durable functions | `*.schedule` | durable |
| factory | `stage_end`, `merge` | durable |
| continual learning | `turn_end` (data-gated), `cron:nightly` | durable |
| llm_wiki | `session_close`, `cron:nightly` (garden) | inline / durable |
| trace / batch / decision / tools | pure leaves | inline |

Nothing here is a new component: each is a `fn(event) -> Verdict | Continuation`.

## 4. Security model for hook scripts

- **Allowlist by path and sha256** in a committed manifest; a vault note can never add or change a
  script. The learner writes prompts and notes, never scripts.
- **argv exec, never `bash -c`**: a filename cannot inject.
- **Timeout, output cap, cwd pinned to the repo, read-only by default**, writable paths declared.
- **The vault is untrusted input.** Anything pasted into a note is data, not configuration.

## 5. Data model

```
~/.prime/agent/sessions/<id>.jsonl        raw conversation (append-only, runtime-owned)
~/.prime/agent/sessions/.cursor.json      learning cursor {offset, turns, corrections}
~/.prime/agent/sessions/<id>.digest.md    compacted narrative (cursor-side copy)
~/.prime/agent/sessions/harness/…         local harness store
~/.prime/agent/harness/harness_state.json global harness store (source for the prompt)
$VAULT/                                    notes; identity = frontmatter `id`, never the path
$VAULT/.meta/manifest.json                 {id: content hash} for the import/export dedupe
```

Organisation in the vault is by `type` property, tags and MOC queries - not numeric folders. The
importer keys on `id` so a note can be renamed or moved without being seen as delete + create.

## 6. Budgets and cost control

| scope | limit | where enforced |
|---|---|---|
| per dispatch | `max_usd`, `timeout_s` | middleware |
| per turn | `max_per_turn` per hook | middleware |
| per root event | `max_total_usd`, `max_iterations` | graph node |
| learning | data-gated (turns/tokens/corrections), quiet period | durable workflow |

Deterministic checks run first; a model is the fallback, never the default.

## 7. Milestones (one commit each)

| # | milestone | acceptance test |
|---|---|---|
| M1 | `core/bus.py`: Event/Verdict/Continuation, `on`, `dispatch`, three deliveries, guards | fake handlers: inline returns verdict; subprocess blocks a 1200-line staged commit; durable signals a stub workflow; depth 4 is refused |
| M2 | middleware + tracing (Langfuse span per dispatch/handler, local fallback) | one dispatch produces exactly one span pair with the verdict attached |
| M3 | `.githooks/` + `install_hooks.py` (pre-commit size/lint/fmt, commit-msg) | throwaway repo: 1200-line commit refused, 200-line accepted; `--no-verify` documented |
| M4 | hook configs + dispatcher wiring for `commit`, `turn_start`, `stage_end` | a policy reminder fires once per turn, never per tool; budget respected |
| M5 | `llm_wiki` import: id-keyed, manifest, guards (missing vault, malformed, delete safety) | temp vault: new/edited/deleted/malformed -> `{created:1, updated:1, deleted:1, skipped:1}` |
| M6 | declutter writes the note first, then imports (single writer) | run twice: second run is a no-op; the harness matches the vault |
| M7 | prune/garden on `cron:nightly` (deterministic archive rules) | stale session archived to `Archive/`, links intact, harness untouched |
| M8 | turn signal emitter (counter + correction detection) | N turns emit N signals; a correction counts 5x; zero turns emit nothing |
| M9 | factory migrated onto the bus (`stage_end` handlers) | a run progresses with the engine retired to one delivery class |
| M10 | docs, ADRs, and the weekly report | report shows notes/harness ratio and conflict count |

## 8. Risks and mitigations

| risk | mitigation |
|---|---|
| the abstraction swallows the codebase | a handler stays `fn(event) -> verdict`; anything larger is a workflow, not a handler |
| event storms / loops | depth cap, dedupe, iteration budget, `on_exhausted: escalate` |
| vault drift (edits not reaching the harness) | import is the only writer; manifest + hashes; conflicts saved as `.conflict-<ts>.md` |
| YAML fragility in notes | malformed notes are skipped and reported, never deleted |
| hook bypass (`--no-verify`) | CI runs the same checks; the report counts bypasses |
| cost runaway | budgets at dispatch, turn and root; model-free first |
| a model in the hot path | policy reminders are capped per turn and never per tool |

## 9. Non-goals

- Replacing the prime-agent runtime or adding a second scheduler.
- Local CI on every tool call.
- A Temporal workflow (or history) per lint check.
- Letting the learner generate or modify scripts.
- Round-tripping note edits without an explicit import step.

## 10. Dependency order

```
core/bus  ->  middleware+trace  ->  .githooks  ->  hook configs
                                          |
                       llm_wiki import  ->  declutter(notes first)  ->  prune
                                          |
                       turn emitter  ->  learning workflow  ->  factory on the bus
```
